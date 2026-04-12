# NSE Live Dashboard

## One-time setup

```bash
pip install "nse[local]" flask flask-cors
```

## Run every time

```bash
python server.py
```

Then open `index.html` with VS Code Live Server (port 5500).

## Why this works

The `nse[local]` package by BennyThadikaran handles NSE session cookies
automatically — this is what was broken before. It seeds the session by
hitting the NSE homepage first, then all API calls succeed.

## Data sources

| Panel | Method used |
|---|---|
| Nifty + indices | `nse.listIndices()` |
| FII / DII cash | `nse.fiiDiiStatistics()` |
| Option chain OI | `nse.optionChain("NIFTY")` |
| Max pain | `nse.maxpain("NIFTY")` |
| Bulk deals | `nse.bulkdeals(today, today)` |
| Block deals | `nse.blockDeals()` |

## Troubleshooting

- **"NSE client not found"** → run `pip install "nse[local]"`
- **Empty data after market hours** → normal, NSE APIs return last close data
- **Port 8000 busy** → change `port=8000` in server.py to 8001
