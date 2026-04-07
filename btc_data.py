"""
Binance 現貨 BTCUSDT 歷史 OHLCV 數據抓取（日線 + 小時線）
"""
import time
import requests
import pandas as pd
from datetime import datetime, timezone

BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"


def fetchKlines(symbol: str, interval: str, startTime: int = None, endTime: int = None, limit: int = 1000) -> list:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if startTime:
        params["startTime"] = startTime
    if endTime:
        params["endTime"] = endTime
    resp = requests.get(BINANCE_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def klinesToDf(klines: list, indexName: str = "datetime") -> pd.DataFrame:
    df = pd.DataFrame(klines, columns=[
        "openTime", "open", "high", "low", "close", "volume",
        "closeTime", "quoteVolume", "trades", "takerBuyBase", "takerBuyQuote", "ignore"
    ])
    df[indexName] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    df = df.set_index(indexName)[["open", "high", "low", "close", "volume"]]
    df = df.astype(float)
    return df


def fetchBtcDaily(startDate: str = "2022-01-01", endDate: str = None) -> pd.DataFrame:
    """
    抓取 BTCUSDT 日線數據

    Returns:
        DataFrame，index 為 UTC date，欄位：open, high, low, close, volume
    """
    startMs = int(pd.Timestamp(startDate, tz="UTC").timestamp() * 1000)
    endMs = int(pd.Timestamp(endDate, tz="UTC").timestamp() * 1000) if endDate else None

    allKlines = []
    since = startMs
    while True:
        klines = fetchKlines("BTCUSDT", "1d", startTime=since, endTime=endMs, limit=1000)
        if not klines:
            break
        allKlines.extend(klines)
        if len(klines) < 1000:
            break
        since = klines[-1][0] + 1
        time.sleep(0.3)

    df = klinesToDf(allKlines)
    df.index = df.index.date  # 轉成 date 方便和 Polymarket 對齊
    return df


def fetchBtcHourly(lookbackHours: int = 500) -> pd.DataFrame:
    """
    抓取最近 N 根小時線（自動分頁，支援超過 1000 根）

    Returns:
        DataFrame，index 為 UTC datetime，欄位：open, high, low, close, volume
    """
    endMs = None
    allKlines = []
    remaining = lookbackHours

    while remaining > 0:
        batch = min(remaining, 1000)
        klines = fetchKlines("BTCUSDT", "1h", endTime=endMs, limit=batch)
        if not klines:
            break
        allKlines = klines + allKlines
        remaining -= len(klines)
        if len(klines) < batch:
            break
        endMs = klines[0][0] - 1  # 往前再抓
        time.sleep(0.2)

    return klinesToDf(allKlines)


def hoursUntilNoonET() -> float:
    """
    計算距離今天正午 ET 還有幾小時
    3月已進入 EDT（UTC-4），正午 ET = UTC 16:00
    11月~3月為 EST（UTC-5），正午 ET = UTC 17:00
    """
    now = datetime.now(timezone.utc)
    month = now.month
    # EDT: 3月第二個週日 ~ 11月第一個週日
    isDst = 3 <= month <= 10
    noonUtcHour = 16 if isDst else 17

    noonToday = now.replace(hour=noonUtcHour, minute=0, second=0, microsecond=0)
    if now >= noonToday:
        # 已過今天正午，算明天
        from datetime import timedelta
        noonToday += timedelta(days=1)

    diff = (noonToday - now).total_seconds() / 3600
    return diff


if __name__ == "__main__":
    print("=== 日線測試 ===")
    daily = fetchBtcDaily("2026-01-01")
    print(f"抓到 {len(daily)} 根日線，最新：{daily.index[-1]}  收盤=${daily['close'].iloc[-1]:,.0f}")

    print("\n=== 小時線測試 ===")
    hourly = fetchBtcHourly(48)
    print(f"抓到 {len(hourly)} 根小時線")
    print(hourly.tail(3)[["open", "close"]])

    print(f"\n=== 距離結算 ===")
    h = hoursUntilNoonET()
    print(f"距今天正午 ET 還有 {h:.1f} 小時")
