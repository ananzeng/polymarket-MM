"""
回測邏輯（小時線版）

結算規則：Binance BTC/USDT 正午 ET 那根 1 分鐘 K 線收盤價 >= $X → Yes 勝

流程：
1. 抓 BTC 小時線
2. 抓 Polymarket 歷史 bitcoin-above 事件
3. 對每個事件，找到當天正午 ET 前最近一根小時線當作「開盤時的當前價格」
4. 用當時的小時線 std × √(距正午小時數) 算預測 std
5. 計算每個關卡的高斯/歷史概率，對比 Polymarket 結算前定價
6. 輸出回測報告
"""
import logging
from datetime import date, datetime, timezone, timedelta

import numpy as np
import pandas as pd

from btc_data import fetchBtcHourly
from polymarket_data import (
    fetchHistoricalAboveEvents, enrichMarketsWithClobPrices,
    fetchAllClobHistories, enrichMarketsFromCache,
)
from bollinger_prob import calcHourlyBollinger, calcAllMarketProbs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOLLINGER_WINDOW = 20
HIST_LOOKBACK = 90      # 歷史法樣本天數
SIGNAL_HOUR_UTC = 9     # 模擬每天幾點 UTC 看盤下注（UTC 9 = 台灣 17:00 = 距正午 ET 7hr）
MIN_EDGE = 0.08


def noonEtToUtc(targetDate: date) -> datetime:
    """
    正午 ET → UTC
    3~10 月 EDT = UTC-4，正午 = UTC 16:00
    11~2 月 EST = UTC-5，正午 = UTC 17:00
    """
    isDst = 3 <= targetDate.month <= 10
    noonUtcHour = 16 if isDst else 17
    return datetime(targetDate.year, targetDate.month, targetDate.day,
                    noonUtcHour, 0, tzinfo=timezone.utc)


def findHourlyRow(hourlyDf: pd.DataFrame, targetDt: datetime):
    """找到 targetDt 前最近一根小時線的 index"""
    candidates = hourlyDf.index[hourlyDf.index <= targetDt]
    return candidates[-1] if len(candidates) > 0 else None


def runBacktest(
    hourlyDf: pd.DataFrame,
    events: list,
    signalHourUtc: int = SIGNAL_HOUR_UTC,
    bollingerWindow: int = BOLLINGER_WINDOW,
    histLookback: int = HIST_LOOKBACK,
) -> pd.DataFrame:
    """
    核心回測

    Args:
        hourlyDf:      小時線 DataFrame（index 為 UTC datetime）
        events:        Polymarket 歷史事件列表
        signalHourUtc: 每天模擬下注的時間（UTC 小時），預設 9 = 台灣 17:00

    Returns:
        DataFrame，每行代表一個「事件日 × 關卡」的回測結果
    """
    bands = calcHourlyBollinger(hourlyDf["close"], window=bollingerWindow)
    #print(bands)
    records = []

    for event in events:
        targetDate = event["targetDate"]

        # 模擬下注時間：當天 signalHourUtc:00 UTC
        signalDt = datetime(targetDate.year, targetDate.month, targetDate.day,
                            signalHourUtc, 0, tzinfo=timezone.utc)
        noonDt = noonEtToUtc(targetDate)
        hoursAhead = (noonDt - signalDt).total_seconds() / 3600

        if hoursAhead <= 0:
            logger.warning("%s 下注時間在結算後，跳過", targetDate)
            continue

        # 找到下注時間點前最近的小時線
        signalRow = findHourlyRow(hourlyDf, signalDt)
        if signalRow is None:
            logger.warning("%s 找不到對應小時線，跳過", targetDate)
            continue

        currentPrice = hourlyDf.loc[signalRow, "close"]
        hourlyStd = bands.loc[signalRow, "std"]

        if pd.isna(hourlyStd) or hourlyStd <= 0:
            continue

        # 歷史法：取 signalRow 前 histLookback 天的小時線
        endIdx = hourlyDf.index.get_loc(signalRow)
        startIdx = max(0, endIdx - histLookback * 24)
        historicalCloses = hourlyDf["close"].iloc[startIdx:endIdx + 1]
        #print("historicalCloses", historicalCloses)
        #print(historicalCloses)
        # 補上 CLOB 歷史定價（下注時間點的真實價格）
        marketsEnriched = enrichMarketsWithClobPrices(
            event["markets"], signalDt, delaySeconds=0.15
        )

        # 計算所有關卡的概率
        marketsWithProb = calcAllMarketProbs(
            currentPrice=currentPrice,
            hourlyStd=hourlyStd,
            hourlyCloses=historicalCloses,
            hoursAhead=hoursAhead,
            markets=marketsEnriched,
        )

        for m in marketsWithProb:
            if m["yesWon"] is None:  # 未結算跳過
                continue

            yesPrice = m.get("clobYesPriceAtSignal")
            if yesPrice is None or yesPrice <= 0:
                continue

            gaussEdge = m["gaussProb"] - yesPrice
            histEdge = m["histProb"] - yesPrice

            def ev1(p, price):
                return p * (1 / price) - 1

            records.append({
                "targetDate": targetDate,
                "signalDt": signalDt,
                "hoursAhead": hoursAhead,
                "targetPrice": m["targetPrice"],
                "currentPrice": currentPrice,
                "hourlyStd": hourlyStd,
                "projectedStd": m["projectedStd"],
                "yesPrice": yesPrice,
                "gaussProb": m["gaussProb"],
                "histProb": m["histProb"],
                "gaussEdge": gaussEdge,
                "histEdge": histEdge,
                "gaussEv1": ev1(m["gaussProb"], yesPrice),
                "histEv1": ev1(m["histProb"], yesPrice),
                "yesWon": m["yesWon"],
                "eventVolume": event["volume"],
            })

    return pd.DataFrame(records)


def runBacktestWithCache(
    hourlyDf: pd.DataFrame,
    events: list,
    clobCaches: dict,
    signalHourUtc: int = SIGNAL_HOUR_UTC,
    bollingerWindow: int = BOLLINGER_WINDOW,
    histLookback: int = HIST_LOOKBACK,
) -> pd.DataFrame:
    bands = calcHourlyBollinger(hourlyDf["close"], window=bollingerWindow)
    records = []

    for event in events:
        targetDate = event["targetDate"]
        eventId = event["eventId"]

        signalDt = datetime(targetDate.year, targetDate.month, targetDate.day,
                            signalHourUtc, 0, tzinfo=timezone.utc)
        noonDt = noonEtToUtc(targetDate)
        hoursAhead = (noonDt - signalDt).total_seconds() / 3600

        if hoursAhead <= 0:
            continue

        signalRow = findHourlyRow(hourlyDf, signalDt)
        if signalRow is None:
            continue

        currentPrice = hourlyDf.loc[signalRow, "close"]
        hourlyStd = bands.loc[signalRow, "std"]

        if pd.isna(hourlyStd) or hourlyStd <= 0:
            continue

        endIdx = hourlyDf.index.get_loc(signalRow)
        startIdx = max(0, endIdx - histLookback * 24)
        historicalCloses = hourlyDf["close"].iloc[startIdx:endIdx + 1]

        cache = clobCaches.get(eventId, {})
        marketsEnriched = enrichMarketsFromCache(event["markets"], signalDt, cache)

        marketsWithProb = calcAllMarketProbs(
            currentPrice=currentPrice,
            hourlyStd=hourlyStd,
            hourlyCloses=historicalCloses,
            hoursAhead=hoursAhead,
            markets=marketsEnriched,
        )

        for m in marketsWithProb:
            if m["yesWon"] is None:
                continue

            yesPrice = m.get("clobYesPriceAtSignal")
            if yesPrice is None or yesPrice <= 0:
                continue

            gaussEdge = m["gaussProb"] - yesPrice
            histEdge = m["histProb"] - yesPrice

            def ev1(p, price):
                return p * (1 / price) - 1

            records.append({
                "targetDate": targetDate,
                "signalDt": signalDt,
                "hoursAhead": hoursAhead,
                "targetPrice": m["targetPrice"],
                "currentPrice": currentPrice,
                "hourlyStd": hourlyStd,
                "projectedStd": m["projectedStd"],
                "yesPrice": yesPrice,
                "gaussProb": m["gaussProb"],
                "histProb": m["histProb"],
                "gaussEdge": gaussEdge,
                "histEdge": histEdge,
                "gaussEv1": ev1(m["gaussProb"], yesPrice),
                "histEv1": ev1(m["histProb"], yesPrice),
                "yesWon": m["yesWon"],
                "eventVolume": event["volume"],
            })

    return pd.DataFrame(records)


def runHourlyComparison(hourlyDf: pd.DataFrame, events: list, clobCaches: dict) -> None:
    print(f"\n{'='*70}")
    print(f"  各小時回測比較（歷史法，edge > {MIN_EDGE:.0%}，每注 $1）")
    print(f"{'='*70}")
    print(f"  {'UTC':>5}  {'台灣':>6}  {'距結算':>6}  {'筆數':>5}  {'勝率':>7}  {'總損益':>9}  {'最大回撤':>9}")
    print("  " + "-" * 65)

    bestPnl = -999
    bestHour = None

    for h in range(16):
        df = runBacktestWithCache(hourlyDf, events, clobCaches, signalHourUtc=h)
        if df.empty:
            print(f"  UTC {h:>2}  TW {(h+8)%24:>2}:00  {'':>6}  {'無資料':>5}")
            continue

        trades = df[df["histEdge"] > MIN_EDGE]
        if trades.empty:
            print(f"  UTC {h:>2}  TW {(h+8)%24:>2}:00  {16-h:>5.0f}h  {0:>5}  {'無信號':>7}")
            continue

        trades = trades.sort_values("signalDt").copy()
        trades["pnl"] = trades.apply(
            lambda r: (1 / r["yesPrice"] - 1) if r["yesWon"] else -1, axis=1
        )
        cumPnl = trades["pnl"].cumsum()
        wins = trades["yesWon"].sum()
        total = len(trades)
        finalPnl = cumPnl.iloc[-1]
        maxDD = (cumPnl - cumPnl.cummax()).min()

        # 粗估距結算小時（DST 期間 noon ET = UTC 16）
        hoursToNoon = 16 - h

        marker = " ←" if h == 9 else ""
        print(f"  UTC {h:>2}  TW {(h+8)%24:>2}:00  {hoursToNoon:>5}h  {total:>5}  "
              f"{wins/total:>6.1%}  ${finalPnl:>+8.2f}  ${maxDD:>8.2f}{marker}")

        if finalPnl > bestPnl:
            bestPnl = finalPnl
            bestHour = h

    if bestHour is not None:
        print(f"\n  最佳下注時間：UTC {bestHour}:00（台灣 {(bestHour+8)%24}:00）損益 ${bestPnl:+.2f}")
    print()


def analyzeResults(df: pd.DataFrame) -> None:
    if df.empty:
        print("沒有可分析的數據")
        return

    print(f"\n{'='*60}")
    print(f"回測報告")
    print(f"{'='*60}")
    print(f"事件數  ：{df['targetDate'].nunique()} 天")
    print(f"關卡總數：{len(df)}")
    print(f"日期範圍：{df['targetDate'].min()} ~ {df['targetDate'].max()}")
    print(f"Yes 勝率：{df['yesWon'].mean():.1%}（整體，含遠離現價的關卡）")

    # 校準分析
    print(f"\n--- 校準分析 ---")
    bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    binLabels = ["0-10%","10-20%","20-30%","30-40%","40-50%",
                 "50-60%","60-70%","70-80%","80-90%","90-100%"]

    for method, col in [("高斯法", "gaussProb"), ("歷史法", "histProb")]:
        df["_bin"] = pd.cut(df[col], bins=bins, labels=binLabels)
        cal = df.groupby("_bin", observed=True)["yesWon"].agg(["mean", "count"])
        cal.columns = ["實際勝率", "樣本數"]
        valid = cal[cal["樣本數"] >= 5]
        print(f"\n{method}（樣本數 >= 5 的區間）：")
        print(valid.to_string())

    # 套利機會回測
    print(f"\n--- 套利機會回測（偏差 > {MIN_EDGE:.0%}）---")
    for method, edgeCol, probCol, evCol in [
        ("高斯法", "gaussEdge", "gaussProb", "gaussEv1"),
        ("歷史法", "histEdge", "histProb", "histEv1"),
    ]:
        pos = df[df[edgeCol] > MIN_EDGE]
        neg = df[df[edgeCol] < -MIN_EDGE]
        print(f"\n{method}：")
        if len(pos) > 0:
            print(f"  正偏差（買 Yes）：{len(pos):3d} 筆  Yes 實際勝率 {pos['yesWon'].mean():.1%}"
                  f"  平均期望值 {pos[evCol].mean():+.1f}")
        if len(neg) > 0:
            print(f"  負偏差（買 No） ：{len(neg):3d} 筆  No  實際勝率 {(~neg['yesWon']).mean():.1%}"
                  f"  平均期望值 {(-neg[evCol]).mean():+.1f}")

    # 模擬累積報酬
    for methodName, edgeCol in [("高斯法", "gaussEdge"), ("歷史法", "histEdge")]:
        print(f"\n--- 模擬累積報酬（{methodName}正偏差，押 $1）---")
        buySignals = df[df[edgeCol] > MIN_EDGE].copy()
        if len(buySignals) == 0:
            print("  無信號")
            continue
        buySignals = buySignals.sort_values("signalDt")
        buySignals["pnl"] = buySignals.apply(
            lambda r: (1 / r["yesPrice"] - 1) if r["yesWon"] else -1, axis=1
        )
        cumPnl = buySignals["pnl"].cumsum()
        print(f"  下注次數：{len(buySignals)}")
        print(f"  勝率    ：{buySignals['yesWon'].mean():.1%}")
        print(f"  總損益  ：${cumPnl.iloc[-1]:+,.2f}（每注 $1）")
        print(f"  最大回撤：${(cumPnl - cumPnl.cummax()).min():,.2f}")


INITIAL_CAPITAL = 10.0
BET_SIZE = 1.0


def generateTradeLogs(df: pd.DataFrame) -> None:
    strategies = [
        ("gaussian", "gaussEdge", "gaussProb", "gaussEv1"),
        ("historical", "histEdge", "histProb", "histEv1"),
        ("both", None, "histProb", "histEv1"),
    ]

    for name, edgeCol, probCol, evCol in strategies:
        if name == "both":
            mask = (df["gaussEdge"] > MIN_EDGE) & (df["histEdge"] > MIN_EDGE)
            trades = df[mask].copy()
            trades["_edge"] = trades["histEdge"]
        else:
            trades = df[df[edgeCol] > MIN_EDGE].copy()
            trades["_edge"] = trades[edgeCol]

        if trades.empty:
            logger.info("trade_log_%s.csv：無信號", name)
            continue

        trades = trades.sort_values("targetDate").reset_index(drop=True)
        trades["pnl"] = trades.apply(
            lambda r: (BET_SIZE / r["yesPrice"] - BET_SIZE) if r["yesWon"] else -BET_SIZE,
            axis=1,
        )
        trades["cumPnl"] = trades["pnl"].cumsum()
        trades["capital"] = INITIAL_CAPITAL + trades["cumPnl"]

        out = trades[["targetDate", "targetPrice", "currentPrice", "yesPrice",
                       probCol, "_edge", evCol, "yesWon", "pnl", "cumPnl", "capital"]].copy()
        out.columns = ["date", "targetPrice", "currentPrice", "yesPrice",
                       "prob", "edge", "ev1", "won", "pnl", "cumPnl", "capital"]

        filename = f"trade_log_{name}.csv"
        out.to_csv(filename, index=False)
        wins = out["won"].sum()
        total = len(out)
        logger.info("%s：%d 筆，勝率 %.1f%%，總損益 $%.2f",
                    filename, total, wins / total * 100, out["cumPnl"].iloc[-1])


def main():
    endDate = date.today() - timedelta(days=1)
    startDate = date(2026, 1, 1)  # bitcoin-above 系列最早日期

    logger.info("抓取 BTC 小時線...")
    totalDays = (endDate - startDate).days + HIST_LOOKBACK + 5
    logger.info(totalDays)
    hourlyDf = fetchBtcHourly(lookbackHours=totalDays * 24)
    logger.info("抓到 %d 根小時線", len(hourlyDf))
    print(hourlyDf)

    logger.info("抓取 Polymarket 事件（%s ~ %s）...", startDate, endDate)
    events = fetchHistoricalAboveEvents(startDate, endDate, verbose=True)

    if not events:
        logger.error("找不到任何事件")
        return

    # 一次性抓好所有 CLOB 歷史定價
    total = len(events)
    logger.info("抓取 CLOB 歷史定價（%d 個事件）...", total)
    clobCaches = {}
    for i, event in enumerate(events, 1):
        cache = fetchAllClobHistories(event["markets"], delaySeconds=0.15)
        clobCaches[event["eventId"]] = cache
        print(f"\r  CLOB 進度：{i}/{total} ({i/total:.0%})", end="", flush=True)
    print()
    logger.info("CLOB 快取完成")

    # 主回測（UTC 9）
    logger.info("開始回測（模擬每天 UTC %d:00 下注）...", SIGNAL_HOUR_UTC)
    df = runBacktestWithCache(hourlyDf, events, clobCaches)

    if df.empty:
        logger.error("回測結果為空")
        return

    df.to_csv("backtest_results.csv", index=False)
    logger.info("結果已存至 backtest_results.csv")
    analyzeResults(df)
    generateTradeLogs(df)

    # 各小時比較
    runHourlyComparison(hourlyDf, events, clobCaches)


if __name__ == "__main__":
    main()
