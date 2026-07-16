"""
Real-time fill feed over Polymarket's user WebSocket channel.

Maintains a thread-safe {orderId: cumulativeMatchedShares} table fed by pushed
`order`/`trade` events, so the bait-layer loop can detect fills without RTT-bound
REST polling. The connection routes through POLYMARKET_PROXY explicitly (the
py-clob-client httpx proxy shim does NOT cover a raw WebSocket socket).
"""
import json
import logging
import threading
import time
from urllib.parse import urlparse

import websocket

logger = logging.getLogger(__name__)

WS_URL_DEFAULT = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
MAX_BACKOFF = 30.0
PING_INTERVAL = 10.0  # Polymarket closes idle user connections; keep alive with an app-level PING


class FillFeed:
    def __init__(self, creds, conditionId: str, proxyUrl: str = None,
                 stopEvent: threading.Event = None, wsUrl: str = WS_URL_DEFAULT):
        self.creds = creds
        self.conditionId = conditionId
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
        self._filled = {}        # orderId -> cumulative matched shares (absolute)
        self._lastFillTs = {}    # orderId -> local recv time of last fill
        self._seenTrades = set()
        self._thread = None
        self._keepaliveThread = None
        self._ws = None
        self.connected = False
        self.lastEventTs = 0.0
        self._authFailed = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="FillFeed")
        self._thread.start()
        self._keepaliveThread = threading.Thread(target=self._keepalive, daemon=True, name="FillFeedPing")
        self._keepaliveThread.start()

    def stop(self):
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=5)

    def setWatched(self, orderIds):
        """Scope the table to the current cycle's orders so stale fills can't trip."""
        ids = set(orderIds)
        with self._lock:
            self._filled = {oid: v for oid, v in self._filled.items() if oid in ids}
            self._lastFillTs = {oid: v for oid, v in self._lastFillTs.items() if oid in ids}
            self._seenTrades.clear()

    def snapshot(self, orderIds) -> dict:
        with self._lock:
            return {oid: self._filled.get(oid, 0.0) for oid in orderIds}

    def reconcile(self, restMap: dict):
        """Merge REST-observed fills; REST can only top up what WS missed (max merge)."""
        with self._lock:
            for oid, val in restMap.items():
                have = self._filled.get(oid, 0.0)
                if val > have:
                    logger.debug("Reconcile: REST oid=%s filled=%.2f > WS %.2f (WS gap)", oid, val, have)
                    self._filled[oid] = val

    def _run(self):
        backoff = 1.0
        while not self.stopEvent.is_set():
            self._authFailed = False
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
                logger.error("WS run_forever error: %s", e)
            self.connected = False
            if self.stopEvent.is_set():
                break
            wait = MAX_BACKOFF if self._authFailed else min(backoff, MAX_BACKOFF)
            logger.info("WS disconnected, reconnecting in %.0fs", wait)
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
        frame = {
            "type": "user",
            "markets": [self.conditionId],
            "auth": {
                "apiKey": self.creds.api_key,
                "secret": self.creds.api_secret,
                "passphrase": self.creds.api_passphrase,
            },
        }
        ws.send(json.dumps(frame))
        self.connected = True
        logger.info("WS connected + subscribed (market=%s)", self.conditionId)

    def _onMessage(self, ws, raw):
        recv = time.time()
        self.lastEventTs = recv
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("WS non-JSON message: %s", str(raw)[:200])
            return
        for m in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(m, dict):
                continue
            eventType = m.get("event_type")
            if eventType == "order":
                self._handleOrder(m, recv)
            elif eventType == "trade":
                self._handleTrade(m, recv)

    def _handleOrder(self, m: dict, recv: float):
        oid = m.get("id")
        if not oid:
            return
        sizeMatched = float(m.get("size_matched", 0) or 0)
        with self._lock:
            if sizeMatched > self._filled.get(oid, 0.0):
                self._filled[oid] = sizeMatched
                self._lastFillTs[oid] = recv
        srv = m.get("timestamp")
        if srv:
            try:
                logger.debug("WS order oid=%s size_matched=%.2f e2e=%.1fms",
                             oid, sizeMatched, recv * 1000 - float(srv))
            except (TypeError, ValueError):
                pass

    def _handleTrade(self, m: dict, recv: float):
        """Latency observation only; size_matched from `order` events is authoritative,
        so we don't accumulate here to avoid double-counting."""
        tid = m.get("id")
        if not tid:
            return
        with self._lock:
            if tid in self._seenTrades:
                return
            self._seenTrades.add(tid)
        logger.debug("WS trade id=%s status=%s recv=%.0f", tid, m.get("status"), recv * 1000)

    def _onError(self, ws, err):
        logger.error("WS error: %s", err)

    def _onClose(self, ws, code, msg):
        self.connected = False
        # 1008 (policy violation) / 4001-style codes usually mean bad auth; slow the retry.
        if code in (1008, 4001, 4003):
            self._authFailed = True
        logger.info("WS closed code=%s msg=%s", code, msg)
