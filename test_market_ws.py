"""
Standalone probe for the Polymarket MARKET WebSocket channel. Connects through the proxy
(no auth — the market channel is public) and by default prints every raw frame, so you can
confirm the exact subscribe envelope the server accepts and the `book`/`price_change` schema
(field names/casing, whether price_change `size` is absolute or a delta) BEFORE the parsing
in book_feed.py is trusted or wired into lp_maker. Places NO orders.

Raw schema probe   : venv/bin/python test_market_ws.py --seconds 60
Derived-value check: venv/bin/python test_market_ws.py --seconds 30 --derive

--derive stops printing raw frames and instead prints the aggregated queries
(mid/bestBid/bestAsk/spread/cushion) once a second, so you can eyeball them against the
website order book and confirm the aggregation is correct.
"""
import argparse
import json
import logging
import os
import threading
import time

import lp_maker as lm
from book_feed import BookFeed


def parse_args():
    parser = argparse.ArgumentParser(description="Probe the Polymarket market WebSocket (no orders placed)")
    parser.add_argument("--seconds", type=int, default=60, help="how long to listen before exiting")
    parser.add_argument("--derive", action="store_true",
                        help="print aggregated mid/bestBid/bestAsk/cushion once a second instead of raw frames")
    parser.add_argument("--cushion-price", type=float, default=0.175,
                        help="Yes price to measure buyDepthAboveExcl against in --derive mode")
    return parser.parse_args()


def _f(v):
    return "–" if v is None else f"{v:.4f}"


def main(seconds: int, derive: bool, cushionPrice: float):
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "hpack", "urllib3", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    market = lm.resolveMarket()
    lm.initClobClient()   # applies the proxy; we don't need the client itself (market channel is public)
    yesToken, noToken = market["yesToken"], market["noToken"]
    print(f"market      : {market['question']}")
    print(f"yesToken    : {yesToken}")
    print(f"noToken     : {noToken}")
    print(f"proxy       : {os.getenv('POLYMARKET_PROXY', '(none)').split('@')[-1]}")

    stopEvent = threading.Event()
    marketWsUrl = os.getenv("MARKET_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    feed = BookFeed([yesToken, noToken], os.getenv("POLYMARKET_PROXY"), stopEvent, marketWsUrl)

    if not derive:
        def rawPrint(ws, raw):
            print(f"\n--- {time.strftime('%H:%M:%S')} frame ---")
            try:
                print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
            except (ValueError, TypeError):
                print(raw)
        feed._onMessage = rawPrint

    feed.start()

    if derive:
        print(f"\nderiving for {seconds}s (mid/bestBid/bestAsk/spread/cushion@{cushionPrice})…\n")
        end = time.time() + seconds
        while time.time() < end and not stopEvent.is_set():
            time.sleep(1)
            print(f"{time.strftime('%H:%M:%S')} conn={feed.connected} "
                  f"snapY={feed.hasSnapshot(yesToken)} snapN={feed.hasSnapshot(noToken)} "
                  f"| Yes mid={_f(feed.mid(yesToken))} bid={_f(feed.bestBid(yesToken))} "
                  f"ask={_f(feed.bestAsk(yesToken))} spread={_f(feed.spreadCents(yesToken))}c "
                  f"cushion>{cushionPrice}={_f(feed.buyDepthAboveExcl(yesToken, cushionPrice))} "
                  f"| No mid={_f(feed.mid(noToken))}")
    else:
        print(f"\nlistening for {seconds}s… (raw frames)\n")
        stopEvent.wait(seconds)

    feed.stop()
    print("\ndone")


if __name__ == "__main__":
    args = parse_args()
    main(args.seconds, args.derive, args.cushion_price)
