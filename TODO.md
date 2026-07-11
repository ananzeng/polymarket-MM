# Backlog

## WebSocket 即時成交推播（取代輪詢偵測）

現況：`_runLayered()` 每秒輪詢 REST `get_open_orders()` 推算成交（延遲 1 秒起跳 + 代理往返，且持續消耗代理流量）。

改造方向：接 Polymarket CLOB WebSocket 的 `user` channel，成交事件由交易所即時推送（毫秒級），收到 fill 事件才觸發撤退。

- 端點文件：https://docs.polymarket.com/developers/CLOB/websocket/wss-overview
- `py_clob_client_v2` 沒有內建 WS 支援，需自行用 `websockets` 套件實作（venv 已安裝）
- 需處理：API 憑證認證、心跳保活、斷線重連
- 輪詢可保留為降頻備援（例如 10 秒一次），WS 斷線期間保底
