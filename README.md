# polymarket-MM

Polymarket 流動性獎勵（LP）動態掛單機器人。在指定子市場的中間價附近**雙邊掛限價單**（買 Yes + 買 No）賺取每日流動性獎勵，並定時全撤重掛以降低被吃單機率；偵測到成交就市價平倉、進入冷卻，儘量不留倉。

## 運作原理

### 獎勵規則
Polymarket 對「在點差內掛單」的 maker 發放每日獎勵：

- 計分公式 **S(v, s) = ((v − s) / v)² · b**，`v` = 市場 `max spread`、`s` = 掛單距中間價的點差。**越貼中間價，分數呈二次成長**。
- 只有份額 **≥ `rewardsMinSize`** 的掛單才計分。
- 中間價落在 [0.10, 0.90] 時單邊也計分但分數 ÷ 3；**雙邊掛單分數最高**。
- 每分鐘抽樣、每日 UTC 午夜結算，依當日分數占比分配該市場獎勵池（單日 < $1 不發放）。

### 免持倉雙邊結構
- 買 Yes @ `(mid − offset)` + 買 No @ `((1 − mid) − offset)`
- 買 No 等同賣 Yes（計分算另一邊），兩單各在自己的 order book，`bidYes + bidNo = 1 − 2·offset < 1`，**永不互相成交**。
- `post_only` 保證只當 maker（會穿價則被拒）。

### 主迴圈（`lp_maker.py`）
1. 偵測庫存（Yes / No 條件代幣餘額）；若被吃出部位 → 市價平倉（FAK）→ 進入冷卻（點差加倍）。
2. 否則：全撤該市場掛單 → 取中間價 → 掛買 Yes / 買 No（`post_only`）。
3. 每 `REFRESH_INTERVAL` 秒重複。

## 設定（全部從 `.env` 讀取）

| Key | 預設 | 說明 |
|-----|------|------|
| `MARKET_SLUG` | `nba-lebron-james-next-team` | 目標事件 slug |
| `MARKET_MATCH` | `Cleveland Cavaliers` | 用來在事件內定位子市場的 question 關鍵字 |
| `OFFSET_CENTS` | `1.0` | 掛單距中間價的點差（¢）。越小獎勵越高但越易被吃，需 ≤ 市場 max spread |
| `ORDER_SIZE` | `200` | 每邊掛單股數，需 ≥ 市場 `rewardsMinSize` 才計獎勵 |
| `REFRESH_INTERVAL` | `15` | 全撤重掛間隔（秒） |
| `COOLDOWN` | `60` | 被吃單後的冷卻秒數（期間點差加倍） |
| `DRY_RUN` | `true` | `true` 只計算列印、不下單，執行一次後結束 |
| `LOG_DIR` | `log` | log 存放資料夾（同時輸出 console 與 `LOG_DIR/YYYY-MM-DD.txt`） |
| `LOG_LEVEL` | `INFO` | log 等級（DEBUG / INFO / WARNING / ERROR） |
| `ALERT_SOUND` | `/System/Library/Sounds/Glass.aiff` | 成交時播放的提示音（macOS afplay），留空則關閉 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | 成交/平倉時發 Telegram 通知，留空則不通知 |
| `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_FUNDER` / `POLYMARKET_PROXY` | — | 錢包與代理設定 |

## 專案結構

```
├── lp_maker.py          # LP 動態掛單機器人（主程式）
├── notifier.py          # Telegram 成交/平倉通知
├── polymarket_data.py   # Polymarket Gamma / CLOB API 資料抓取
├── approve.py           # 一次性：approve USDC + CTF 給 Polymarket 合約
├── test_order.py        # 下單環境測試腳本
└── requirements.txt
```

## 安裝

```bash
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt

# .env 已含錢包設定；首次需授權（只做一次）
venv/bin/python approve.py
```

## 使用

```bash
# 1. 環境檢查（ClobClient 初始化 + 餘額 / allowance）
venv/bin/python test_order.py

# 2. 乾跑（.env 設 DRY_RUN=true）：印出鎖定的市場、中間價、雙邊掛單價，不下單
venv/bin/python lp_maker.py

# 3. 實跑（.env 設 DRY_RUN=false）：開始動態雙邊掛單，Ctrl+C 停止並清空掛單
venv/bin/python lp_maker.py
```

## Proxy（必要）
Polymarket **交易**對部分地區（含台灣）做地區封鎖，讀取／認證不封。因此下單一定要透過 `POLYMARKET_PROXY`，且該 proxy 的**出口需在允許地區**（如西班牙）。程式啟動時會印出對外出口 IP／國家，若 proxy 不通會直接報錯。CLOB 於 2026-04 升級到 V2，本專案使用 `py-clob-client-v2`（舊版 `py-clob-client` 產生的訂單會被拒）。

## 風險提醒
- `OFFSET_CENTS` 越小獎勵越高但越易被吃；15 秒重掛僅降低、無法消除被吃機率。
- 雙邊皆成交＝持有 Yes + No 的鎖定 $1 對（無方向風險），平倉會付兩次點差。
- 平倉走 taker 會付點差成本；長期淨損益 = 獎勵 − 被吃平倉的點差成本，需自行觀察是否為正。

## 資料來源
- 市場資料：Polymarket Gamma API（`gamma-api.polymarket.com/events`）
- 下單 / 撤單 / 取價：Polymarket CLOB V2（`clob.polymarket.com`，透過 `py-clob-client-v2`）
