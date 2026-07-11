"""
Reward CP (cost-performance) for the market in .env: is it worth farming?

Weighs today's LP rewards against the trading cost of getting filled and flattened,
as a net daily yield on the capital deployed. Net CP > 0 = the market is worth it.

Run: venv/bin/python reward_cp.py
"""
from datetime import datetime, timezone

import lp_maker as lm
from pnl import userFills, fillsToCashAndPos, rewardForDay, dayStartUtc, netCpPerDay
from py_clob_client_v2 import TradeParams


def main():
    market = lm.resolveMarket()
    client = lm.initClobClient()
    funder = lm.getFunder()

    now = datetime.now(timezone.utc)
    todayStart = dayStartUtc(now)
    hrs = max((now.timestamp() - todayStart) / 3600, 0.1)

    trades = client.get_trades(TradeParams(market=market["conditionId"]))
    fills = [f for f in userFills(trades, funder) if f[0] >= todayStart]
    tradingPnL, _ = fillsToCashAndPos(fills)

    reward = rewardForDay(client, now.strftime("%Y-%m-%d"))

    capital = lm.orderSize  # two-sided (buy Yes + buy No) commits ~= orderSize USDC
    net = reward + tradingPnL

    def daily(x):
        return x / hrs * 24

    print(f"Market : {market['question']}")
    print(f"Window : today (UTC, {hrs:.1f}h elapsed) | {len(fills)} fills | capital ≈ ${capital:.0f}\n")
    print(f"  LP rewards   : +${reward:.4f}")
    print(f"  Trading PnL  : {tradingPnL:+.4f}   (fill/flatten spread cost)")
    print(f"  Net          : {net:+.4f}\n")
    print(f"Projected daily:  rewards ~${daily(reward):.2f}   trading ~${daily(tradingPnL):.2f}   net ~${daily(net):.2f}")
    print(f"\nReward CP (gross): {netCpPerDay(reward, hrs, capital):+.2f}%/day")
    print(f"Net CP           : {netCpPerDay(net, hrs, capital):+.2f}%/day   "
          f"{'GOOD to farm ✅' if net > 0 else 'LOSING — not worth it ❌'}")


if __name__ == "__main__":
    main()
