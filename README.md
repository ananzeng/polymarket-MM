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
| `REFRESH_INTERVAL` | `15` | 主單掛滿多久重掛（秒）。**訂單要掛 ≥ ~25 秒才會被 Polymarket 抽樣計分**，太短賺不到獎勵 |
| `COOLDOWN` | `60` | 被吃單後的冷卻秒數（期間點差加倍） |
| `DRY_RUN` | `true` | `true` 只計算列印、不下單，執行一次後結束 |
| `LOG_DIR` | `log` | log 存放資料夾（同時輸出 console 與 `LOG_DIR/YYYY-MM-DD.txt`） |
| `LOG_LEVEL` | `INFO` | log 等級（DEBUG / INFO / WARNING / ERROR） |
| `ALERT_SOUND` | `/System/Library/Sounds/Glass.aiff` | 成交時播放的提示音（macOS afplay），留空則關閉 |
| `BAIT_ENABLED` | `false` | 啟用誘餌分層模式（見下） |
| `BAIT_OFFSET_CENTS` | `0.4` | 誘餌距中間價點差（貼盤口，比主單更前面才會先被吃） |
| `BAIT_SIZE` | `15` | 誘餌股數（< `rewardsMinSize` 不計獎勵，純預警） |
| `BAIT_TRIGGER_RATIO` | `0.5` | 誘餌被吃到此比例（或主單被吃）就撤主單、平倉、冷卻 |
| `POLL_INTERVAL` | `1.0` | 分層模式下查各單成交量的間隔（秒） |
| `SCORING_CHECK_INTERVAL` | `20` | 每隔幾秒查主單是否正在計分（賺獎勵）並 log，`0` 關閉 |
| `DASHBOARD_PORT` | `8000` | dashboard.py 網頁埠號（http://localhost:8000） |
| `REWARD_RATE_LOG_INTERVAL` | `300` | 每隔幾秒把 reward rate 快照寫進 `log/reward_rate.csv`（預設 5 分鐘） |
| `CHART_WINDOW_HOURS` | `12` | 網頁圖表顯示的滾動時間視窗（小時） |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | 成交/平倉時發 Telegram 通知，留空則不通知 |
| `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_FUNDER` / `POLYMARKET_PROXY` | — | 錢包與代理設定 |

### 誘餌分層模式（`BAIT_ENABLED=true`）
為了賺 LP 獎勵，主單必須掛得夠久（≥ ~25 秒）才會被抽樣計分——但掛久了容易被吃。此模式在盤口最前面放一張小「誘餌單」當預警：
- **主單**（`OFFSET_CENTS`，如 2.5¢、size ≥ 200）：掛滿 `REFRESH_INTERVAL`（如 45s）賺獎勵。
- **誘餌**（`BAIT_OFFSET_CENTS`，如 0.4¢、size 小）：貼盤口，賣單先撞它。
- 每 `POLL_INTERVAL` 秒查各單成交量；當誘餌被吃 ≥ `BAIT_TRIGGER_RATIO`（或主單被吃）→ 撤主單 → 平掉庫存 → 冷卻 → 重掛。
- 限制：擋不住「一次大單同時掃穿誘餌+主單」（同一撮合事件，來不及反應）；只擋小單/連續流/緩慢漂移。

## 專案結構

```
├── lp_maker.py          # LP 動態掛單機器人（主程式）
├── dashboard.py         # 網頁儀表板：跑 bot + 即時 PnL/獎勵速率/計分 + Start/Stop
├── notifier.py          # Telegram 成交/平倉通知
├── market_info.py       # 貼網址 → 輸出 .env 的 MARKET_SLUG/MARKET_MATCH 並檢查獎勵
├── pnl.py               # 從成交+獎勵算 PnL 並畫 3 條折線（log/pnl.png）
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
# 0. 換市場：貼 Polymarket 網址 → 得到 .env 的 MARKET_SLUG/MARKET_MATCH 並確認有獎勵
venv/bin/python market_info.py "https://polymarket.com/event/.../..."

# 1. 環境檢查（ClobClient 初始化 + 餘額 / allowance）
venv/bin/python test_order.py

# 2. 乾跑（.env 設 DRY_RUN=true）：印出鎖定的市場、中間價、雙邊掛單價，不下單
venv/bin/python lp_maker.py

# 3. 實跑（.env 設 DRY_RUN=false）：開始動態雙邊掛單，Ctrl+C 停止並清空掛單
venv/bin/python lp_maker.py

# 4. 查 PnL：從成交+獎勵算交易 PnL / LP 獎勵 / 總和，畫成 log/pnl.png
venv/bin/python pnl.py

# 5. 儀表板（建議）：一個指令同時跑 bot + 網頁，瀏覽器開 http://localhost:8000
#    即時顯示獎勵速率($/5min，市場 CP)、今日 PnL、計分狀態，附 Start/Stop 按鈕
venv/bin/python dashboard.py
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
