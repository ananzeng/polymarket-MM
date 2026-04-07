"""
績效圖 - 讀取 backtest_results.csv，模擬三種策略的累積損益

規則：
- 每天 UTC 09:00 下注
- 一天只選一單：偏差絕對值最大的關卡
- 正偏差 → 買 Yes，負偏差 → 買 No
- 超過門檻才下注，否則跳過
- 初始資金 $10，每注 $1

三種策略：
1. 高斯法
2. 歷史法
3. 高斯+歷史（兩個都超過門檻）
"""
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

THRESHOLD = 0.08
INITIAL_CAPITAL = 10.0
BET_SIZE = 1.0


def calcPnl(row, direction):
    """計算單筆損益"""
    if direction == "yes":
        return (BET_SIZE / row["yesPrice"] - BET_SIZE) if row["yesWon"] else -BET_SIZE
    else:
        noPrice = 1 - row["yesPrice"]
        return (BET_SIZE / noPrice - BET_SIZE) if not row["yesWon"] else -BET_SIZE


def runStrategy(df, edgeCol):
    """
    只買 Yes：正偏差超過門檻的關卡全部下注
    edgeCol: 'gaussEdge' / 'histEdge' / 'bothEdge'
    """
    tmp = df.copy()

    if edgeCol == "bothEdge":
        # 高斯+歷史：兩個都 > 門檻
        mask = (tmp["gaussEdge"] > THRESHOLD) & (tmp["histEdge"] > THRESHOLD)
        tmp = tmp[mask].copy()
        tmp["_edge"] = tmp["histEdge"]
    else:
        tmp = tmp[tmp[edgeCol] > THRESHOLD].copy()
        tmp["_edge"] = tmp[edgeCol]

    if tmp.empty:
        return pd.DataFrame()

    records = []
    for _, row in tmp.sort_values("targetDate").iterrows():
        pnl = calcPnl(row, "yes")
        records.append({
            "date": row["targetDate"],
            "targetPrice": row["targetPrice"],
            "direction": "yes",
            "edge": row["_edge"],
            "yesPrice": row["yesPrice"],
            "won": row["yesWon"],
            "pnl": pnl,
        })

    result = pd.DataFrame(records)
    result["date"] = pd.to_datetime(result["date"])
    result = result.sort_values("date").reset_index(drop=True)
    result["cumPnl"] = result["pnl"].cumsum()
    result["capital"] = INITIAL_CAPITAL + result["cumPnl"]
    return result


def plot(df):
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(
        f"BTC Polymarket Backtest (Yes Only)\n"
        f"Rule: edge > {THRESHOLD*100:.0f}%, bet ${BET_SIZE} each, capital ${INITIAL_CAPITAL}",
        fontsize=13
    )

    strategies = [
        ("Gaussian", "gaussEdge", "steelblue"),
        ("Historical", "histEdge",  "darkorange"),
        ("Gauss+Hist", "bothEdge", "green"),
    ]

    # 上排：各自的累積損益
    for i, (name, col, color) in enumerate(strategies):
        ax = axes[0][i] if i < 2 else axes[1][0]
        result = runStrategy(df, col)
        if result.empty:
            ax.text(0.5, 0.5, "No signal", ha="center", va="center", transform=ax.transAxes)
            ax.set_title(name)
            continue

        wins = result["won"].sum()
        total = len(result)
        finalPnl = result["cumPnl"].iloc[-1]
        maxDD = (result["cumPnl"] - result["cumPnl"].cummax()).min()

        ax.plot(result["date"], result["cumPnl"], color=color, linewidth=2)
        ax.fill_between(result["date"], result["cumPnl"], 0,
                        where=result["cumPnl"] >= 0, alpha=0.15, color="green")
        ax.fill_between(result["date"], result["cumPnl"], 0,
                        where=result["cumPnl"] < 0, alpha=0.15, color="red")
        ax.axhline(0, color="black", linestyle="--", alpha=0.4)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30)
        ax.set_title(
            f"{name}\n"
            f"Bets: {total}  WR: {wins/total:.1%}  "
            f"PnL: ${finalPnl:+.2f}  MaxDD: ${maxDD:.2f}"
        )
        ax.set_ylabel("Cumulative PnL $")

    # 右下：三條曲線合併比較
    ax = axes[1][1]
    ax.axhline(0, color="black", linestyle="--", alpha=0.4)
    for name, col, color in strategies:
        result = runStrategy(df, col)
        if not result.empty:
            ax.plot(result["date"], result["cumPnl"], color=color,
                    linewidth=2, label=f"{name} ({len(result)} bets)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30)
    ax.set_title("All Strategies")
    ax.set_ylabel("Cumulative PnL $")
    ax.legend()

    plt.tight_layout()
    plt.savefig("performance.png", dpi=150, bbox_inches="tight")
    print("圖表已存至 performance.png")
    plt.close(fig)


def printSummary(df):
    print(f"\n{'='*60}")
    print(f"  績效摘要  門檻={THRESHOLD*100:.0f}%  每注=${BET_SIZE}  初始資金=${INITIAL_CAPITAL}")
    print(f"{'='*60}")
    print(f"  {'策略':<12}  {'下注':>5}  {'勝率':>7}  {'總損益':>9}  {'最大回撤':>9}")
    print("  " + "-" * 50)

    for name, col, _ in [
        ("高斯法",   "gaussEdge", None),
        ("歷史法",   "histEdge",  None),
        ("高斯+歷史", "bothEdge",  None),
    ]:
        result = runStrategy(df, col)
        if result.empty:
            print(f"  {name:<12}  {'無信號':>5}")
            continue
        wins = result["won"].sum()
        total = len(result)
        finalPnl = result["cumPnl"].iloc[-1]
        maxDD = (result["cumPnl"] - result["cumPnl"].cummax()).min()
        print(f"  {name:<12}  {total:>5}  {wins/total:>6.1%}  ${finalPnl:>+8.2f}  ${maxDD:>8.2f}")
    print()


if __name__ == "__main__":
    df = pd.read_csv("backtest_results.csv")
    df["targetDate"] = pd.to_datetime(df["targetDate"])
    df["yesWon"] = df["yesWon"].astype(bool)
    printSummary(df)
    plot(df)
