"""
Fetch Polymarket "Bitcoin above ___ on [date]?" market data.

Settlement rule: if the Binance BTC/USDT 1-minute candle close at noon ET is >= $X, Yes wins.
"""
import json
import re
import time
import logging
import requests
from typing import Optional
from datetime import date, datetime, timezone, timedelta

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


def buildAboveSlug(targetDate: date) -> str:
    """2026-03-16 → bitcoin-above-on-march-16"""
    month = targetDate.strftime("%B").lower()
    day = str(targetDate.day)
    return f"bitcoin-above-on-{month}-{day}"


def parseTargetPrice(question: str) -> Optional[float]:
    """
    'Will the price of Bitcoin be above $74,000 on March 16?' → 74000.0
    'Bitcoin above $106K on August 29?' → 106000.0
    """
    match = re.search(r"\$([0-9,]+)(K?)", question, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    if match.group(2).upper() == "K":
        value *= 1000
    return value


def fetchEventBySlug(slug: str) -> Optional[dict]:
    try:
        resp = requests.get(f"{GAMMA_URL}/events", params={"slug": slug, "limit": 1}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except Exception as e:
        logger.warning("fetchEventBySlug %s failed: %s", slug, e)
        return None


def parseJsonList(value) -> list:
    """Gamma returns list fields as JSON strings; normalize to a real list."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return value or []


def parseEventMarkets(event: dict) -> list:
    """
    Parse all markets of an event.

    Returns list of:
    {
        question, targetPrice,
        yesPrice, noPrice,
        yesWon,   # True / False / None (unsettled)
        closed,
        volume,
        clobYesTokenId, clobNoTokenId,
    }
    """
    results = []
    for m in event.get("markets", []):
        targetPrice = parseTargetPrice(m.get("question", ""))
        if targetPrice is None:
            continue

        outcomePrices = parseJsonList(m.get("outcomePrices"))
        clobTokenIds = parseJsonList(m.get("clobTokenIds"))

        outcomesYesPrice = float(outcomePrices[0]) if len(outcomePrices) > 0 else None
        outcomesNoPrice = float(outcomePrices[1]) if len(outcomePrices) > 1 else None
        isClosed = m.get("closed", False)

        # lastTradePrice = last actual traded price, matches what the site shows.
        # In-progress markets use lastTradePrice; settled ones use outcomePrices to decide the winner.
        lastTradePrice = m.get("lastTradePrice")
        if lastTradePrice is not None:
            lastTradePrice = float(lastTradePrice)

        # In progress: use lastTradePrice as the Yes price.
        # After settlement: outcomePrices becomes 1 or 0.
        if isClosed:
            yesPrice = outcomesYesPrice
        else:
            yesPrice = lastTradePrice if lastTradePrice is not None else outcomesYesPrice

        noPrice = (1 - yesPrice) if yesPrice is not None else None
        yesWon = (outcomesYesPrice >= 0.99) if (isClosed and outcomesYesPrice is not None) else None

        results.append({
            "question": m.get("question"),
            "targetPrice": targetPrice,
            "yesPrice": yesPrice,
            "noPrice": noPrice,
            "yesWon": yesWon,
            "closed": isClosed,
            "volume": float(m.get("volumeNum", 0) or 0),
            "clobYesTokenId": clobTokenIds[0] if len(clobTokenIds) > 0 else None,
            "clobNoTokenId": clobTokenIds[1] if len(clobTokenIds) > 1 else None,
        })

    return sorted(results, key=lambda x: x["targetPrice"])


def fetchDailyAboveEvent(targetDate: date) -> Optional[dict]:
    """
    Fetch the bitcoin-above event for a given day.

    Returns:
        { targetDate, slug, eventId, volume, markets } or None
    """
    slug = buildAboveSlug(targetDate)
    event = fetchEventBySlug(slug)
    if not event:
        return None

    markets = parseEventMarkets(event)
    if not markets:
        return None

    return {
        "targetDate": targetDate,
        "slug": slug,
        "eventId": event.get("id"),
        "volume": float(event.get("volume", 0) or 0),
        "markets": markets,
    }


def fetchClobPriceHistory(tokenId: str) -> list:
    """
    Fetch a token's full price history from the CLOB API (one point per hour).

    Returns:
        list of {"t": unix_ts, "p": float}, in ascending time order
    """
    try:
        resp = requests.get(f"{CLOB_URL}/prices-history", params={
            "market": tokenId,
            "interval": "max",
            "fidelity": 60,
        }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("history", [])
    except Exception as e:
        logger.warning("fetchClobPriceHistory %s failed: %s", tokenId[:20], e)
        return []


def getPriceAtTime(history: list, targetDt: datetime) -> Optional[float]:
    """
    Find the most recent price at or before targetDt in the price-history series.

    Args:
        history:  list of {"t": unix_ts, "p": float}
        targetDt: UTC datetime

    Returns:
        float price or None
    """
    targetTs = targetDt.timestamp()
    result = None
    for h in history:
        if h["t"] <= targetTs:
            result = h["p"]
        else:
            break
    return result


def fetchAllClobHistories(
    markets: list,
    delaySeconds: float = 0.2,
) -> dict:
    """
    Fetch CLOB price histories for all strikes at once.

    Returns:
        {clobYesTokenId: [{"t": unix_ts, "p": float}, ...]}
    """
    cache = {}
    for m in markets:
        tokenId = m.get("clobYesTokenId")
        if not m.get("closed") or tokenId is None:
            continue
        if tokenId in cache:
            continue
        cache[tokenId] = fetchClobPriceHistory(tokenId)
        time.sleep(delaySeconds)
    return cache


def enrichMarketsFromCache(
    markets: list,
    signalDt: datetime,
    clobCache: dict,
) -> list:
    enriched = []
    for m in markets:
        tokenId = m.get("clobYesTokenId")
        if not m.get("closed") or tokenId is None:
            enriched.append({**m, "clobYesPriceAtSignal": None})
            continue
        history = clobCache.get(tokenId, [])
        priceAtSignal = getPriceAtTime(history, signalDt)
        enriched.append({**m, "clobYesPriceAtSignal": priceAtSignal})
    return enriched


def fetchHistoricalAboveEvents(
    startDate: date,
    endDate: date,
    delaySeconds: float = 0.3,
    verbose: bool = True,
) -> list:
    """Batch-fetch all bitcoin-above events within a date range."""
    results = []
    current = startDate
    if verbose:
        total = (endDate - startDate).days + 1
        print(f"Searching {startDate} ~ {endDate}, {total} days total...")

    while current <= endDate:
        event = fetchDailyAboveEvent(current)
        if event:
            closedCount = sum(1 for m in event["markets"] if m["closed"])
            if verbose:
                print(f"  ✓ {current}  {len(event['markets'])} strikes ({closedCount} settled)  vol=${event['volume']:,.0f}")
            results.append(event)
        time.sleep(delaySeconds)
        current += timedelta(days=1)

    if verbose:
        print(f"\nFound {len(results)} valid events")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    event = fetchDailyAboveEvent(date.today())
    if event:
        print(f"Today's event: {event['slug']}")
        print(f"Volume: ${event['volume']:,.0f}\n")
        print(f"  {'Strike':>10}  {'YesPrice':>8}  {'NoPrice':>8}  {'Status'}")
        print("  " + "-" * 45)
        for m in event["markets"]:
            status = "✓ Yes won" if m["yesWon"] is True else ("✗ No won" if m["yesWon"] is False else "in progress")
            print(f"  ${m['targetPrice']:>9,.0f}  {m['yesPrice']:>8.4f}  {m['noPrice']:>8.4f}  {status}")
