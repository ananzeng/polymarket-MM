"""
Real-time order-book feed over Polymarket's market WebSocket channel.

Maintains a thread-safe per-token order book (bids/asks with sizes) fed by pushed
`book` snapshots and `price_change` deltas, so the bait-layer loop can watch the whole
market's real depth and a live mid without RTT-bound REST polling. Unlike the user
channel (ws_fills.FillFeed) this needs NO auth — the market channel is public.

HONEST CEILING: a single atomic sweep that consumes bait + main in one match event is
still unbeatable here — the market `price_change`/`trade` arrives AFTER the match, exactly
like a fill notification. This layer only defends against telegraphed pressure (depth being
pulled, staged sweeps, mid drift); it lowers probability and loss, not to zero.

The connection routes through POLYMARKET_PROXY explicitly (the py-clob-client httpx proxy
shim does NOT cover a raw WebSocket socket), mirroring ws_fills.FillFeed.
"""
import json
import logging
import threading
import time
from urllib.parse import urlparse

import websocket

logger = logging.getLogger(__name__)

WS_URL_DEFAULT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_BACKOFF = 30.0
PING_INTERVAL = 10.0  # Polymarket closes idle connections; keep alive with an app-level PING


class BookFeed:
    def __init__(self, assetIds, proxyUrl: str = None,
                 stopEvent: threading.Event = None, wsUrl: str = WS_URL_DEFAULT):
        self.assetIds = list(assetIds)
        self.wsUrl = wsUrl
        self.stopEvent = stopEvent or threading.Event()

        self._proxyHost = None
        self._proxyPort = None
        self._proxyAuth = None
        if proxyUrl:
            p = urlparse(proxyUrl)
            self._proxyHost = p.hostname
            self._proxyPort = p.port
            self._proxyAuth = (p.username, p.password) if p.username else None

        self._lock = threading.Lock()
        # assetId -> {"bids": {priceFloat: sizeFloat}, "asks": {...}, "hasSnap": bool, "ts": float}
        self._books = {aid: {"bids": {}, "asks": {}, "hasSnap": False, "ts": 0.0}
                       for aid in self.assetIds}
        self._thread = None
        self._keepaliveThread = None
        self._ws = None
        self.connected = False
        self.lastEventTs = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="BookFeed")
        self._thread.start()
        self._keepaliveThread = threading.Thread(target=self._keepalive, daemon=True, name="BookFeedPing")
        self._keepaliveThread.start()

    def stop(self):
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=5)

    # ----- connection / supervision (mirrors ws_fills.FillFeed) -----

    def _run(self):
        backoff = 1.0
        while not self.stopEvent.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self.wsUrl,
                    on_open=self._onOpen,
                    on_message=self._onMessage,
                    on_error=self._onError,
                    on_close=self._onClose,
                )
                self._ws.run_forever(
                    http_proxy_host=self._proxyHost,
                    http_proxy_port=self._proxyPort,
                    http_proxy_auth=self._proxyAuth,
                    proxy_type="http",
                    ping_interval=10,
                    ping_timeout=8,
                    reconnect=0,          # we own reconnection so stopEvent stays responsive
                )
            except Exception as e:
                logger.error("BookFeed run_forever error: %s", e)
            self.connected = False
            self._invalidateSnapshots()   # never apply pre-disconnect deltas onto a fresh snapshot
            if self.stopEvent.is_set():
                break
            wait = min(backoff, MAX_BACKOFF)
            logger.info("BookFeed disconnected, reconnecting in %.0fs", wait)
            self.stopEvent.wait(wait)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _keepalive(self):
        while not self.stopEvent.is_set():
            if self.stopEvent.wait(PING_INTERVAL):
                break
            if self.connected and self._ws:
                try:
                    self._ws.send("PING")
                except Exception:
                    pass

    def _onOpen(self, ws):
        # Market channel is public: subscribe by asset ids, no auth block. The exact envelope
        # (assets_ids key, "market" type) is confirmed empirically with test_market_ws.py before
        # this feed is ever wired into the live bot.
        frame = {"assets_ids": self.assetIds, "type": "market"}
        ws.send(json.dumps(frame))
        self.connected = True
        logger.info("BookFeed connected + subscribed (%d assets)", len(self.assetIds))

    def _onMessage(self, ws, raw):
        recv = time.time()
        self.lastEventTs = recv
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("BookFeed non-JSON message: %s", str(raw)[:200])
            return
        for m in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(m, dict):
                continue
            eventType = m.get("event_type")
            if eventType == "book":
                self._applySnapshot(m)
            elif eventType == "price_change":
                self._applyPriceChange(m)

    def _applySnapshot(self, m: dict):
        """`book` event: full replacement of one token's resting orders."""
        assetId = m.get("asset_id")
        if assetId not in self._books:
            return
        bids = {float(b["price"]): float(b["size"]) for b in (m.get("bids") or [])}
        asks = {float(a["price"]): float(a["size"]) for a in (m.get("asks") or [])}
        with self._lock:
            book = self._books[assetId]
            book["bids"] = bids
            book["asks"] = asks
            book["hasSnap"] = True
            book["ts"] = time.time()

    def _applyPriceChange(self, m: dict):
        """`price_change` event: a batch of per-level upserts under `price_changes`. A single
        frame may carry entries for BOTH tokens, so asset_id lives on each entry (not the top
        level). `size` is the new ABSOLUTE level size (0 = level removed). Entries for a token
        whose snapshot we have not seen yet are dropped, else they would corrupt a book we lack
        the base of. (Schema confirmed live via test_market_ws.py: array key `price_changes`,
        per-entry asset_id, side BUY/SELL, entries also carry best_bid/best_ask.)"""
        changes = m.get("price_changes") or []
        with self._lock:
            for ch in changes:
                book = self._books.get(ch.get("asset_id"))
                if not book or not book["hasSnap"]:
                    continue
                side = str(ch.get("side", "")).upper()
                price = float(ch["price"])
                size = float(ch["size"])
                sideBook = book["bids"] if side == "BUY" else book["asks"]
                if size <= 0:
                    sideBook.pop(price, None)
                else:
                    sideBook[price] = size
                book["ts"] = time.time()

    def _invalidateSnapshots(self):
        with self._lock:
            for book in self._books.values():
                book["hasSnap"] = False

    def _onError(self, ws, err):
        logger.error("BookFeed error: %s", err)

    def _onClose(self, ws, code, msg):
        self.connected = False
        logger.info("BookFeed closed code=%s msg=%s", code, msg)

    # ----- thread-safe queries (aggregate inside the lock, return plain values) -----

    def hasSnapshot(self, assetId) -> bool:
        with self._lock:
            book = self._books.get(assetId)
            return bool(book and book["hasSnap"])

    def bestBid(self, assetId):
        with self._lock:
            book = self._books.get(assetId)
            if not book or not book["hasSnap"] or not book["bids"]:
                return None
            return max(book["bids"])

    def bestAsk(self, assetId):
        with self._lock:
            book = self._books.get(assetId)
            if not book or not book["hasSnap"] or not book["asks"]:
                return None
            return min(book["asks"])

    def mid(self, assetId):
        with self._lock:
            book = self._books.get(assetId)
            if not book or not book["hasSnap"] or not book["bids"] or not book["asks"]:
                return None
            return (max(book["bids"]) + min(book["asks"])) / 2

    def spreadCents(self, assetId):
        with self._lock:
            book = self._books.get(assetId)
            if not book or not book["hasSnap"] or not book["bids"] or not book["asks"]:
                return None
            return (min(book["asks"]) - max(book["bids"])) * 100

    def buyDepthAboveExcl(self, assetId, price: float):
        """Total buy-order size at price levels STRICTLY above `price` — the cushion a seller
        must eat through before reaching an order resting at `price`. Returns None when there
        is no live snapshot (callers must treat None as 'signal unavailable', not zero depth)."""
        with self._lock:
            book = self._books.get(assetId)
            if not book or not book["hasSnap"]:
                return None
            return sum(sz for px, sz in book["bids"].items() if px > price)
