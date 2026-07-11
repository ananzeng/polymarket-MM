"""
Compute and chart PnL from Polymarket fills + LP rewards.

Three lines over time: trading PnL, LP rewards, and their total.
Trading PnL is rebuilt from the real fills (get_trades, mark-to-market); rewards come
from the daily rewards API (daily granularity). Chart is saved to LOG_DIR/pnl.png.

Run: venv/bin/python pnl.py
"""
import os
from datetime import datetime, timezone, timedelta

from lp_maker import initClobClient, resolveMarket, getFunder, parseFloat, logDir
from py_clob_client_v2 import TradeParams

UTC = timezone.utc


def userFills(trades: list, funder: str) -> list:
    """Return the user's actual fills as sorted (ts, outcome, side, size, price).

    When the user is the maker, the trade's top-level size is the taker's total, so the
    user's real fill must be read from maker_orders[].matched_amount.
    """
    fills = []
    for t in trades:
        if t.get("status") != "CONFIRMED":
            continue
        ts = int(t["match_time"])
        if t.get("trader_side") == "TAKER":
            fills.append((ts, t["outcome"], t["side"], float(t["size"]), float(t["price"])))
        else:
            for mo in t.get("maker_orders", []):
                if mo.get("maker_address", "").lower() == funder:
                    fills.append((ts, mo["outcome"], mo["side"],
                                  float(mo["matched_amount"]), float(mo["price"])))
    fills.sort()
    return fills


def fillsToCashAndPos(fills: list) -> tuple:
    """Aggregate fills into (net cash flow, per-outcome position)."""
    cash = 0.0
    pos = {"Yes": 0.0, "No": 0.0}
    for _, oc, side, sz, px in fills:
        if side == "BUY":
            cash -= px * sz
            pos[oc] += sz
        else:
            cash += px * sz
            pos[oc] -= sz
    return cash, pos


def rewardForDay(client, dayStr: str) -> float:
    """Total LP-reward earnings for one UTC day (YYYY-MM-DD)."""
    rows = client.get_total_earnings_for_user_for_day(dayStr)
    return sum(float(r.get("earnings", 0) or 0) for r in rows) if rows else 0.0


def dayStartUtc(now: datetime) -> float:
    return datetime(now.year, now.month, now.day, tzinfo=UTC).timestamp()


def netCpPerDay(net: float, elapsedHours: float, capital: float) -> float:
    """Projected daily yield (%) on the deployed capital."""
    return net / elapsedHours * 24 / capital * 100


def tradingSeries(fills: list, marks: dict):
    """Cumulative trading PnL (realized + mark-to-market) at each fill time."""
    cashFlow = 0.0
    pos = {"Yes": 0.0, "No": 0.0}
    lastPrice = dict(marks)
    times, pnl = [], []
    for ts, oc, side, sz, px in fills:
        if side == "BUY":
            cashFlow -= px * sz
            pos[oc] += sz
        else:
            cashFlow += px * sz
            pos[oc] -= sz
        lastPrice[oc] = px
        mtm = pos["Yes"] * lastPrice["Yes"] + pos["No"] * lastPrice["No"]
        times.append(datetime.fromtimestamp(ts, UTC))
        pnl.append(cashFlow + mtm)
    return times, pnl


def rewardSeries(client, firstDate, today):
    """Cumulative LP rewards over time (stepped daily). Returns (times, cumReward)."""
    times = [datetime(firstDate.year, firstDate.month, firstDate.day, tzinfo=UTC)]
    cumVals = [0.0]
    cum = 0.0
    d = firstDate
    while d <= today:
        cum += rewardForDay(client, d.strftime("%Y-%m-%d"))
        endTs = (datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1)
                 if d < today else datetime.now(UTC))
        times.append(endTs)
        cumVals.append(cum)
        d += timedelta(days=1)
    return times, cumVals


def ffill(times, vals, queries):
    """Forward-fill: value at each query time = last val with time <= query (0 before start)."""
    out = []
    i, last = 0, 0.0
    for q in queries:
        while i < len(times) and times[i] <= q:
            last = vals[i]
            i += 1
        out.append(last)
    return out


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    market = resolveMarket()
    client = initClobClient()
    funder = getFunder()

    trades = client.get_trades(TradeParams(market=market["conditionId"]))
    fills = userFills(trades, funder)
    if not fills:
        print("No confirmed fills yet.")
        return

    def mid(token):
        return parseFloat(client.get_midpoint(token), "mid") or 0.5

    marks = {"Yes": mid(market["yesToken"]), "No": mid(market["noToken"])}

    tTimes, tPnl = tradingSeries(fills, marks)
    firstDate = datetime.fromtimestamp(fills[0][0], UTC).date()
    today = datetime.now(UTC).date()
    rTimes, rCum = rewardSeries(client, firstDate, today)

    now = datetime.now(UTC)
    axis = sorted(set(tTimes + rTimes + [now]))
    trLine = ffill(tTimes, tPnl, axis)
    rwLine = ffill(rTimes, rCum, axis)
    totLine = [a + b for a, b in zip(trLine, rwLine)]

    trading, reward, total = trLine[-1], rwLine[-1], totLine[-1]
    print(f"Fills            : {len(fills)}")
    print(f"Trading PnL      : ${trading:+.4f}")
    print(f"LP rewards       : ${reward:+.4f}")
    print(f"Total PnL        : ${total:+.4f}")

    plt.figure(figsize=(11, 6))
    plt.axhline(0, color="#999999", linewidth=0.8)
    # Total drawn first (thick, faded) so Trading stays visible on top; they overlap when rewards are tiny.
    plt.plot(axis, totLine, color="#8E44AD", linewidth=4.0, alpha=0.45, label=f"Total (${total:+.2f})")
    plt.plot(axis, trLine, color="#2E86DE", linewidth=1.5, label=f"Trading PnL (${trading:+.2f})")
    plt.plot(axis, rwLine, color="#27AE60", linewidth=1.8, label=f"LP rewards (${reward:+.2f})")
    plt.title(f"PnL — {market['question']}")
    plt.ylabel("USDC")
    plt.legend(loc="best")
    plt.grid(True, alpha=0.3)
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=UTC))
    plt.gcf().autofmt_xdate()
    plt.tight_layout()

    os.makedirs(logDir, exist_ok=True)
    out = os.path.join(logDir, "pnl.png")
    plt.savefig(out, dpi=130)
    print(f"\nChart saved to {out}")
    if os.uname().sysname == "Darwin":
        os.system(f"open '{out}'")


if __name__ == "__main__":
    main()
