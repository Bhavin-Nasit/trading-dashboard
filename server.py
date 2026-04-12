"""
NSE Live Dashboard — Python Backend
Uses: nse[local] package by BennyThadikaran (handles sessions correctly)

Install:  pip install "nse[local]" flask flask-cors
Run:      python server.py
API:      http://localhost:8000
Frontend: http://localhost:5500  (VS Code Live Server on index.html)
"""

import os
import time
import datetime
import traceback
from pathlib import Path
from flask import Flask, jsonify
from flask_cors import CORS
from flask import send_from_directory

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path("./nse_data")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ── NSE client ────────────────────────────────────────────────────────────────
_nse = None

def get_nse():
    global _nse
    if _nse is not None:
        return _nse
    try:
        from nse import NSE
        _nse = NSE(download_folder=str(DOWNLOAD_DIR), server=False)
        print("  ✓ NSE client ready")
    except ImportError:
        print("  ✗ nse package not found. Run: pip install \"nse[local]\"")
    except Exception as e:
        print(f"  ✗ NSE init error: {e}")
    return _nse

# ── Cache ─────────────────────────────────────────────────────────────────────
_cache     = {}
_cache_ttl = {}
CACHE_SEC  = 60

def cached(key, fn):
    now = time.time()
    if key in _cache and now - _cache_ttl.get(key, 0) < CACHE_SEC:
        return _cache[key]
    try:
        data = fn()
        if data is not None:
            _cache[key]     = data
            _cache_ttl[key] = now
            return data
    except Exception as e:
        print(f"  ✗ {key}: {e}")
    return _cache.get(key, None)

# ── Helpers ───────────────────────────────────────────────────────────────────
def ist_now():
    tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.now(tz).strftime("%I:%M:%S %p IST")

def is_market_open():
    tz  = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    ist = datetime.datetime.now(tz)
    if ist.weekday() >= 5:
        return False
    o = ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    c = ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return o <= ist <= c

def safe_float(v, default=0.0):
    try:
        return float(v or default)
    except:
        return default

def safe_int(v, default=0):
    try:
        return int(float(v or default))
    except:
        return default

# ── Fetchers ──────────────────────────────────────────────────────────────────

def _fetch_indices():
    nse = get_nse()
    if not nse:
        return None

    data = nse.listIndices()
    if not data:
        return None

    want = {
        "NIFTY 50":         "nifty",
        "NIFTY BANK":       "banknifty",
        "NIFTY IT":         "niftyit",
        "INDIA VIX":        "vix",
        "NIFTY MIDCAP 100": "midcap",
        "NIFTY AUTO":       "niftyauto",
        "NIFTY PHARMA":     "niftypharma",
        "NIFTY FMCG":       "niftyfmcg",
    }

    result = {}
    for row in data.get("data", []):
        key = want.get(row.get("index", ""))
        if not key:
            continue
        last   = safe_float(row.get("last"))
        prev   = safe_float(row.get("previousClose"), last)
        change = safe_float(row.get("change"), last - prev)
        pct    = safe_float(row.get("percentChange"))
        result[key] = {
            "name":      row.get("index", key),
            "price":     round(last, 2),
            "change":    round(change, 2),
            "changePct": round(pct, 2),
            "high":      safe_float(row.get("high"), last),
            "low":       safe_float(row.get("low"),  last),
            "open":      safe_float(row.get("open"), last),
            "prev":      round(prev, 2),
            "advances":  safe_int(row.get("advances")),
            "declines":  safe_int(row.get("declines")),
        }

    result["timestamp"] = ist_now()
    return result


def _fetch_fii_dii():
    nse = get_nse()
    if not nse:
        return None

    # nse package method name
    fn = getattr(nse, "fiiDiiStatistics", None) or getattr(nse, "fii_dii", None)
    if not fn:
        # fallback: direct request using nse's internal session
        return _fetch_fii_dii_direct()

    data = fn()
    return _parse_fii_dii(data)


def _fetch_fii_dii_direct():
    """Direct NSE call using requests with proper headers"""
    import requests
    sess = requests.Session()
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://www.nseindia.com/",
        "Accept":     "application/json, text/plain, */*",
    }
    sess.headers.update(hdrs)
    # seed cookies
    sess.get("https://www.nseindia.com", timeout=10)
    sess.get("https://www.nseindia.com/market-data/live-equity-market", timeout=8)

    r = sess.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
    r.raise_for_status()
    return _parse_fii_dii(r.json())


def _parse_fii_dii(data):
    if not data:
        return None
    result = {"fii": {}, "dii": {}, "timestamp": ist_now()}
    rows = data if isinstance(data, list) else data.get("data", [])
    for row in rows:
        cat  = str(row.get("category", "")).upper()
        buy  = safe_float(row.get("buyValue")  or row.get("buy_value"))
        sell = safe_float(row.get("sellValue") or row.get("sell_value"))
        net  = safe_float(row.get("netValue")  or row.get("net_value"), buy - sell)
        entry = {"buy": round(buy,2), "sell": round(sell,2), "net": round(net,2)}
        if any(x in cat for x in ["FII","FPI"]):
            result["fii"] = entry
        elif "DII" in cat:
            result["dii"] = entry
    return result


def _fetch_option_chain():
    nse = get_nse()
    if not nse:
        return None

    raw = nse.optionChain("NIFTY")
    if not raw:
        return None

    rec_data = (raw.get("filtered") or raw.get("records") or {})
    records  = rec_data.get("data", [])
    spot     = safe_float((raw.get("records") or raw.get("filtered") or {}).get("underlyingValue"))
    atm      = round(spot / 50) * 50

    strikes       = []
    total_call_oi = 0.0
    total_put_oi  = 0.0

    for rec in records:
        s = safe_float(rec.get("strikePrice"))
        if abs(s - atm) > 1500:
            continue
        ce = rec.get("CE") or {}
        pe = rec.get("PE") or {}

        c_oi  = safe_float(ce.get("openInterest"))
        c_chg = safe_float(ce.get("changeinOpenInterest"))
        p_oi  = safe_float(pe.get("openInterest"))
        p_chg = safe_float(pe.get("changeinOpenInterest"))

        total_call_oi += c_oi
        total_put_oi  += p_oi

        strikes.append({
            "strike":  int(s),
            "callOI":  round(c_oi  / 100000, 2),
            "callChg": round(c_chg / 100000, 2),
            "callIV":  round(safe_float(ce.get("impliedVolatility")), 1),
            "callLTP": round(safe_float(ce.get("lastPrice")), 2),
            "putOI":   round(p_oi  / 100000, 2),
            "putChg":  round(p_chg / 100000, 2),
            "putIV":   round(safe_float(pe.get("impliedVolatility")), 1),
            "putLTP":  round(safe_float(pe.get("lastPrice")), 2),
            "isATM":   int(s) == atm,
        })

    strikes.sort(key=lambda x: x["strike"])
    pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi else 0

    # Try built-in maxpain first
    max_pain = atm
    try:
        mp = nse.maxpain("NIFTY")
        max_pain = mp.get("maxpain", atm) if isinstance(mp, dict) else atm
    except:
        max_pain = _calc_max_pain(strikes, atm)

    return {
        "symbol":      "NIFTY",
        "spot":        round(spot, 2),
        "atm":         atm,
        "maxPain":     max_pain,
        "pcr":         pcr,
        "totalCallOI": round(total_call_oi / 100000, 1),
        "totalPutOI":  round(total_put_oi  / 100000, 1),
        "strikes":     strikes,
        "timestamp":   ist_now(),
    }


def _calc_max_pain(strikes, atm):
    if not strikes:
        return atm
    best, low = atm, float("inf")
    for c in strikes:
        cs   = c["strike"]
        pain = (sum((cs - r["strike"]) * r["callOI"] for r in strikes if r["strike"] < cs) +
                sum((r["strike"] - cs) * r["putOI"]  for r in strikes if r["strike"] > cs))
        if pain < low:
            low, best = pain, cs
    return best


def _fetch_bulk_deals():
    nse = get_nse()
    if not nse:
        return None

    deals = []
    today = datetime.date.today()

    def parse_deal(d, is_block=False):
        qty   = safe_int(d.get("BD_QTY_TRD") or d.get("quantity"))
        price = safe_float(d.get("BD_TP_WATP") or d.get("tradePrice"))
        flag  = str(d.get("BD_BUY_SELL_FLAG") or d.get("buySell") or "B")
        return {
            "stock":   str(d.get("BD_SYMBOL") or d.get("symbol") or "N/A"),
            "buyer":   str(d.get("BD_CLIENT_NAME") or d.get("clientName") or "Institutional"),
            "type":    "BUY" if flag.upper().startswith("B") else "SELL",
            "qty":     f"{qty:,}",
            "price":   round(price, 2),
            "value":   round((qty * price) / 1e7, 2),
            "isBlock": is_block,
        }

    try:
        for d in (nse.bulkdeals(from_dt=today, to_dt=today) or [])[:8]:
            deals.append(parse_deal(d, False))
    except Exception as e:
        print(f"  bulk deals: {e}")

    try:
        for d in (nse.blockDeals() or [])[:5]:
            deals.append(parse_deal(d, True))
    except Exception as e:
        print(f"  block deals: {e}")

    return {"deals": deals, "timestamp": ist_now()}


def _fetch_fii_derivatives():
    """FII participant-wise OI — uses nse session to hit the correct endpoint"""
    nse = get_nse()
    result = {"futLong":0,"futShort":0,"ratio":0,"callOI":0,"putOI":0,
              "stance":"N/A","timestamp":ist_now()}
    try:
        sess = getattr(nse, "_session", None) if nse else None
        import requests as req
        s = sess or req.Session()
        if not sess:
            s.headers.update({
                "User-Agent": "Mozilla/5.0",
                "Referer":    "https://www.nseindia.com/",
                "Accept":     "application/json",
            })
            s.get("https://www.nseindia.com", timeout=8)

        for url in [
            "https://www.nseindia.com/api/participant-wise-open-interest",
            "https://www.nseindia.com/api/equity-stock-indices?index=NIFTY%2050",
        ]:
            r = s.get(url, timeout=8)
            if r.status_code != 200:
                continue
            rows = r.json() if isinstance(r.json(), list) else r.json().get("data",[])
            for row in rows:
                cat = str(row.get("clientType") or row.get("category","")).upper()
                if "FII" in cat or "FPI" in cat:
                    result["futLong"]  = safe_int(row.get("futureIndexLong"))
                    result["futShort"] = safe_int(row.get("futureIndexShort"))
                    result["callOI"]   = safe_int(row.get("optionIndexCallLong"))
                    result["putOI"]    = safe_int(row.get("optionIndexPutLong"))
                    if result["futShort"]:
                        ratio = round(result["futLong"]/result["futShort"],2)
                        result["ratio"]  = ratio
                        result["stance"] = "Net Long — Bullish" if ratio>1 else "Net Short — Bearish"
                    break
            if result["futLong"]:
                break
    except Exception as e:
        print(f"  FII derivatives: {e}")
    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify({
        "status":     "ok",
        "marketOpen": is_market_open(),
        "serverTime": ist_now(),
        "nseReady":   get_nse() is not None,
    })

@app.route("/api/all")
def api_all():
    get_nse()
    return jsonify({
        "indices":        cached("indices",         _fetch_indices),
        "fiiDii":         cached("fii_dii",         _fetch_fii_dii),
        "optionChain":    cached("option_chain",    _fetch_option_chain),
        "fiiDerivatives": cached("fii_derivatives", _fetch_fii_derivatives),
        "bulkDeals":      cached("bulk_deals",      _fetch_bulk_deals),
        "marketOpen":     is_market_open(),
        "serverTime":     ist_now(),
    })

@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/api/indices")
def api_indices():
    return jsonify(cached("indices", _fetch_indices) or {"error":"failed"})

@app.route("/api/fii-dii")
def api_fii_dii():
    return jsonify(cached("fii_dii", _fetch_fii_dii) or {"error":"failed"})

@app.route("/api/option-chain")
def api_option_chain():
    return jsonify(cached("option_chain", _fetch_option_chain) or {"error":"failed"})

@app.route("/api/bulk-deals")
def api_bulk_deals():
    return jsonify(cached("bulk_deals", _fetch_bulk_deals) or {"error":"failed"})


if __name__ == "__main__":
    print("\n" + "="*52)
    print("  NSE Live Dashboard — Python Backend")
    print("="*52)
    print('  pip install "nse[local]" flask flask-cors')
    print()
    print("  API  →  http://localhost:8000")
    print("  UI   →  open index.html with Live Server")
    print("="*52 + "\n")
    get_nse()
    # app.run(host="0.0.0.0", port=8000, debug=False)
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
