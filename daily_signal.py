"""
即時訊號工具

三種模式：
  1. 單次執行：python daily_signal.py
  2. 持續監測：python daily_signal.py --monitor
  3. 自動下單：python daily_signal.py --monitor --auto-trade
"""
import argparse
import logging
import os
import time
from datetime import date, datetime

from dotenv import load_dotenv

from btc_data import fetchBtcHourly, hoursUntilNoonET
from polymarket_data import fetchDailyAboveEvent
from bollinger_prob import calcAllMarketProbs, calcHourlyBollinger

load_dotenv()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

BOLLINGER_WINDOW = 20   # 小時線布林通道窗口
HIST_LOOKBACK = 90      # 歷史法取過去幾天樣本
BET_SIZE = 5.0          # 每筆下注金額 (USDC)
MAX_DAILY_BETS = 1      # 每天最多下注次數
TRADE_HOUR_UTC = 11     # 只在這個 UTC 小時內下單（台灣 19:00）


def _setProxy():
    proxy = os.environ.get("POLYMARKET_PROXY")
    if proxy:
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["HTTP_PROXY"] = proxy


def _clearProxy():
    os.environ.pop("HTTPS_PROXY", None)
    os.environ.pop("HTTP_PROXY", None)


def initClobClient():
    from py_clob_client.client import ClobClient

    privateKey = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not privateKey:
        raise ValueError("POLYMARKET_PRIVATE_KEY not found in .env")

    funder = os.environ.get("POLYMARKET_FUNDER")
    _setProxy()
    try:
        client = ClobClient(
            "https://clob.polymarket.com",
            key=privateKey,
            chain_id=137,
            signature_type=2,
            funder=funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
    finally:
        _clearProxy()
    logger.warning("ClobClient 初始化完成")
    return client


def placeBuyYes(client, tokenId: str, yesPrice: float, size: float = BET_SIZE) -> dict:
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    order = OrderArgs(
        token_id=tokenId,
        price=round(yesPrice, 2),
        size=size,
        side=BUY,
    )
    signed = client.create_order(order)
    _setProxy()
    try:
        resp = client.post_order(signed, OrderType.GTC)
    finally:
        _clearProxy()
    return resp


def expectedValue(prob: float, yesPrice: float, bet: float = 1) -> float:
    """押 bet 元買 Yes 的期望值"""
    payout = bet / yesPrice
    return prob * payout - bet


def runSignal(targetDate: date = None, verbose: bool = True) -> list:
    """
    計算今日所有關卡的訊號

    Returns:
        list of dicts，每個代表一個關卡的完整分析
    """
    if targetDate is None:
        targetDate = date.today()

    # 1. 抓小時線
    hourly = fetchBtcHourly(lookbackHours=HIST_LOOKBACK * 24 + 50)
    bands = calcHourlyBollinger(hourly["close"], window=BOLLINGER_WINDOW)
    currentPrice = hourly["close"].iloc[-1]
    hourlyStd = bands["std"].iloc[-1]

    # 2. 距結算幾小時
    hoursAhead = hoursUntilNoonET()

    # 3. 抓 Polymarket 今日事件
    event = fetchDailyAboveEvent(targetDate)
    if not event:
        print(f"找不到 {targetDate} 的 Polymarket 事件")
        return []

    # 4. 計算所有關卡的概率
    markets = calcAllMarketProbs(
        currentPrice=currentPrice,
        hourlyStd=hourlyStd,
        hourlyCloses=hourly["close"],
        hoursAhead=hoursAhead,
        markets=event["markets"],
    )

    # 5. 計算期望值、Kelly
    results = []
    for m in markets:
        yesPrice = m["yesPrice"]
        if yesPrice is None or yesPrice <= 0:
            continue

        gEdge = m["gaussProb"] - yesPrice
        hEdge = m["histProb"] - yesPrice
        gEv = expectedValue(m["gaussProb"], yesPrice)
        hEv = expectedValue(m["histProb"], yesPrice)

        results.append({
            **m,
            "currentPrice": currentPrice,
            "hourlyStd": hourlyStd,
            "hoursAhead": hoursAhead,
            "projectedStd": m["projectedStd"],
            "gaussEdge": gEdge,
            "histEdge": hEdge,
            "gaussEv1": gEv,
            "histEv1": hEv,
        })

    if verbose:
        printSignal(results, targetDate, currentPrice, hourlyStd, hoursAhead)

    return results


def analyzeEdge(r: dict) -> tuple:
    """
    根據歷史法判斷是否值得買 Yes
    回傳 (action, edge)
    action: 'yes' / None
    """
    hEdge = r["histEdge"]
    if hEdge > 0.08:
        return "yes", hEdge
    return None, 0.0


def printSignal(results: list, targetDate: date, currentPrice: float, hourlyStd: float, hoursAhead: float):
    print(f"\n{'='*80}")
    print(f"  BTC Polymarket 訊號  {targetDate}")
    print(f"{'='*80}")
    print(f"  當前 BTC 價格  : ${currentPrice:>10,.0f}")
    print(f"  小時線 std     : ${hourlyStd:>10,.0f}")
    print(f"  距結算         : {hoursAhead:>9.1f} 小時")
    print(f"  預測 std       : ${results[0]['projectedStd']:>10,.0f}" if results else "")
    print()

    # 表頭 固定！ 人工已經編輯好了
    header = f"{'關卡':>8}  {'PolyYes':>10}  {'高斯%':>2}  {'歷史%':>4}  {'高斯偏差':>6}  {'歷史偏差':>4}  {'EV/$1':>5}  建議"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for r in results:
        target = r["targetPrice"]
        yesP = r["yesPrice"]
        gProb = r["gaussProb"]
        hProb = r["histProb"]
        gEdge = r["gaussEdge"]
        hEdge = r["histEdge"]
        hEv = r["histEv1"]

        action, _ = analyzeEdge(r)
        suggestion = "★ 買Yes" if action == "yes" else ""

        marker = " ←" if abs(target - currentPrice) < 2500 else ""

        print(f"  ${target:>9,.0f}  {yesP:>7.3f}  {gProb:>6.3f}  {hProb:>6.3f}  "
              f"{gEdge:>+8.3f}  {hEdge:>+8.3f}  {hEv:>+7.3f}  {suggestion}{marker}")

    print()

    # 摘要
    yesOpps = [r for r in results if analyzeEdge(r)[0] == "yes"]

    if not yesOpps:
        print("  目前無明顯套利機會（歷史法偏差 < 8%）")
    else:
        print("  ★ 買 Yes 機會：")
        for r in yesOpps:
            print(f"     above ${r['targetPrice']:,.0f}  "
                  f"歷史概率 {r['histProb']:.1%} vs Poly {r['yesPrice']:.1%}  "
                  f"押$1 期望值 {r['histEv1']:+.3f}  偏差 {r['histEdge']:+.1%}")
    print()


EDGE_CHANGE_THRESHOLD = 0.02  # edge 變化超過 2% 才重新印


def monitor(interval: int = 60, autoTrade: bool = False) -> None:
    mode = "自動下單" if autoTrade else "監測"
    print(f"開始{mode}模式（每 {interval} 秒刷新，Ctrl+C 停止）")
    if autoTrade:
        print(f"  每筆 ${BET_SIZE} USDC，每天最多 {MAX_DAILY_BETS} 筆")
    print()

    client = None
    if autoTrade:
        client = initClobClient()

    seen = {}  # {targetPrice: lastEdge}
    todayBetCount = 0
    todayDate = date.today()

    while True:
        now = datetime.now().strftime("%H:%M:%S")

        # 跨日重置
        if date.today() != todayDate:
            todayDate = date.today()
            todayBetCount = 0
            seen.clear()

        try:
            results = runSignal(verbose=False)
        except Exception as e:
            print(f"[{now}] 抓取失敗：{e}")
            time.sleep(interval)
            continue

        if not results:
            print(f"[{now}] 找不到今日事件")
            time.sleep(interval)
            continue

        opps = [r for r in results if analyzeEdge(r)[0] == "yes"]

        if not opps:
            print(f"[{now}] BTC ${results[0]['currentPrice']:,.0f}  無套利機會")
        else:
            newOpps = []
            for r in opps:
                tp = r["targetPrice"]
                edge = r["histEdge"]
                if tp not in seen or abs(edge - seen[tp]) >= EDGE_CHANGE_THRESHOLD:
                    newOpps.append(r)
                    seen[tp] = edge

            if newOpps:
                os.system("afplay /System/Library/Sounds/Glass.aiff &")
                print(f"\n[{now}] ★ 發現 {len(newOpps)} 個新機會  BTC ${results[0]['currentPrice']:,.0f}")
                for r in newOpps:
                    print(f"  above ${r['targetPrice']:>9,.0f}  "
                          f"Poly {r['yesPrice']:.3f}  "
                          f"歷史 {r['histProb']:.3f}  "
                          f"edge {r['histEdge']:+.3f}  "
                          f"EV/$1 {r['histEv1']:+.3f}")

                # 自動下單（只在 TRADE_HOUR_UTC 那個小時內）
                utcHour = datetime.utcnow().hour
                if autoTrade and todayBetCount < MAX_DAILY_BETS and utcHour == TRADE_HOUR_UTC:
                    best = max(opps, key=lambda r: r["histEdge"])
                    tokenId = best.get("clobYesTokenId")
                    if tokenId:
                        try:
                            resp = placeBuyYes(client, tokenId, best["yesPrice"])
                            todayBetCount += 1
                            print(f"\n  💰 已下單！above ${best['targetPrice']:,.0f}  "
                                  f"price {best['yesPrice']:.3f}  ${BET_SIZE} USDC")
                            print(f"     回應：{resp}")
                            print(f"     今日已下 {todayBetCount}/{MAX_DAILY_BETS} 筆\n")
                        except Exception as e:
                            print(f"\n  ❌ 下單失敗：{e}\n")
                    else:
                        print(f"\n  ⚠️ 找不到 tokenId，無法下單\n")
                elif autoTrade and todayBetCount >= MAX_DAILY_BETS:
                    print(f"  （今日已達 {MAX_DAILY_BETS} 筆上限，不再下單）\n")
                elif autoTrade and utcHour != TRADE_HOUR_UTC:
                    print(f"  （目前 UTC {utcHour}:00，下單時段 UTC {TRADE_HOUR_UTC}:00 / 台灣 {(TRADE_HOUR_UTC+8)%24}:00）\n")
                else:
                    print()
            else:
                print(f"[{now}] BTC ${results[0]['currentPrice']:,.0f}  {len(opps)} 個機會（無變化）")

        time.sleep(interval)


def parseArgs():
    parser = argparse.ArgumentParser(description="BTC Polymarket 訊號工具")
    parser.add_argument("--monitor", action="store_true", help="持續監測模式")
    parser.add_argument("--auto-trade", action="store_true", help="啟用自動下單（需搭配 --monitor）")
    parser.add_argument("--interval", type=int, default=60, help="監測間隔（秒），預設 60")
    return parser.parse_args()


if __name__ == "__main__":
    args = parseArgs()
    if args.monitor:
        try:
            monitor(interval=args.interval, autoTrade=args.auto_trade)
        except KeyboardInterrupt:
            print("\n監測已停止")
    else:
        runSignal()
