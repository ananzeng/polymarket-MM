"""
Polymarket「Bitcoin above ___ on [date]?」市場數據抓取

結算規則：Binance BTC/USDT 正午 ET 那根 1 分鐘 K 線收盤價 >= $X → Yes 勝
"""
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
        logger.warning("fetchEventBySlug %s 失敗：%s", slug, e)
        return None


def parseEventMarkets(event: dict) -> list:
    """
    解析事件的所有市場

    Returns list of:
    {
        question, targetPrice,
        yesPrice, noPrice,
        yesWon,   # True / False / None（未結算）
        closed,
        volume,
        clobYesTokenId, clobNoTokenId,
    }
    """
    import json
    results = []
    for m in event.get("markets", []):
        targetPrice = parseTargetPrice(m.get("question", ""))
        if targetPrice is None:
            continue

        outcomePrices = m.get("outcomePrices", [])
        if isinstance(outcomePrices, str):
            try:
                outcomePrices = json.loads(outcomePrices)
            except Exception:
                outcomePrices = []

        clobTokenIds = m.get("clobTokenIds", [])
        if isinstance(clobTokenIds, str):
            try:
                clobTokenIds = json.loads(clobTokenIds)
            except Exception:
                clobTokenIds = []

        outcomesYesPrice = float(outcomePrices[0]) if len(outcomePrices) > 0 else None
        outcomesNoPrice = float(outcomePrices[1]) if len(outcomePrices) > 1 else None
        isClosed = m.get("closed", False)

        # lastTradePrice = 最後實際成交價，和網站顯示一致
        # 進行中的市場用 lastTradePrice，已結算用 outcomePrices 判斷勝負
        lastTradePrice = m.get("lastTradePrice")
        if lastTradePrice is not None:
            lastTradePrice = float(lastTradePrice)

        # 進行中：用 lastTradePrice 作為 Yes 定價
        # 結算後：outcomePrices 會變成 1 或 0
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
    抓取某日的 bitcoin-above 事件

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
    從 CLOB API 抓取某個 token 的完整歷史定價（每小時一筆）

    Returns:
        list of {"t": unix_ts, "p": float}，按時間升序
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
        logger.warning("fetchClobPriceHistory %s 失敗：%s", tokenId[:20], e)
        return []


def getPriceAtTime(history: list, targetDt: datetime) -> Optional[float]:
    """
    從歷史定價序列中，找到 targetDt 前最近一筆的價格

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


def enrichMarketsWithClobPrices(
    markets: list,
    signalDt: datetime,
    delaySeconds: float = 0.2,
) -> list:
    """
    對每個市場，用 CLOB prices-history 補上 signalDt 時的定價

    結果會新增 key：
        clobYesPriceAtSignal  - 下注時間點的 Yes 定價（回測用）

    已結算（closed=True）的市場才需要補，進行中的用 lastTradePrice 即可
    """
    enriched = []
    for m in markets:
        if not m.get("closed") or m.get("clobYesTokenId") is None:
            enriched.append({**m, "clobYesPriceAtSignal": None})
            continue

        history = fetchClobPriceHistory(m["clobYesTokenId"])
        priceAtSignal = getPriceAtTime(history, signalDt)
        enriched.append({**m, "clobYesPriceAtSignal": priceAtSignal})
        time.sleep(delaySeconds)

    return enriched


def fetchAllClobHistories(
    markets: list,
    delaySeconds: float = 0.2,
) -> dict:
    """
    一次抓好所有關卡的 CLOB 歷史定價

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
    """批量抓取日期範圍內所有 bitcoin-above 事件"""
    results = []
    current = startDate
    if verbose:
        total = (endDate - startDate).days + 1
        print(f"搜尋 {startDate} ~ {endDate} 共 {total} 天...")

    while current <= endDate:
        event = fetchDailyAboveEvent(current)
        if event:
            closedCount = sum(1 for m in event["markets"] if m["closed"])
            if verbose:
                print(f"  ✓ {current}  {len(event['markets'])} 個關卡（{closedCount} 已結算）  vol=${event['volume']:,.0f}")
            results.append(event)
        time.sleep(delaySeconds)
        current += timedelta(days=1)

    if verbose:
        print(f"\n找到 {len(results)} 個有效事件")
    return results


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)

    event = fetchDailyAboveEvent(date.today())
    if event:
        print(f"今日事件：{event['slug']}")
        print(f"交易量：${event['volume']:,.0f}\n")
        print(f"  {'關卡':>10}  {'Yes定價':>8}  {'No定價':>8}  {'狀態'}")
        print("  " + "-" * 45)
        for m in event["markets"]:
            status = "✓ Yes勝" if m["yesWon"] is True else ("✗ No勝" if m["yesWon"] is False else "進行中")
            print(f"  ${m['targetPrice']:>9,.0f}  {m['yesPrice']:>8.4f}  {m['noPrice']:>8.4f}  {status}")
