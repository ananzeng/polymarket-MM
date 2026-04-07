# polymarket-MM

BTC/Polymarket arbitrage tool — uses historical volatility to estimate the probability of BTC reaching specific price levels, compares with Polymarket's "Bitcoin above $X?" market pricing, and bets when the model finds a pricing discrepancy.

## How It Works

### Core Concept

Polymarket runs daily binary markets like "Bitcoin above $74,000 on March 16?" with ~11 strike prices per day, spaced $2K apart. Settlement is based on Binance BTC/USDT 1-min candle **close** at **noon ET**.

This tool calculates the statistical probability of BTC reaching each strike, compares it with Polymarket's Yes price, and identifies mispriced markets.

### Probability Models

**Gaussian Method**
- Rolling std from last 20 hourly candle closes
- Projects std forward: `hourly_std × √(hours_remaining)`
- Uses normal CDF: `P(BTC > strike) = 1 - Φ(strike, current_price, projected_std)`

**Historical Method**
- Takes all hourly candles from the past 90 days
- For each candle, looks N hours forward (where N = hours until settlement)
- Maps those returns to current price
- Counts what percentage exceed the strike

### Signal Logic

```
edge = model_probability - polymarket_yes_price
```

If `histEdge > 8%` → Buy Yes (market is underpricing the probability).

## Backtest Results

**Date range**: 2026-03-07 ~ 2026-04-06 (30 days, 330 data points)

| Strategy | Trades | Win Rate | PnL (per $1 bet) |
|----------|--------|----------|-------------------|
| Gaussian (gaussEdge > 8%) | 14 | 14.3% | -$10.27 |
| Historical (histEdge > 8%) | 9 | 44.4% | -$3.75 |
| Both (gauss + hist > 8%) | 4 | 0.0% | -$4.00 |

> Note: Results vary significantly across different market regimes. The historical method shows better calibration than Gaussian due to BTC's leptokurtic (fat-tailed) distribution.

## Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `BOLLINGER_WINDOW` | 20 | Rolling std window (hourly candles) |
| `HIST_LOOKBACK` | 90 days | Historical sample period |
| `MIN_EDGE` | 8% | Minimum edge to trigger a bet |
| `SIGNAL_HOUR_UTC` | 9 (backtest default) | Signal check time |
| `TRADE_HOUR_UTC` | 11 | Auto-trade execution hour (best from hourly comparison) |
| `BET_SIZE` | $5 USDC | Minimum order size on Polymarket |
| `MAX_DAILY_BETS` | 1 | Daily bet limit |

## Project Structure

```
├── daily_signal.py      # Real-time monitor + auto-trading
├── backtest.py          # Historical backtesting engine
├── plotPerformance.py   # Performance chart generator
├── backtest.ipynb       # Interactive analysis notebook
├── bollinger_prob.py    # Probability calculation (Gaussian + Historical)
├── btc_data.py          # Binance BTC/USDT hourly klines
├── polymarket_data.py   # Polymarket Gamma + CLOB API
├── approve.py           # One-time USDC approval for Polymarket contracts
├── test_order.py        # Order placement test script
└── requirements.txt
```

## Setup

```bash
# Create venv (Python 3.9.10+)
python3.12 -m venv venv

# Install dependencies
venv/bin/pip install -r requirements.txt

# Configure .env
cat > .env << 'EOF'
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER=0x...
POLYMARKET_PROXY=http://user:pass@ip:port
EOF

# One-time: approve USDC for Polymarket contracts
venv/bin/python approve.py
```

## Usage

### Daily Signal (one-shot)
```bash
venv/bin/python daily_signal.py
```

### Monitor Mode (continuous, every 60s)
```bash
venv/bin/python daily_signal.py --monitor
```

### Auto-Trade Mode
```bash
venv/bin/python daily_signal.py --monitor --auto-trade
```
Only places orders during `TRADE_HOUR_UTC` (UTC 11 / Taiwan 19:00). Proxy is only used for order placement.

### Run Backtest
```bash
venv/bin/python backtest.py
```
Outputs `backtest_results.csv`, trade logs, and hourly comparison table.

### Generate Performance Chart
```bash
venv/bin/python plotPerformance.py
```

## Data Sources

- **BTC Price**: Binance API (`/api/v3/klines`, BTC/USDT 1h)
- **Market Data**: Polymarket Gamma API (`gamma-api.polymarket.com/events`)
- **Historical Pricing**: Polymarket CLOB API (`clob.polymarket.com/prices-history`)
