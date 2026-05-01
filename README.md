# NIFTY Smart Money Command Center

Render-ready Flask dashboard for live NIFTY 50 decision support using public NSE data.

The original app showed raw NSE panels: indices, FII/DII cash, option-chain OI, FII derivatives, and bulk/block deals. This version keeps the same simple Python setup, but adds a decision engine that converts those feeds into daily, weekly, and monthly NIFTY views with a final conclusion.

## What It Reads

| Area | Source |
|---|---|
| NIFTY, Bank Nifty, sector breadth, India VIX | NSE indices |
| FII / DII cash market flow | NSE FII/DII statistics |
| NIFTY option OI, OI change, IV, expiry walls | NSE option chain |
| Participant-wise F&O positioning | NSE participant OI archive |
| Bulk and block deal tape | NSE package methods |

## What It Produces

- Final action posture: `BUY BIAS`, `BUY DIPS`, `SELL RISES`, `SELL BIAS`, or `WAIT / RANGE`.
- Daily, weekly, and monthly scorecards based on separate NIFTY expiries.
- Put base, call wall, max pain, expected range, PCR, OI change percentage, and OI pressure.
- FII/DII cash interpretation and participant-wise FII, Pro, Client, and DII derivative tilt.
- Invalidation level and a plain-English hidden story behind the move.

## Run Locally

```bash
pip install -r requirements.txt
python app.py
```

Open:

```text
http://localhost:8000
```

`server.py` is kept as a compatibility runner, so this also works:

```bash
python server.py
```

## Render Deployment

Use the same Flask service shape:

```text
Build command: pip install -r requirements.txt
Start command: gunicorn app:app
```

Optional environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `PORT` | `8000` | Local/server port |
| `CACHE_SEC` | `90` | Live NSE cache TTL |
| `NSE_DATA_DIR` | `nse_data` | Cookie/download/snapshot folder |
| `NSE_SERVER_MODE` | auto | Force `nse` server mode with `1` or local mode with `0` |

## Notes

- Public NSE endpoints can be delayed, blocked, or temporarily unavailable. The dashboard falls back to recent cached data when possible.
- Weekly and monthly views are built from available NIFTY option expiries. If NSE returns fewer expiries, the app reuses the nearest available expiry and marks the evidence accordingly.
- This is a research dashboard only. It is not financial advice or a recommendation to buy or sell.
