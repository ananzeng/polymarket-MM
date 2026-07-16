"""
Standalone probe for the Polymarket user WebSocket. Connects through the proxy with
the real API creds and prints every raw frame. Places NO orders — use it to confirm
the proxy/auth handshake works and to inspect the real message schema (event_type,
id, size_matched, timestamp) before enabling WS_ENABLED in lp_maker.

Run: venv/bin/python test_ws.py --seconds 60
Then, in another terminal, place + cancel one tiny order (or wait for an organic
fill) and watch the printed frames.
"""
import argparse
import json
import logging
import os
import threading
import time

import lp_maker as lm
from ws_fills import FillFeed


def parse_args():
    parser = argparse.ArgumentParser(description="Probe the Polymarket user WebSocket (no orders placed)")
    parser.add_argument("--seconds", type=int, default=60, help="how long to listen before exiting")
    return parser.parse_args()


def main(seconds: int):
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "hpack", "urllib3", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    market = lm.resolveMarket()
    client = lm.initClobClient()
    print(f"market      : {market['question']}")
    print(f"conditionId : {market['conditionId']}")
    print(f"api_key     : {client.creds.api_key[:8]}…")
    print(f"proxy       : {os.getenv('POLYMARKET_PROXY', '(none)').split('@')[-1]}")

    stopEvent = threading.Event()
    feed = FillFeed(client.creds, market["conditionId"], os.getenv("POLYMARKET_PROXY"), stopEvent)

    def rawPrint(ws, raw):
        print(f"\n--- {time.strftime('%H:%M:%S')} frame ---")
        try:
            print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
        except (ValueError, TypeError):
            print(raw)

    feed._onMessage = rawPrint
    feed.start()

    print(f"\nlistening for {seconds}s… (place+cancel a tiny order elsewhere to see events)\n")
    stopEvent.wait(seconds)
    feed.stop()
    print("\ndone")


if __name__ == "__main__":
    args = parse_args()
    main(args.seconds)
