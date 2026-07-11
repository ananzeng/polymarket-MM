"""
Parse a Polymarket market URL and print the .env values (MARKET_SLUG / MARKET_MATCH)
plus a reward-eligibility check, so you know whether the market is worth farming.

Uses only the public Gamma API (no wallet / proxy needed).

Run: venv/bin/python market_info.py <polymarket-url>
"""
import argparse
import re

from polymarket_data import fetchEventBySlug


def parseArgs():
    parser = argparse.ArgumentParser(description="Polymarket URL -> .env market config + reward check")
    parser.add_argument("url", help="Polymarket market/event URL")
    return parser.parse_args()


def parseUrl(url: str):
    """.../event/<eventSlug>[/<marketSlug>] -> (eventSlug, marketSlug or None)."""
    m = re.search(r"/event/([^/?#]+)(?:/([^/?#]+))?", url)
    if not m:
        raise ValueError("Not a Polymarket /event/ URL")
    return m.group(1), m.group(2)


def suggestMatch(target: dict, markets: list) -> str:
    """Pick the shortest keyword that uniquely identifies the target market's question."""
    question = target.get("question") or ""
    targetWords = re.findall(r"[A-Za-z0-9]+", question)
    otherText = " ".join((m.get("question") or "").lower() for m in markets if m is not target)
    unique = [w for w in targetWords if len(w) >= 3 and w.lower() not in otherText]
    if unique:
        return max(unique, key=len)  # e.g. "France"
    return question  # fall back to the full (always-unique) question


def rewardInfo(m: dict) -> dict:
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    dailyRate = 0.0
    for r in (m.get("clobRewards") or []):
        dailyRate = max(dailyRate, num(r.get("rewardsDailyRate")))

    bid, ask = num(m.get("bestBid")), num(m.get("bestAsk"))
    mid = (bid + ask) / 2 if bid and ask else num(m.get("lastTradePrice"))

    return {
        "maxSpread": num(m.get("rewardsMaxSpread")),
        "minSize": num(m.get("rewardsMinSize")),
        "dailyRate": dailyRate,
        "tick": num(m.get("orderPriceMinTickSize")),
        "mid": mid,
        "enabled": bool(m.get("enableOrderBook")) and bool(m.get("active")) and not m.get("closed"),
        "negRisk": bool(m.get("negRiskMarketID")),
    }


def main():
    args = parseArgs()
    eventSlug, marketSlug = parseUrl(args.url)

    event = fetchEventBySlug(eventSlug)
    if not event:
        print(f"❌ Event not found for slug: {eventSlug}")
        return

    markets = event.get("markets", [])
    target = None
    if marketSlug:
        target = next((m for m in markets if m.get("slug") == marketSlug), None)
    if not target and len(markets) == 1:
        target = markets[0]
    if not target:
        print(f"❌ Could not locate the market in event '{eventSlug}'. Available markets:")
        for m in markets[:20]:
            print(f"   - {m.get('slug')}  ({m.get('question')})")
        return

    match = suggestMatch(target, markets)
    info = rewardInfo(target)

    print(f"Market : {target.get('question')}")
    print(f"Event  : {event.get('title')}  (negRisk={event.get('negRisk')})\n")

    print("=== paste into .env ===")
    print(f"MARKET_SLUG={eventSlug}")
    print(f"MARKET_MATCH={match}")

    print("\n=== reward check ===")
    rewarded = info["enabled"] and info["maxSpread"] > 0 and info["minSize"] > 0 and info["dailyRate"] > 0
    print(f"{'✅ reward-enabled' if rewarded else '❌ NOT reward-enabled'}")
    print(f"  daily reward pool : ${info['dailyRate']:,.0f}")
    print(f"  max spread        : {info['maxSpread']}¢   (OFFSET_CENTS must be <= this)")
    print(f"  min size          : {info['minSize']:.0f}   (ORDER_SIZE must be >= this)")
    print(f"  tick              : {info['tick']}")
    print(f"  midpoint          : {info['mid']:.4f}" + ("" if 0.10 <= info["mid"] <= 0.90
          else "  ⚠️ outside [0.10,0.90]: single-sided won't score, needs two-sided"))
    print(f"  negRisk           : {info['negRisk']}   (bot reads this from the event automatically)")

    if rewarded:
        print("\n=== suggested settings ===")
        print(f"  ORDER_SIZE={max(200, int(info['minSize']))}   OFFSET_CENTS≈2.5 (<= {info['maxSpread']})")


if __name__ == "__main__":
    main()
