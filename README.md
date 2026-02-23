# 🪐 Galactic Trader

Autonomous paper trading agent with a real-time web dashboard.

**Stack:** Python · FastAPI · Alpaca Paper API · SQLite · Chart.js

---

## What it does

1. Connects to **Alpaca Paper Trading** (free — real market data, fake money)
2. Every **5 minutes**, fetches live price bars for your chosen symbols
3. Computes **EMA-9, EMA-21, RSI-14, MACD, ATR, Bollinger Bands** indicators
4. Runs the active strategy (BBRSI / DHLAO / 3MACD) for buy/sell/hold decisions
5. Executes trades on paper with ATR-based stop-loss and take-profit
6. Logs everything to **SQLite** — trades, decisions, P&L snapshots
7. Serves a **live dashboard** at `http://localhost:8080`

---

## Quick Start

### 1. Get a free Alpaca Paper Trading account
→ [alpaca.markets](https://alpaca.markets) — create account → Paper Trading → API Keys

### 2. Clone & run

**macOS / Linux:**
```bash
git clone https://github.com/abhaymundhara/galactic-trader.git
cd galactic-trader
chmod +x launch.sh
./launch.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/abhaymundhara/galactic-trader.git
cd galactic-trader
.\launch.ps1
```

Edit `.env` with your Alpaca keys and preferred symbols, then run again.

### 4. Open the dashboard
→ `http://localhost:8080`

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `ALPACA_API_KEY` | — | Your Alpaca paper API key |
| `ALPACA_SECRET_KEY` | — | Your Alpaca paper secret |
| `SYMBOLS` | `AAPL,MSFT,NVDA,TSLA,AMZN` | Comma-separated tickers |
| `STARTING_CAPITAL` | `10000` | Starting paper portfolio ($) |
| `MAX_POSITION_SIZE` | `0.10` | Max 10% per position |
| `PORT` | `8080` | Dashboard port |

---

## Dashboard

| Section | Shows |
|---|---|
| Stats row | Total value, cash, positions value, experiment week (1-6) |
| P&L chart | Portfolio value over time |
| Decisions | Latest strategy signal per symbol (action, confidence, reasoning) |
| Open Positions | Live positions with unrealised P&L |
| Trade Log | Every executed paper trade |

---

## Project Structure

```
galactic-trader/
├── main.py          # FastAPI app + WebSocket
├── agent.py         # Trading loop + strategy engine
├── strategy_engine.py # Strategy orchestration (BBRSI/DHLAO/3MACD)
├── database.py      # SQLite layer
├── dashboard.html   # Single-file dashboard UI
├── requirements.txt
├── .env.example
├── launch.sh        # macOS/Linux launcher
└── launch.ps1       # Windows launcher
```

---

## License

MIT
