"""
Polymarket LP reward dynamic market-making bot.

Places two-sided limit orders (buy Yes + buy No) near a sub-market's midpoint to earn
liquidity rewards, cancelling and re-quoting every REFRESH_INTERVAL seconds to reduce
fill risk; on a detected fill it flattens at market and enters a cooldown.

All parameters are read from .env. Run: venv/bin/python lp_maker.py
"""
import csv
import logging
import math
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

from polymarket_data import fetchEventBySlug
from notifier import sendTelegram

load_dotenv()
logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
TZ_UTC8 = timezone(timedelta(hours=8))

marketSlug = os.getenv("MARKET_SLUG", "nba-lebron-james-next-team")
marketMatch = os.getenv("MARKET_MATCH", "Cleveland Cavaliers")
offsetCents = float(os.getenv("OFFSET_CENTS", "1.0"))
orderSize = float(os.getenv("ORDER_SIZE", "200"))
refreshInterval = int(os.getenv("REFRESH_INTERVAL", "15"))
cooldownSeconds = int(os.getenv("COOLDOWN", "60"))
dryRun = os.getenv("DRY_RUN", "false").lower() == "true"
logDir = os.getenv("LOG_DIR", "log")
logLevel = os.getenv("LOG_LEVEL", "INFO").upper()
alertSound = os.getenv("ALERT_SOUND", "/System/Library/Sounds/Glass.aiff")

LOG_CSV = os.path.join(logDir, "lp_maker_log.csv")
DUST_SHARES = 5.0        # inventory below this is treated as dust (min order size is 5, cannot flatten)
SHARE_DECIMALS = 1_000_000  # conditional token balances have 6 decimals


def setupLogging():
    """Log to both console and a date-named file under LOG_DIR."""
    os.makedirs(logDir, exist_ok=True)
    logFile = os.path.join(logDir, datetime.now(TZ_UTC8).strftime("%Y-%m-%d") + ".txt")
    logging.basicConfig(
        level=getattr(logging, logLevel, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(logFile, mode="a", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def playAlertSound():
    """Play a non-blocking alert sound (macOS afplay); ALERT_SOUND empty disables it."""
    if not alertSound:
        return
    try:
        subprocess.Popen(["afplay", alertSound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning("Failed to play alert sound: %s", e)


def _applyProxy():
    """
    py-clob-client uses a module-level httpx singleton that reads its proxy config only
    once at construction; changing env vars afterward has no effect. Replace the singleton
    with a client built with the proxy so every request (including orders) goes through it.
    """
    proxy = os.environ.get("POLYMARKET_PROXY")
    if not proxy:
        logger.warning("POLYMARKET_PROXY not set; connecting directly (orders will be geo-blocked in restricted regions)")
        return

    import httpx
    import py_clob_client_v2.http_helpers.helpers as helpers

    helpers._http_client = httpx.Client(http2=True, proxy=proxy)

    try:
        info = helpers._http_client.get("https://ipinfo.io/json", timeout=15).json()
        logger.info("Proxy applied, outbound exit IP: %s (%s)", info.get("ip"), info.get("country"))
    except Exception as e:
        logger.error("Proxy is unreachable (out of data / needs top-up / misconfigured): %s", e)
        raise


def initClobClient():
    from py_clob_client_v2 import ClobClient

    privateKey = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not privateKey:
        raise ValueError("POLYMARKET_PRIVATE_KEY not found in .env")

    _applyProxy()

    funder = os.environ.get("POLYMARKET_FUNDER")
    client = ClobClient(
        CLOB_HOST,
        137,
        key=privateKey,
        signature_type=2,
        funder=funder,
    )
    try:
        creds = client.derive_api_key()
    except Exception:
        creds = client.create_api_key()
    client.set_api_creds(creds)
    return client


def resolveMarket() -> dict:
    """Locate the target sub-market inside the gamma event and extract order/reward params."""
    import json

    event = fetchEventBySlug(marketSlug)
    if not event:
        raise ValueError(f"Event not found for slug={marketSlug}")

    for m in event.get("markets", []):
        question = m.get("question") or ""
        if marketMatch.lower() not in question.lower():
            continue

        tokenIds = m.get("clobTokenIds")
        if isinstance(tokenIds, str):
            tokenIds = json.loads(tokenIds)

        return {
            "question": question,
            "conditionId": m.get("conditionId"),
            "yesToken": tokenIds[0],
            "noToken": tokenIds[1],
            "tickSize": float(m.get("orderPriceMinTickSize", 0.001)),
            "rewardsMinSize": float(m.get("rewardsMinSize", 0) or 0),
            "rewardsMaxSpread": float(m.get("rewardsMaxSpread", 0) or 0),
        }

    raise ValueError(f"No market matching '{marketMatch}' found in the event")


class PolymarketMaker:
    def __init__(self, client, market: dict):
        self.client = client
        self.market = market
        self.tickSize = market["tickSize"]
        self.priceDecimals = max(0, len(f"{self.tickSize:.10f}".rstrip("0").split(".")[1]))
        self.cooldownUntil = 0.0

    def run(self):
        logger.info("Target market: %s", self.market["question"])
        logger.info("conditionId=%s", self.market["conditionId"])
        logger.info(
            "offset=%.1f¢ size=%s refresh=%ss cooldown=%ss dryRun=%s",
            offsetCents, orderSize, refreshInterval, cooldownSeconds, dryRun,
        )
        if offsetCents > self.market["rewardsMaxSpread"]:
            logger.warning("offset %.1f¢ > rewardsMaxSpread %.1f¢; orders will not earn rewards!",
                           offsetCents, self.market["rewardsMaxSpread"])
        if orderSize < self.market["rewardsMinSize"]:
            logger.warning("size %s < rewardsMinSize %s; orders will not earn rewards!",
                           orderSize, self.market["rewardsMinSize"])

        if dryRun:
            logger.info("=== DRY RUN: compute only, no orders; run once then exit ===")
            self._tick()
            return

        try:
            while True:
                try:
                    self._tick()
                except Exception as e:
                    logger.error("tick error: %s", e)
                time.sleep(refreshInterval)
        except KeyboardInterrupt:
            logger.info("Stopping, cancelling all orders...")
            self._cancelAll()
            logger.info("Done")

    def _tick(self):
        if self._handleInventory():
            return
        self._refreshOrders()

    def _handleInventory(self) -> bool:
        """Detect whether we were filled into inventory; if so, flatten at market and cool down."""
        yesBal = self._getBalanceShares(self.market["yesToken"])
        noBal = self._getBalanceShares(self.market["noToken"])
        if yesBal <= DUST_SHARES and noBal <= DUST_SHARES:
            return False

        logger.info("Inventory detected Yes=%.2f No=%.2f; cancelling orders then flattening", yesBal, noBal)
        playAlertSound()
        sendTelegram(f"🔔 Filled ({marketMatch}): Yes={yesBal:.2f} No={noBal:.2f} shares — flattening")
        self._cancelAll()
        if yesBal > DUST_SHARES:
            self._flatten(self.market["yesToken"], yesBal, "Yes")
        if noBal > DUST_SHARES:
            self._flatten(self.market["noToken"], noBal, "No")
        self.cooldownUntil = time.time() + cooldownSeconds
        logger.info("Entering cooldown %ds (re-quote spread doubled)", cooldownSeconds)
        return True

    def _refreshOrders(self):
        self._cancelAll()

        mid = self._getMid()
        if mid is None:
            logger.warning("No valid midpoint; skipping this cycle")
            return

        effOffset = offsetCents
        if time.time() < self.cooldownUntil:
            effOffset = offsetCents * 2
            logger.info("In cooldown, spread doubled → %.1f¢", effOffset)
        offset = effOffset / 100.0

        bidYes = self._clampPrice(mid - offset)
        bidNo = self._clampPrice((1 - mid) - offset)
        logger.info("mid=%.4f | buy Yes @ %s | buy No @ %s | size=%s",
                    mid, bidYes, bidNo, orderSize)

        self._placeBuy(self.market["yesToken"], bidYes, "Yes")
        self._placeBuy(self.market["noToken"], bidNo, "No")

    def _getMid(self):
        resp = self.client.get_midpoint(self.market["yesToken"])
        mid = self._parseFloat(resp, "mid")
        if mid is not None and 0.01 < mid < 0.99:
            return mid

        # Fallback: compute directly from the order book
        book = self.client.get_order_book(self.market["yesToken"])
        bids = [float(b.price) for b in (book.bids or [])]
        asks = [float(a.price) for a in (book.asks or [])]
        if bids and asks:
            return (max(bids) + min(asks)) / 2
        return None

    def _placeBuy(self, token: str, price: float, label: str):
        if dryRun:
            logger.info("[dryRun] skip placing buy %s @ %s x%s", label, price, orderSize)
            return

        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

        try:
            order = self.client.create_order(
                OrderArgs(token_id=token, price=price, size=orderSize, side=Side.BUY),
                options=PartialCreateOrderOptions(tick_size=str(self.tickSize), neg_risk=True),
            )
            resp = self.client.post_order(order, OrderType.GTC, True)
            oid = resp.get("orderID") if isinstance(resp, dict) else None
            logger.info("Placed buy %s @ %s x%s → %s", label, price, orderSize, oid or resp)
            self._logCsv("place", label, price, orderSize, str(oid or resp))
        except Exception as e:
            logger.error("Failed to place buy %s @ %s: %s", label, price, e)

    def _flatten(self, token: str, shares: float, label: str):
        # Floor to the 2-decimal size precision so we never try to sell more than we hold
        # (round() could round up above the actual balance and get rejected).
        shares = math.floor(shares * 100) / 100
        logger.info("Flatten: market-sell %s %.2f shares", label, shares)
        if dryRun:
            logger.info("[dryRun] skip flatten")
            return

        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side

        try:
            order = self.client.create_market_order(
                MarketOrderArgs(token_id=token, amount=shares, side=Side.SELL, order_type=OrderType.FAK),
                options=PartialCreateOrderOptions(tick_size=str(self.tickSize), neg_risk=True),
            )
            resp = self.client.post_order(order, OrderType.FAK)
            logger.info("Flatten response: %s", resp)
            sendTelegram(f"✅ Flattened {label} {shares} shares ({marketMatch})")
            self._logCsv("flatten", label, "", shares, str(resp))
        except Exception as e:
            logger.error("Flatten failed %s %.2f shares: %s", label, shares, e)
            sendTelegram(f"❌ Flatten failed {label} {shares} ({marketMatch}): {e}")
            self._logCsv("flatten_error", label, "", shares, str(e))

    def _cancelAll(self):
        if dryRun:
            return
        from py_clob_client_v2 import OrderMarketCancelParams

        try:
            resp = self.client.cancel_market_orders(
                OrderMarketCancelParams(market=self.market["conditionId"])
            )
            logger.info("Cancelled orders: %s", resp)
        except Exception as e:
            logger.error("Failed to cancel orders: %s", e)

    def _getBalanceShares(self, token: str) -> float:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType

        resp = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token),
        )
        raw = self._parseFloat(resp, "balance") or 0.0
        return raw / SHARE_DECIMALS

    def _clampPrice(self, price: float) -> float:
        snapped = round(price, self.priceDecimals)
        lo, hi = self.tickSize, round(1 - self.tickSize, self.priceDecimals)
        return min(max(snapped, lo), hi)

    @staticmethod
    def _parseFloat(resp, key):
        if isinstance(resp, dict):
            value = resp.get(key)
        else:
            value = resp
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _logCsv(self, action: str, side: str, price, size, extra: str):
        os.makedirs(logDir, exist_ok=True)
        newFile = not os.path.exists(LOG_CSV)
        with open(LOG_CSV, "a", newline="") as f:
            writer = csv.writer(f)
            if newFile:
                writer.writerow(["time", "action", "side", "price", "size", "extra"])
            writer.writerow([datetime.now(TZ_UTC8).isoformat(), action, side, price, size, extra])


def main():
    setupLogging()
    market = resolveMarket()
    client = initClobClient()
    PolymarketMaker(client, market).run()


if __name__ == "__main__":
    main()
