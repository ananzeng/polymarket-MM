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
import threading
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

from polymarket_data import CLOB_URL, fetchEventBySlug, parseJsonList
from notifier import sendTelegram
from py_clob_client_v2 import (
    AssetType, BalanceAllowanceParams, ClobClient, MarketOrderArgs, OpenOrderParams,
    OrderArgs, OrderMarketCancelParams, OrdersScoringParams, OrderType,
    PartialCreateOrderOptions, Side,
)

load_dotenv()
logger = logging.getLogger(__name__)

TZ_UTC8 = timezone(timedelta(hours=8))

marketSlug = os.getenv("MARKET_SLUG", "nba-lebron-james-next-team")
marketMatch = os.getenv("MARKET_MATCH", "Cleveland Cavaliers")
offsetCents = float(os.getenv("OFFSET_CENTS", "1.0"))
maxSpreadCents = float(os.getenv("MAX_SPREAD_CENTS", "0.2"))
orderSize = float(os.getenv("ORDER_SIZE", "200"))
refreshInterval = int(os.getenv("REFRESH_INTERVAL", "15"))
cooldownSeconds = int(os.getenv("COOLDOWN", "60"))
dryRun = os.getenv("DRY_RUN", "false").lower() == "true"
logDir = os.getenv("LOG_DIR", "log")
logLevel = os.getenv("LOG_LEVEL", "INFO").upper()
alertSound = os.getenv("ALERT_SOUND", "/System/Library/Sounds/Glass.aiff")

# Bait layer: a small order at the top of book that gets hit first, acting as an early
# warning. When it is consumed enough (or the main order is hit), retreat the main order.
baitEnabled = os.getenv("BAIT_ENABLED", "false").lower() == "true"
baitOffsetCents = float(os.getenv("BAIT_OFFSET_CENTS", "0.4"))
baitSize = float(os.getenv("BAIT_SIZE", "15"))
baitTriggerRatio = float(os.getenv("BAIT_TRIGGER_RATIO", "0.5"))
tripPauseSeconds = int(os.getenv("TRIP_PAUSE_SECONDS", "30"))
pollInterval = float(os.getenv("POLL_INTERVAL", "1.0"))

# How often (seconds) to check whether the main orders are scoring (earning rewards); 0 disables.
scoringCheckInterval = int(os.getenv("SCORING_CHECK_INTERVAL", "20"))

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
    privateKey = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not privateKey:
        raise ValueError("POLYMARKET_PRIVATE_KEY not found in .env")

    _applyProxy()

    funder = os.environ.get("POLYMARKET_FUNDER")
    client = ClobClient(
        CLOB_URL,
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


def getFunder() -> str:
    return os.environ["POLYMARKET_FUNDER"].lower()


def parseFloat(resp, key):
    value = resp.get(key) if isinstance(resp, dict) else resp
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def getBalanceShares(client, token: str = None, assetType=AssetType.CONDITIONAL) -> float:
    resp = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=assetType, token_id=token),
    )
    return (parseFloat(resp, "balance") or 0.0) / SHARE_DECIMALS


def appendCsvRow(path: str, header: list, row: list):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    newFile = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if newFile:
            writer.writerow(header)
        writer.writerow(row)


def resolveMarket() -> dict:
    """Locate the target sub-market inside the gamma event and extract order/reward params."""
    event = fetchEventBySlug(marketSlug)
    if not event:
        raise ValueError(f"Event not found for slug={marketSlug}")

    for m in event.get("markets", []):
        question = m.get("question") or ""
        if marketMatch.lower() not in question.lower():
            continue

        tokenIds = parseJsonList(m.get("clobTokenIds"))

        return {
            "question": question,
            "conditionId": m.get("conditionId"),
            "yesToken": tokenIds[0],
            "noToken": tokenIds[1],
            "tickSize": float(m.get("orderPriceMinTickSize", 0.001)),
            "rewardsMinSize": float(m.get("rewardsMinSize", 0) or 0),
            "rewardsMaxSpread": float(m.get("rewardsMaxSpread", 0) or 0),
            "negRisk": bool(event.get("negRisk")),
        }

    raise ValueError(f"No market matching '{marketMatch}' found in the event")


class PolymarketMaker:
    def __init__(self, client, market: dict):
        self.client = client
        self.market = market
        self.tickSize = market["tickSize"]
        self.priceDecimals = len(f"{self.tickSize:.10f}".rstrip("0").split(".")[1])
        self.cooldownUntil = 0.0
        # Set by the dashboard to stop the loop; live status attrs below are read by it.
        self.stopEvent = threading.Event()
        self.lastMid = None
        self.lastQuotes = {}
        self.lastScoring = {}
        self.lastScoringTs = 0.0

    def run(self):
        logger.info("Target market: %s", self.market["question"])
        logger.info("conditionId=%s", self.market["conditionId"])
        logger.info(
            "offset=%.1f¢ size=%s refresh=%ss cooldown=%ss dryRun=%s",
            offsetCents, orderSize, refreshInterval, cooldownSeconds, dryRun,
        )
        if baitEnabled:
            logger.info("Bait layer: offset=%.1f¢ size=%s triggerRatio=%.2f poll=%ss",
                        baitOffsetCents, baitSize, baitTriggerRatio, pollInterval)
        if offsetCents > self.market["rewardsMaxSpread"]:
            logger.warning("offset %.1f¢ > rewardsMaxSpread %.1f¢; orders will not earn rewards!",
                           offsetCents, self.market["rewardsMaxSpread"])
        if orderSize < self.market["rewardsMinSize"]:
            logger.warning("size %s < rewardsMinSize %s; orders will not earn rewards!",
                           orderSize, self.market["rewardsMinSize"])

        if dryRun:
            logger.info("=== DRY RUN: compute only, no orders; run once then exit ===")
            if baitEnabled:
                self._quoteLayered()
            else:
                self._tick()
            return

        try:
            if baitEnabled:
                self._runLayered()
            else:
                self._runSimple()
        except KeyboardInterrupt:
            logger.info("Stopping, cancelling all orders...")
            self._cancelAll()
            logger.info("Done")

    def _runSimple(self):
        while not self.stopEvent.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("tick error: %s", e)
            self.stopEvent.wait(refreshInterval)

    def _runLayered(self):
        while not self.stopEvent.is_set():
            try:
                tracked = self._quoteLayered()
                if not tracked:
                    self.stopEvent.wait(pollInterval)
                    continue
                deadline = time.time() + refreshInterval
                lastScoringCheck = time.time()
                tripped = False
                while time.time() < deadline:
                    if self.stopEvent.wait(pollInterval):
                        break
                    baitFilled, mainFilled = self._pollFills(tracked)
                    if mainFilled > 0 or baitFilled >= baitTriggerRatio * baitSize:
                        logger.info("Bait tripped: baitFilled=%.2f mainFilled=%.2f -> retreat",
                                    baitFilled, mainFilled)
                        playAlertSound()
                        sendTelegram(f"⚡ 誘餌單被吃 ({marketMatch})：誘餌={baitFilled:.1f} "
                                     f"主單={mainFilled:.1f} 股 — 撤退中")
                        tripped = True
                        break
                    if scoringCheckInterval and time.time() - lastScoringCheck >= scoringCheckInterval:
                        self._checkScoring(tracked)
                        lastScoringCheck = time.time()
                self._cancelAll()
                self._flattenInventory()
                if tripped:
                    logger.info("Bait tripped, pausing all quoting for %ds", tripPauseSeconds)
                    self.stopEvent.wait(tripPauseSeconds)
                    self.cooldownUntil = time.time() + cooldownSeconds
                    logger.info("Entering cooldown %ds (main+bait spread doubled)", cooldownSeconds)
            except Exception as e:
                logger.error("layered tick error: %s", e)
                self.stopEvent.wait(pollInterval)

    def _tick(self):
        if self._handleInventory():
            return
        self._refreshOrders()

    def _handleInventory(self) -> bool:
        """Detect whether we were filled into inventory; if so, flatten at market and cool down."""
        yesBal = getBalanceShares(self.client, self.market["yesToken"])
        noBal = getBalanceShares(self.client, self.market["noToken"])
        if yesBal <= DUST_SHARES and noBal <= DUST_SHARES:
            return False

        logger.info("Inventory detected Yes=%.2f No=%.2f; cancelling orders then flattening", yesBal, noBal)
        playAlertSound()
        sendTelegram(f"🔔 被成交 ({marketMatch})：Yes={yesBal:.2f} No={noBal:.2f} 股 — 平倉中")
        self._cancelAll()
        if yesBal > DUST_SHARES:
            self._flatten(self.market["yesToken"], yesBal, "Yes")
        if noBal > DUST_SHARES:
            self._flatten(self.market["noToken"], noBal, "No")
        self.cooldownUntil = time.time() + cooldownSeconds
        logger.info("Entering cooldown %ds (re-quote spread doubled)", cooldownSeconds)
        return True

    def _prepareQuote(self):
        """Cancel open orders and return the current mid, or None when this cycle should be skipped."""
        self._cancelAll()

        mid, spreadCents = self._getMidAndSpreadCents()
        if mid is None:
            logger.warning("No valid midpoint; skipping this cycle")
            return None

        if spreadCents > maxSpreadCents:
            logger.info("Spread %.2f¢ > max %.2f¢; skipping this cycle (book too thin)",
                        spreadCents, maxSpreadCents)
            return None

        return mid

    def _refreshOrders(self):
        mid = self._prepareQuote()
        if mid is None:
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
        self.lastMid = mid
        self.lastQuotes = {"main Yes": bidYes, "main No": bidNo}

        self._placeOrder(self.market["yesToken"], bidYes, orderSize, "Yes")
        self._placeOrder(self.market["noToken"], bidNo, orderSize, "No")

    def _quoteLayered(self) -> dict:
        """Place main orders (reward-earning) plus small bait orders at the top of book.

        Returns {orderId: {"kind": "main"/"bait", "size": float}} for fill tracking.
        """
        mid = self._prepareQuote()
        if mid is None:
            return {}

        effOffset = offsetCents
        effBaitOffset = baitOffsetCents
        if time.time() < self.cooldownUntil:
            effOffset = offsetCents * 2
            effBaitOffset = baitOffsetCents * 2
            logger.info("In cooldown, main+bait spread doubled → %.1f¢/%.1f¢", effOffset, effBaitOffset)
            if effOffset > self.market["rewardsMaxSpread"]:
                logger.info("Cooldown spread %.1f¢ exceeds rewardsMaxSpread %.1f¢; no quotes, flattening, waiting out cooldown",
                            effOffset, self.market["rewardsMaxSpread"])
                self._flattenInventory()
                return {}
        mainOff = effOffset / 100.0
        baitOff = effBaitOffset / 100.0

        mainYes = self._clampPrice(mid - mainOff)
        mainNo = self._clampPrice((1 - mid) - mainOff)
        baitYes = self._clampPrice(mid - baitOff)
        baitNo = self._clampPrice((1 - mid) - baitOff)
        logger.info("mid=%.4f | main Yes@%s No@%s x%s | bait Yes@%s No@%s x%s",
                    mid, mainYes, mainNo, orderSize, baitYes, baitNo, baitSize)
        self.lastMid = mid
        self.lastQuotes = {"main Yes": mainYes, "main No": mainNo,
                           "bait Yes": baitYes, "bait No": baitNo}

        plan = [
            (self.market["yesToken"], mainYes, orderSize, "main", "main Yes"),
            (self.market["noToken"], mainNo, orderSize, "main", "main No"),
            (self.market["yesToken"], baitYes, baitSize, "bait", "bait Yes"),
            (self.market["noToken"], baitNo, baitSize, "bait", "bait No"),
        ]
        tracked = {}
        for token, price, size, kind, label in plan:
            oid = self._placeOrder(token, price, size, label)
            if oid:
                tracked[oid] = {"kind": kind, "size": size, "label": label}
        return tracked

    def _pollFills(self, tracked: dict):
        """Return (baitFilledShares, mainFilledShares) across the tracked orders."""
        openOrders = self.client.get_open_orders(OpenOrderParams(market=self.market["conditionId"]))
        matched = {o["id"]: float(o.get("size_matched", 0) or 0) for o in openOrders}

        baitFilled = 0.0
        mainFilled = 0.0
        for oid, info in tracked.items():
            filled = matched[oid] if oid in matched else self._confirmFilled(oid, info["label"])
            if info["kind"] == "bait":
                baitFilled += filled
            else:
                mainFilled += filled
        return baitFilled, mainFilled

    def _confirmFilled(self, oid: str, label: str) -> float:
        """An order missing from get_open_orders is either truly filled or just not indexed
        yet (placed <1-2s ago); confirm via the single-order endpoint instead of assuming."""
        try:
            order = self.client.get_order(oid)
        except Exception:
            return 0.0
        if not isinstance(order, dict):
            return 0.0
        filled = float(order.get("size_matched", 0) or 0)
        if filled > 0:
            logger.info("Order %s (%s) gone from book, confirmed filled=%.2f status=%s",
                        oid, label, filled, order.get("status"))
        return filled

    def _flattenInventory(self):
        for token, label in [(self.market["yesToken"], "Yes"), (self.market["noToken"], "No")]:
            bal = getBalanceShares(self.client, token)
            if bal > DUST_SHARES:
                self._flatten(token, bal, label)

    def shutdown(self):
        """Cancel all orders and flatten any inventory; used by external controllers (dashboard)."""
        self._cancelAll()
        self._flattenInventory()

    def _checkScoring(self, tracked: dict):
        """Log whether the main orders are currently scoring (earning rewards)."""
        mainIds = [oid for oid, info in tracked.items() if info["kind"] == "main"]
        if not mainIds:
            return
        try:
            result = self.client.are_orders_scoring(OrdersScoringParams(orderIds=mainIds))
        except Exception as e:
            logger.error("scoring check failed: %s", e)
            return

        scoring = sum(1 for oid in mainIds if result.get(oid))
        parts = [f"{tracked[oid]['label']}={'✅' if result.get(oid) else '❌'}" for oid in mainIds]
        logger.info("Reward scoring: %d/%d earning | %s", scoring, len(mainIds), " ".join(parts))
        self.lastScoring = {tracked[oid]["label"]: bool(result.get(oid)) for oid in mainIds}
        self.lastScoringTs = time.time()

    def _getMidAndSpreadCents(self):
        """Mid price and best-bid/best-ask spread (cents), from a single Yes order-book fetch.

        get_order_book returns a raw dict: {"bids": [{"price","size"}, ...], "asks": [...]}.
        """
        book = self.client.get_order_book(self.market["yesToken"])
        bids = [float(b["price"]) for b in (book.get("bids") or [])]
        asks = [float(a["price"]) for a in (book.get("asks") or [])]
        if not bids or not asks:
            return None, None
        bestBid, bestAsk = max(bids), min(asks)
        return (bestBid + bestAsk) / 2, (bestAsk - bestBid) * 100

    def _placeOrder(self, token: str, price: float, size: float, label: str):
        if dryRun:
            logger.info("[dryRun] skip placing buy %s @ %s x%s", label, price, size)
            return None

        try:
            order = self.client.create_order(
                OrderArgs(token_id=token, price=price, size=size, side=Side.BUY),
                options=PartialCreateOrderOptions(tick_size=str(self.tickSize),
                                                  neg_risk=self.market["negRisk"]),
            )
            resp = self.client.post_order(order, OrderType.GTC, True)
            oid = resp.get("orderID") if isinstance(resp, dict) else None
            logger.info("Placed buy %s @ %s x%s → %s", label, price, size, oid or resp)
            self._logCsv("place", label, price, size, str(oid or resp))
            return oid
        except Exception as e:
            logger.error("Failed to place buy %s @ %s: %s", label, price, e)
            return None

    def _flatten(self, token: str, shares: float, label: str):
        # Floor to the 2-decimal size precision so we never try to sell more than we hold
        # (round() could round up above the actual balance and get rejected).
        shares = math.floor(shares * 100) / 100
        logger.info("Flatten: market-sell %s %.2f shares", label, shares)
        if dryRun:
            logger.info("[dryRun] skip flatten")
            return

        try:
            order = self.client.create_market_order(
                MarketOrderArgs(token_id=token, amount=shares, side=Side.SELL, order_type=OrderType.FAK),
                options=PartialCreateOrderOptions(tick_size=str(self.tickSize),
                                                  neg_risk=self.market["negRisk"]),
            )
            resp = self.client.post_order(order, OrderType.FAK)
            logger.info("Flatten response: %s", resp)
            sendTelegram(f"✅ 已平倉 {label} {shares} 股 ({marketMatch})")
            self._logCsv("flatten", label, "", shares, str(resp))
        except Exception as e:
            logger.error("Flatten failed %s %.2f shares: %s", label, shares, e)
            sendTelegram(f"❌ 平倉失敗 {label} {shares} 股 ({marketMatch})：{e}")
            self._logCsv("flatten_error", label, "", shares, str(e))

    def _cancelAll(self):
        if dryRun:
            return
        try:
            resp = self.client.cancel_market_orders(
                OrderMarketCancelParams(market=self.market["conditionId"])
            )
            logger.info("Cancelled orders: %s", resp)
        except Exception as e:
            logger.error("Failed to cancel orders: %s", e)

    def _clampPrice(self, price: float) -> float:
        snapped = round(price, self.priceDecimals)
        lo, hi = self.tickSize, round(1 - self.tickSize, self.priceDecimals)
        return min(max(snapped, lo), hi)

    def _logCsv(self, action: str, side: str, price, size, extra: str):
        appendCsvRow(LOG_CSV, ["time", "action", "side", "price", "size", "extra"],
                     [datetime.now(TZ_UTC8).isoformat(), action, side, price, size, extra])


def main():
    setupLogging()
    market = resolveMarket()
    client = initClobClient()
    PolymarketMaker(client, market).run()


if __name__ == "__main__":
    main()
