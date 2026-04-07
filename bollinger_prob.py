"""
布林通道 + 概率計算（小時線版）

結算規則：正午 ET 瞬間收盤價 >= $X → Yes 勝

核心公式：
  距結算 N 小時，預測 std = hourly_std × √N
  P(close >= X) = 1 - norm.cdf(X, loc=currentPrice, scale=projectedStd)

兩種方法：
  高斯法：假設報酬率常態分佈
  歷史法：直接統計過去 N 小時後的實際價格分佈
"""
from typing import Optional
import numpy as np
import pandas as pd
from scipy.stats import norm


def calcHourlyBollinger(closes: pd.Series, window: int = 20) -> pd.DataFrame:
    """
    計算小時線布林通道

    Returns:
        DataFrame with columns: mid, upper, lower, std
    """
    mid = closes.rolling(window).mean()
    std = closes.rolling(window).std()
    return pd.DataFrame({
        "mid": mid,
        "upper": mid + 2 * std,
        "lower": mid - 2 * std,
        "std": std,
    })


def projectStd(hourlyStd: float, hoursAhead: float) -> float:
    """
    將小時 std 縮放到 N 小時後的預測 std
    公式：projected_std = hourly_std × √N
    """
    return hourlyStd * np.sqrt(max(hoursAhead, 0.25))  # 最少 15 分鐘


def gaussianAboveProb(currentPrice: float, projectedStd: float, targetPrice: float) -> float:
    """
    高斯法：P(正午收盤 >= targetPrice)
    假設正午收盤 ~ N(currentPrice, projectedStd)
    """
    if projectedStd <= 0:
        return 1.0 if currentPrice >= targetPrice else 0.0
    return float(1 - norm.cdf(targetPrice, loc=currentPrice, scale=projectedStd))


def historicalAboveProb(
    hourlyCloses: pd.Series,
    currentPrice: float,
    targetPrice: float,
    hoursAhead: float,
    sampleWindow: int = 90,
) -> float:
    """
    歷史法：統計過去 sampleWindow 天內，
    「從某一小時收盤開始，N 小時後的收盤」超過目標漲跌幅的頻率

    Args:
        hourlyCloses:  小時線收盤價序列
        currentPrice:  當前收盤價
        targetPrice:   目標關卡
        hoursAhead:    距結算幾小時
        sampleWindow:  取過去幾天的樣本（天）
    """
    hoursAhead = max(1, round(hoursAhead))
    closes = hourlyCloses.dropna()

    # 計算每個時間點「N 小時後的報酬率」
    futureReturns = closes.shift(-hoursAhead) / closes - 1
    # 只取最近 sampleWindow 天（24小時/天）的樣本
    futureReturns = futureReturns.iloc[-(sampleWindow * 24):]
    futureReturns = futureReturns.dropna()

    if len(futureReturns) == 0:
        return 0.0

    # 模擬：從 currentPrice 出發，加上歷史報酬率
    simulatedPrices = currentPrice * (1 + futureReturns)
    return float((simulatedPrices >= targetPrice).mean())


def calcAllMarketProbs(
    currentPrice: float,
    hourlyStd: float,
    hourlyCloses: pd.Series,
    hoursAhead: float,
    markets: list,
) -> list:
    """
    對一個事件的所有關卡同時計算概率

    Args:
        currentPrice: 最新小時線收盤價
        hourlyStd:    最近 20 根小時線的 std
        hourlyCloses: 小時線收盤序列（用於歷史法）
        hoursAhead:   距結算幾小時
        markets:      list of dicts from polymarket_data.parseEventMarkets

    Returns:
        same list with added keys: projectedStd, gaussProb, histProb
    """
    projStd = projectStd(hourlyStd, hoursAhead)
    result = []
    for m in markets:
        target = m["targetPrice"]
        gProb = gaussianAboveProb(currentPrice, projStd, target)
        hProb = historicalAboveProb(hourlyCloses, currentPrice, target, hoursAhead)
        result.append({
            **m,
            "projectedStd": projStd,
            "gaussProb": gProb,
            "histProb": hProb,
        })
    return result


if __name__ == "__main__":
    # 測試
    currentPrice = 73200.0
    hourlyStd = 800.0
    hoursAhead = 11.3

    projStd = projectStd(hourlyStd, hoursAhead)
    print(f"當前價格：${currentPrice:,.0f}")
    print(f"小時 std：${hourlyStd:,.0f}")
    print(f"距結算：{hoursAhead:.1f} 小時")
    print(f"預測 std：${projStd:,.0f}  （{hourlyStd:.0f} × √{hoursAhead:.1f}）")
    print()

    targets = [70000, 72000, 74000, 76000, 78000]
    print(f"  {'關卡':>10}  {'高斯概率':>8}")
    print("  " + "-" * 25)
    for t in targets:
        p = gaussianAboveProb(currentPrice, projStd, t)
        print(f"  ${t:>9,.0f}  {p:>8.3f}")
