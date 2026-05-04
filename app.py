from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify, render_template_string

try:
    from flask_cors import CORS
except Exception:  # pragma: no cover - optional local dependency
    CORS = None


app = Flask(__name__)
if CORS:
    CORS(app)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DATA_DIR = Path(os.environ.get("NSE_DATA_DIR", "nse_data"))
DATA_DIR.mkdir(exist_ok=True)
SNAPSHOT_FILE = DATA_DIR / "nifty_signal_history.json"
CACHE_SEC = int(os.environ.get("CACHE_SEC", "90"))
PARTICIPANT_COLUMNS = (
    "Future Index Long",
    "Future Index Short",
    "Option Index Call Long",
    "Option Index Call Short",
    "Option Index Put Long",
    "Option Index Put Short",
)

_cache: dict[str, tuple[float, Any]] = {}
_nse = None
_http_session = None


def ist_now() -> dt.datetime:
    return dt.datetime.now(IST)


def ist_stamp() -> str:
    return ist_now().strftime("%d-%b-%Y %I:%M:%S %p IST")


def is_market_open() -> bool:
    now = ist_now()
    if now.weekday() >= 5:
        return False
    open_at = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_at = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_at <= now <= close_at


def cached(key: str, ttl: int, fn: Callable[[], Any]) -> Any:
    now = time.time()
    item = _cache.get(key)
    if item and now - item[0] < ttl:
        return item[1]
    try:
        value = fn()
        _cache[key] = (now, value)
        return value
    except Exception as exc:
        fallback = item[1] if item else None
        return {"error": str(exc), "fallback": fallback, "timestamp": ist_stamp()}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()
        if value in ("-", "--"):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(safe_float(value, float(default))))
    except Exception:
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def weighted(values: list[tuple[float, float]]) -> float:
    total_weight = sum(w for _, w in values if w > 0)
    if total_weight <= 0:
        return 50.0
    return round(sum(v * w for v, w in values) / total_weight, 1)


def pct(part: float, total: float) -> float:
    return round((part / total) * 100, 2) if total else 0.0


def money_cr(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}Rs.{abs(value):,.0f} Cr"


def parse_expiry(value: Any) -> dt.date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def get_nse():
    global _nse
    if _nse is not None:
        return _nse
    try:
        from nse import NSE
    except Exception:
        return None

    server_pref = os.environ.get("NSE_SERVER_MODE")
    if server_pref is None:
        preferred = bool(os.environ.get("RENDER"))
    else:
        preferred = server_pref.lower() in {"1", "true", "yes", "server"}

    for server_mode in (preferred, not preferred):
        try:
            _nse = NSE(download_folder=str(DATA_DIR), server=server_mode, timeout=15)
            return _nse
        except Exception:
            continue
    return None


def request_json(url: str, timeout: int = 12) -> Any:
    global _http_session
    import requests

    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.nseindia.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        _http_session.get("https://www.nseindia.com", timeout=timeout)
    _http_session.headers.update({"Accept": "application/json, text/plain, */*"})
    response = _http_session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def request_text(url: str, timeout: int = 12) -> str:
    global _http_session
    import requests

    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "text/csv,text/plain,*/*",
                "Referer": "https://www.nseindia.com/",
            }
        )
        _http_session.get("https://www.nseindia.com", timeout=timeout)
    _http_session.headers.update({"Accept": "text/csv,text/plain,*/*"})
    response = _http_session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def normalize_index_name(value: Any) -> str:
    return " ".join(str(value or "").upper().replace("&", "AND").split())


def parse_index_row(row: dict[str, Any]) -> dict[str, Any]:
    last = safe_float(row.get("last") or row.get("lastPrice"))
    prev = safe_float(row.get("previousClose") or row.get("previousclose"), last)
    change = safe_float(row.get("change"), last - prev)
    change_pct = safe_float(row.get("percentChange") or row.get("perChange"))
    return {
        "name": str(row.get("index") or row.get("indexName") or ""),
        "price": round(last, 2),
        "change": round(change, 2),
        "changePct": round(change_pct, 2),
        "open": round(safe_float(row.get("open"), last), 2),
        "high": round(safe_float(row.get("high"), last), 2),
        "low": round(safe_float(row.get("low"), last), 2),
        "prev": round(prev, 2),
        "advances": safe_int(row.get("advances")),
        "declines": safe_int(row.get("declines")),
        "unchanged": safe_int(row.get("unchanged")),
    }


def fetch_indices() -> dict[str, Any]:
    wanted = {
        "NIFTY 50": "nifty",
        "NIFTY BANK": "banknifty",
        "NIFTY FINANCIAL SERVICES": "finservice",
        "NIFTY MIDCAP 100": "midcap",
        "NIFTY NEXT 50": "next50",
        "NIFTY IT": "it",
        "NIFTY AUTO": "auto",
        "NIFTY FMCG": "fmcg",
        "NIFTY PHARMA": "pharma",
        "INDIA VIX": "vix",
    }
    rows: list[dict[str, Any]] = []
    source = "nse package"
    nse = get_nse()
    if nse:
        try:
            payload = nse.listIndices()
            rows = payload.get("data", []) if isinstance(payload, dict) else []
        except Exception:
            rows = []
    if not rows:
        source = "nse direct"
        payload = request_json("https://www.nseindia.com/api/allIndices")
        rows = payload.get("data", []) if isinstance(payload, dict) else []

    result: dict[str, Any] = {"source": source, "timestamp": ist_stamp()}
    for row in rows:
        key = wanted.get(normalize_index_name(row.get("index") or row.get("indexName")))
        if key:
            result[key] = parse_index_row(row)

    if "nifty" in result:
        n = result["nifty"]
        total = n["advances"] + n["declines"]
        result["breadthPct"] = round((n["advances"] / total) * 100, 1) if total else 50.0
    else:
        result["breadthPct"] = 50.0
    return result


def parse_fii_dii_rows(payload: Any) -> dict[str, Any]:
    rows = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
    result = {"fii": {}, "dii": {}, "source": "nse", "timestamp": ist_stamp()}
    for row in rows:
        category = normalize_index_name(row.get("category") or row.get("name"))
        buy = safe_float(row.get("buyValue") or row.get("buy_value") or row.get("buy"))
        sell = safe_float(row.get("sellValue") or row.get("sell_value") or row.get("sell"))
        net = safe_float(row.get("netValue") or row.get("net_value") or row.get("net"), buy - sell)
        item = {"buy": round(buy, 2), "sell": round(sell, 2), "net": round(net, 2)}
        if "FII" in category or "FPI" in category:
            result["fii"] = item
        elif "DII" in category:
            result["dii"] = item
    return result


def fetch_fii_dii() -> dict[str, Any]:
    nse = get_nse()
    if nse:
        fn = getattr(nse, "fiiDiiStatistics", None) or getattr(nse, "fii_dii", None)
        if fn:
            try:
                parsed = parse_fii_dii_rows(fn())
                parsed["source"] = "nse package"
                return parsed
            except Exception:
                pass
    parsed = parse_fii_dii_rows(request_json("https://www.nseindia.com/api/fiidiiTradeReact"))
    parsed["source"] = "nse direct"
    return parsed


def fetch_option_chain_raw() -> dict[str, Any]:
    nse = get_nse()
    if nse:
        try:
            raw = nse.optionChain("NIFTY")
            if isinstance(raw, dict) and raw:
                raw["source"] = "nse package"
                return raw
        except Exception:
            pass
    raw = request_json("https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY")
    raw["source"] = "nse direct"
    return raw


def select_expiries(expiries: list[dt.date]) -> dict[str, dt.date | None]:
    if not expiries:
        return {"daily": None, "weekly": None, "monthly": None}
    today = ist_now().date()
    future = [x for x in expiries if x >= today] or expiries
    future = sorted(set(future))
    daily = future[0]
    weekly = future[1] if len(future) > 1 else future[0]
    same_month = [x for x in future if x.month == daily.month and x.year == daily.year]
    monthly = max(same_month) if same_month else future[min(len(future) - 1, 3)]
    if monthly == daily and len(future) > 1:
        monthly = future[min(len(future) - 1, 3)]
    return {"daily": daily, "weekly": weekly, "monthly": monthly}


def calc_max_pain(rows: list[dict[str, Any]], atm: int) -> int:
    if not rows:
        return atm
    best = atm
    best_pain = math.inf
    strikes = [safe_int(row.get("strike")) for row in rows]
    for settlement in strikes:
        pain = 0.0
        for row in rows:
            strike = safe_float(row.get("strike"))
            call_oi = safe_float(row.get("callOIContracts"))
            put_oi = safe_float(row.get("putOIContracts"))
            pain += max(settlement - strike, 0) * call_oi
            pain += max(strike - settlement, 0) * put_oi
        if pain < best_pain:
            best_pain = pain
            best = settlement
    return int(best)


def chain_view(records: list[dict[str, Any]], expiry: dt.date | None, spot: float, label: str) -> dict[str, Any]:
    if not expiry:
        return {"label": label, "available": False, "reason": "No expiry data"}

    atm = int(round(spot / 50.0) * 50) if spot else 0
    near_limit = 1300 if label == "daily" else 1800
    rows: list[dict[str, Any]] = []
    totals = defaultdict(float)
    atm_iv_values: list[float] = []
    atm_straddle = 0.0

    for rec in records:
        if parse_expiry(rec.get("expiryDate")) != expiry:
            continue
        strike = safe_float(rec.get("strikePrice"))
        if atm and abs(strike - atm) > near_limit:
            continue
        ce = rec.get("CE") or {}
        pe = rec.get("PE") or {}
        call_oi = safe_float(ce.get("openInterest"))
        put_oi = safe_float(pe.get("openInterest"))
        call_chg = safe_float(ce.get("changeinOpenInterest"))
        put_chg = safe_float(pe.get("changeinOpenInterest"))
        call_iv = safe_float(ce.get("impliedVolatility"))
        put_iv = safe_float(pe.get("impliedVolatility"))
        call_ltp = safe_float(ce.get("lastPrice"))
        put_ltp = safe_float(pe.get("lastPrice"))

        totals["callOI"] += call_oi
        totals["putOI"] += put_oi
        totals["callChg"] += call_chg
        totals["putChg"] += put_chg
        totals["callUnwind"] += abs(call_chg) if call_chg < 0 else 0
        totals["putUnwind"] += abs(put_chg) if put_chg < 0 else 0

        if int(strike) == atm:
            atm_straddle = call_ltp + put_ltp
            if call_iv:
                atm_iv_values.append(call_iv)
            if put_iv:
                atm_iv_values.append(put_iv)

        rows.append(
            {
                "strike": int(strike),
                "callOI": round(call_oi / 100000, 2),
                "putOI": round(put_oi / 100000, 2),
                "callChg": round(call_chg / 100000, 2),
                "putChg": round(put_chg / 100000, 2),
                "callChgPct": pct(call_chg, max(call_oi - call_chg, 0)),
                "putChgPct": pct(put_chg, max(put_oi - put_chg, 0)),
                "callIV": round(call_iv, 1),
                "putIV": round(put_iv, 1),
                "callLTP": round(call_ltp, 2),
                "putLTP": round(put_ltp, 2),
                "callOIContracts": call_oi,
                "putOIContracts": put_oi,
            }
        )

    rows.sort(key=lambda item: item["strike"])
    if not rows:
        return {"label": label, "available": False, "expiry": expiry.isoformat(), "reason": "No rows for expiry"}

    put_oi = totals["putOI"]
    call_oi = totals["callOI"]
    put_chg = totals["putChg"]
    call_chg = totals["callChg"]
    total_chg_abs = abs(put_chg) + abs(call_chg)
    chg_pressure = ((put_chg - call_chg) / total_chg_abs) if total_chg_abs else 0.0

    above = [row for row in rows if row["strike"] >= atm]
    below = [row for row in rows if row["strike"] <= atm]
    call_wall_row = max(above or rows, key=lambda row: row["callOIContracts"])
    put_base_row = max(below or rows, key=lambda row: row["putOIContracts"])
    max_pain = calc_max_pain(rows, atm)
    pcr = round(put_oi / call_oi, 2) if call_oi else 0.0
    call_chg_pct = pct(call_chg, max(call_oi - call_chg, 0))
    put_chg_pct = pct(put_chg, max(put_oi - put_chg, 0))
    avg_iv = round(sum(atm_iv_values) / len(atm_iv_values), 2) if atm_iv_values else 0.0
    days = max((expiry - ist_now().date()).days, 1)
    iv_range = round(spot * (avg_iv / 100) * math.sqrt(days / 365), 0) if avg_iv and spot else 0.0
    straddle_range = round(atm_straddle, 0) if atm_straddle else iv_range
    top_rows = sorted(rows, key=lambda row: abs(row["strike"] - atm))[:17]

    return {
        "label": label,
        "available": True,
        "expiry": expiry.strftime("%d-%b-%Y"),
        "daysToExpiry": days,
        "spot": round(spot, 2),
        "atm": atm,
        "pcr": pcr,
        "totalCallOI": round(call_oi / 100000, 1),
        "totalPutOI": round(put_oi / 100000, 1),
        "totalCallChg": round(call_chg / 100000, 1),
        "totalPutChg": round(put_chg / 100000, 1),
        "callChgPct": round(call_chg_pct, 2),
        "putChgPct": round(put_chg_pct, 2),
        "oiShiftPct": round(chg_pressure * 100, 1),
        "callWall": call_wall_row["strike"],
        "putBase": put_base_row["strike"],
        "maxPain": max_pain,
        "avgATMIV": avg_iv,
        "expectedMove": straddle_range,
        "expectedLow": round(spot - straddle_range, 0),
        "expectedHigh": round(spot + straddle_range, 0),
        "callUnwind": round(totals["callUnwind"] / 100000, 1),
        "putUnwind": round(totals["putUnwind"] / 100000, 1),
        "strikes": top_rows,
    }


def fetch_option_chain() -> dict[str, Any]:
    raw = fetch_option_chain_raw()
    records = (raw.get("records") or {}).get("data", []) or (raw.get("filtered") or {}).get("data", [])
    spot = safe_float((raw.get("records") or {}).get("underlyingValue") or (raw.get("filtered") or {}).get("underlyingValue"))
    expiries = sorted({x for x in (parse_expiry(row.get("expiryDate")) for row in records) if x})
    selected = select_expiries(expiries)
    views = {name: chain_view(records, expiry, spot, name) for name, expiry in selected.items()}
    return {
        "symbol": "NIFTY",
        "spot": round(spot, 2),
        "expiries": [x.strftime("%d-%b-%Y") for x in expiries],
        "selectedExpiries": {k: v.strftime("%d-%b-%Y") if v else None for k, v in selected.items()},
        "views": views,
        "source": raw.get("source", "nse"),
        "timestamp": ist_stamp(),
    }


def parse_participant_csv(text: str, source_date: dt.date) -> dict[str, Any] | None:
    lines = [line for line in text.splitlines() if line.strip()]
    start = 0
    for idx, line in enumerate(lines):
        if "Client Type" in line and "Future Index Long" in line:
            start = idx
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows: dict[str, dict[str, Any]] = {}
    for raw in reader:
        label = normalize_index_name(raw.get("Client Type"))
        if not label or label.startswith("TOTAL"):
            continue
        key = "fii" if "FII" in label or "FPI" in label else label.lower().split()[0]
        rows[key] = {k.strip(): safe_int(v) for k, v in raw.items() if k and k != "Client Type"}
    if not rows:
        return None
    return {"date": source_date.strftime("%d-%b-%Y"), "rows": rows, "source": "NSE participant OI archive"}


def participant_deltas(
    latest: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> dict[str, dict[str, int]]:
    deltas: dict[str, dict[str, int]] = {}
    for name in sorted(set(latest) | set(previous)):
        deltas[name] = {
            col: safe_int(latest.get(name, {}).get(col)) - safe_int(previous.get(name, {}).get(col))
            for col in PARTICIPANT_COLUMNS
        }
    return deltas


def fetch_participant_oi() -> dict[str, Any]:
    today = ist_now().date()
    urls = (
        "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{date}.csv",
        "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{date}.csv",
    )
    errors: list[str] = []
    found: list[dict[str, Any]] = []
    for days_back in range(0, 12):
        target = today - dt.timedelta(days=days_back)
        if target.weekday() >= 5:
            continue
        date_key = target.strftime("%d%m%Y")
        for template in urls:
            try:
                parsed = parse_participant_csv(request_text(template.format(date=date_key)), target)
                if parsed:
                    parsed["timestamp"] = ist_stamp()
                    found.append(parsed)
                    break
            except Exception as exc:
                errors.append(f"{date_key}: {exc}")
        if len(found) >= 2:
            latest, previous = found[0], found[1]
            latest["previousDate"] = previous.get("date")
            latest["previousRows"] = previous.get("rows", {})
            latest["deltas"] = participant_deltas(latest.get("rows", {}), previous.get("rows", {}))
            return latest
    if found:
        found[0]["previousDate"] = None
        found[0]["previousRows"] = {}
        found[0]["deltas"] = {}
        return found[0]
    return {"date": None, "rows": {}, "source": "unavailable", "errors": errors[-3:], "timestamp": ist_stamp()}


def parse_deal(row: dict[str, Any], block: bool = False) -> dict[str, Any]:
    qty = safe_int(row.get("BD_QTY_TRD") or row.get("quantity") or row.get("qty"))
    price = safe_float(row.get("BD_TP_WATP") or row.get("tradePrice") or row.get("price"))
    side = str(row.get("BD_BUY_SELL_FLAG") or row.get("buySell") or row.get("side") or "").upper()
    return {
        "stock": str(row.get("BD_SYMBOL") or row.get("symbol") or "N/A"),
        "name": str(row.get("BD_CLIENT_NAME") or row.get("clientName") or "Institutional"),
        "side": "SELL" if side.startswith("S") else "BUY",
        "qty": qty,
        "price": round(price, 2),
        "valueCr": round((qty * price) / 10000000, 2),
        "block": block,
    }


def fetch_deals() -> dict[str, Any]:
    nse = get_nse()
    deals: list[dict[str, Any]] = []
    if nse:
        today = ist_now().date()
        try:
            for row in (nse.bulkdeals(from_dt=today, to_dt=today) or [])[:12]:
                deals.append(parse_deal(row, False))
        except Exception:
            pass
        try:
            for row in (nse.blockDeals() or [])[:8]:
                deals.append(parse_deal(row, True))
        except Exception:
            pass
    return {"deals": deals[:16], "timestamp": ist_stamp(), "source": "nse package" if nse else "unavailable"}


def score_pcr(pcr_value: float) -> tuple[float, str]:
    if not pcr_value:
        return 50.0, "PCR unavailable"
    if pcr_value < 0.75:
        return 24.0, "Call writers dominate; downside pressure"
    if pcr_value < 0.9:
        return 38.0, "Call-heavy setup; sell-on-rise risk"
    if pcr_value < 1.15:
        return 52.0, "Balanced options book"
    if pcr_value < 1.45:
        return 68.0, "Put-heavy book; dips likely defended"
    if pcr_value < 1.75:
        return 60.0, "Very put-heavy; bullish but crowded"
    return 45.0, "Extreme put side crowding; reversal risk"


def score_oi_view(view: dict[str, Any]) -> dict[str, Any]:
    if not view.get("available"):
        return {"score": 50.0, "bias": "Neutral", "drivers": ["Option data unavailable"]}
    pcr_score, pcr_note = score_pcr(safe_float(view.get("pcr")))
    shift = safe_float(view.get("oiShiftPct"))
    shift_score = clamp(50 + shift * 0.75, 5, 95)
    spot = safe_float(view.get("spot"))
    call_wall = safe_float(view.get("callWall"))
    put_base = safe_float(view.get("putBase"))
    max_pain = safe_float(view.get("maxPain"))
    wall_score = 50.0
    wall_notes: list[str] = []

    if spot and call_wall and put_base:
        resistance_gap = ((call_wall - spot) / spot) * 100
        support_gap = ((spot - put_base) / spot) * 100
        if resistance_gap < 0.35:
            wall_score -= 12
            wall_notes.append(f"Call wall at {call_wall:,.0f} is close overhead")
        elif resistance_gap > 1.2:
            wall_score += 6
            wall_notes.append("Overhead call wall is not immediate")
        if support_gap < 0.35:
            wall_score += 12
            wall_notes.append(f"Put base at {put_base:,.0f} is close support")
        elif support_gap > 1.2:
            wall_score -= 6
            wall_notes.append("Put support is far below spot")

    pain_score = 50.0
    if spot and max_pain:
        pain_gap = ((spot - max_pain) / spot) * 100
        pain_score = clamp(50 - pain_gap * 8, 25, 75)
        if abs(pain_gap) < 0.35:
            pain_note = "Spot is pinned near max pain"
        elif pain_gap > 0:
            pain_note = "Max pain is below spot; expiry magnet leans lower"
        else:
            pain_note = "Max pain is above spot; expiry magnet leans higher"
    else:
        pain_note = "Max pain unavailable"

    score = weighted([(pcr_score, 0.35), (shift_score, 0.35), (wall_score, 0.2), (pain_score, 0.1)])
    drivers = [
        pcr_note,
        f"Fresh OI pressure is {shift:+.1f}% from put-call change",
        pain_note,
        *wall_notes[:2],
    ]
    return {"score": score, "bias": bias_label(score), "drivers": drivers[:5]}


def index_score(indices: dict[str, Any]) -> dict[str, Any]:
    nifty = indices.get("nifty", {})
    bank = indices.get("banknifty", {})
    mid = indices.get("midcap", {})
    vix = indices.get("vix", {})
    n_score = clamp(50 + safe_float(nifty.get("changePct")) * 12, 0, 100)
    bank_score = clamp(50 + safe_float(bank.get("changePct")) * 10, 0, 100) if bank else 50
    mid_score = clamp(50 + safe_float(mid.get("changePct")) * 8, 0, 100) if mid else 50
    breadth_score = clamp(safe_float(indices.get("breadthPct"), 50), 0, 100)
    vix_value = safe_float(vix.get("price"))
    vix_score = 55
    if vix_value:
        if vix_value < 12:
            vix_score = 60
        elif vix_value < 17:
            vix_score = 55
        elif vix_value < 21:
            vix_score = 45
        else:
            vix_score = 34
    score = weighted([(n_score, 0.35), (bank_score, 0.25), (mid_score, 0.15), (breadth_score, 0.15), (vix_score, 0.1)])
    drivers = [
        f"NIFTY change {safe_float(nifty.get('changePct')):+.2f}%",
        f"Breadth {safe_float(indices.get('breadthPct'), 50):.1f}% advancing",
        f"Bank Nifty confirmation {safe_float(bank.get('changePct')):+.2f}%" if bank else "Bank Nifty unavailable",
        f"India VIX {vix_value:.2f}" if vix_value else "India VIX unavailable",
    ]
    return {"score": score, "bias": bias_label(score), "drivers": drivers}


def cash_flow_score(flows: dict[str, Any]) -> dict[str, Any]:
    fii = flows.get("fii", {})
    dii = flows.get("dii", {})
    fii_net = safe_float(fii.get("net"))
    dii_net = safe_float(dii.get("net"))
    fii_score = clamp(50 + fii_net / 70, 5, 95)
    dii_score = clamp(50 + dii_net / 110, 15, 85)
    score = weighted([(fii_score, 0.7), (dii_score, 0.3)])
    if fii_net < 0 < dii_net:
        story = "DIIs are absorbing FII selling; downside may be slower but rallies can fade"
    elif fii_net > 0 and dii_net > 0:
        story = "Both FII and DII cash desks are net buyers; institutional tailwind"
    elif fii_net < 0 and dii_net < 0:
        story = "Both major cash desks are net sellers; distribution risk"
    else:
        story = "Cash flow is mixed; derivatives carry more weight"
    return {
        "score": round(score, 1),
        "bias": bias_label(score),
        "drivers": [f"FII cash {money_cr(fii_net)}", f"DII cash {money_cr(dii_net)}", story],
    }


def participant_direction(row: dict[str, Any], delta: dict[str, Any] | None = None) -> dict[str, float]:
    fut_long = safe_float(row.get("Future Index Long"))
    fut_short = safe_float(row.get("Future Index Short"))
    call_long = safe_float(row.get("Option Index Call Long"))
    call_short = safe_float(row.get("Option Index Call Short"))
    put_long = safe_float(row.get("Option Index Put Long"))
    put_short = safe_float(row.get("Option Index Put Short"))
    fut_net = fut_long - fut_short
    option_net = (call_long - call_short) + (put_short - put_long)
    gross = max(fut_long + fut_short + call_long + call_short + put_long + put_short, 1)
    directional_pct = ((fut_net * 2.0 + option_net * 0.7) / gross) * 100
    delta = delta or {}
    fut_net_delta = safe_float(delta.get("Future Index Long")) - safe_float(delta.get("Future Index Short"))
    option_net_delta = (
        safe_float(delta.get("Option Index Call Long"))
        - safe_float(delta.get("Option Index Call Short"))
        + safe_float(delta.get("Option Index Put Short"))
        - safe_float(delta.get("Option Index Put Long"))
    )
    call_writing_delta = safe_float(delta.get("Option Index Call Short")) - safe_float(delta.get("Option Index Call Long"))
    put_writing_delta = safe_float(delta.get("Option Index Put Short")) - safe_float(delta.get("Option Index Put Long"))
    gross_delta = max(sum(abs(safe_float(delta.get(col))) for col in PARTICIPANT_COLUMNS), 1)
    directional_delta_pct = ((fut_net_delta * 2.0 + option_net_delta * 0.7) / gross_delta) * 100
    return {
        "futureNet": round(fut_net),
        "optionNet": round(option_net),
        "directionalPct": round(directional_pct, 2),
        "futureNetDelta": round(fut_net_delta),
        "optionNetDelta": round(option_net_delta),
        "callWritingDelta": round(call_writing_delta),
        "putWritingDelta": round(put_writing_delta),
        "directionalDeltaPct": round(directional_delta_pct, 2),
        "longShortRatio": round(fut_long / fut_short, 2) if fut_short else 0,
        "futLong": round(fut_long),
        "futShort": round(fut_short),
        "callLong": round(call_long),
        "callShort": round(call_short),
        "putLong": round(put_long),
        "putShort": round(put_short),
    }


def participant_score(participant: dict[str, Any]) -> dict[str, Any]:
    rows = participant.get("rows", {})
    if not rows:
        return {"score": 50.0, "bias": "Neutral", "details": {}, "drivers": ["Participant OI unavailable"]}
    deltas = participant.get("deltas", {})
    details = {name: participant_direction(row, deltas.get(name, {})) for name, row in rows.items()}
    fii_dir = details.get("fii", {}).get("directionalPct", 0)
    pro_dir = details.get("pro", {}).get("directionalPct", 0)
    client_dir = details.get("client", {}).get("directionalPct", 0)
    dii_dir = details.get("dii", {}).get("directionalPct", 0)
    fii_delta = details.get("fii", {}).get("directionalDeltaPct", 0)
    pro_delta = details.get("pro", {}).get("directionalDeltaPct", 0)
    smart_direction = fii_dir * 0.43 + pro_dir * 0.24 + fii_delta * 0.18 + pro_delta * 0.10 + dii_dir * 0.05 - client_dir * 0.10
    score = clamp(50 + smart_direction * 1.8, 5, 95)
    drivers = [
        f"FII derivative tilt {fii_dir:+.1f}%",
        f"Pro desk tilt {pro_dir:+.1f}%",
        f"FII one-day change {fii_delta:+.1f}%",
        f"Pro one-day change {pro_delta:+.1f}%",
        f"Client tilt {client_dir:+.1f}% treated contrarian",
        f"Participant data date {participant.get('date') or 'unavailable'}",
    ]
    return {"score": round(score, 1), "bias": bias_label(score), "details": details, "drivers": drivers}


def stance_from_score(score: float) -> str:
    if score >= 62:
        return "Bullish"
    if score <= 38:
        return "Bearish"
    return "Neutral"


def build_check(factor: str, score: float, reading: str) -> dict[str, Any]:
    return {"factor": factor, "score": round(clamp(score), 1), "stance": stance_from_score(score), "reading": reading}


def build_big_player_model(
    indices: dict[str, Any],
    options: dict[str, Any],
    flows: dict[str, Any],
    participant: dict[str, Any],
    part_score_data: dict[str, Any],
) -> dict[str, Any]:
    details = part_score_data.get("details", {})
    daily = options.get("views", {}).get("daily", {})
    fii = details.get("fii", {})
    pro = details.get("pro", {})
    client = details.get("client", {})
    dii = details.get("dii", {})
    cash_fii = safe_float(flows.get("fii", {}).get("net"))
    cash_dii = safe_float(flows.get("dii", {}).get("net"))
    vix = safe_float(indices.get("vix", {}).get("price"))
    oi_shift = safe_float(daily.get("oiShiftPct"))
    pcr_value = safe_float(daily.get("pcr"))
    spot = safe_float(daily.get("spot") or options.get("spot") or indices.get("nifty", {}).get("price"))
    call_wall = safe_float(daily.get("callWall"))
    put_base = safe_float(daily.get("putBase"))

    fii_direction = safe_float(fii.get("directionalPct"))
    pro_direction = safe_float(pro.get("directionalPct"))
    client_direction = safe_float(client.get("directionalPct"))
    fii_change = safe_float(fii.get("directionalDeltaPct"))
    pro_change = safe_float(pro.get("directionalDeltaPct"))
    client_change = safe_float(client.get("directionalDeltaPct"))

    wall_score = 50.0
    wall_reading = "No clean wall edge"
    if spot and call_wall and put_base:
        resistance_gap = ((call_wall - spot) / spot) * 100
        support_gap = ((spot - put_base) / spot) * 100
        if support_gap < resistance_gap:
            wall_score = 58 + min(18, (resistance_gap - support_gap) * 8)
            wall_reading = f"Nearest defense is put base {put_base:,.0f}; call wall room {resistance_gap:.2f}%"
        elif resistance_gap < support_gap:
            wall_score = 42 - min(18, (support_gap - resistance_gap) * 8)
            wall_reading = f"Nearest supply is call wall {call_wall:,.0f}; put base room {support_gap:.2f}%"

    cash_score = clamp(50 + cash_fii / 80 + cash_dii / 180, 5, 95)
    oi_score = clamp(50 + oi_shift * 0.7, 5, 95)
    fii_score = clamp(50 + fii_direction * 1.5 + fii_change * 0.7, 5, 95)
    pro_score = clamp(50 + pro_direction * 1.25 + pro_change * 0.65, 5, 95)
    client_contra_score = clamp(50 - client_direction * 1.15 - client_change * 0.45, 5, 95)
    volatility_score = 50
    if vix:
        volatility_score = 58 if vix < 14 else 52 if vix < 18 else 43 if vix < 22 else 34

    checks = [
        build_check(
            "FII carry-forward",
            fii_score,
            f"FII directional tilt {fii_direction:+.1f}% with one-day change {fii_change:+.1f}%",
        ),
        build_check(
            "Pro desk confirmation",
            pro_score,
            f"Pro directional tilt {pro_direction:+.1f}% with one-day change {pro_change:+.1f}%",
        ),
        build_check(
            "Client contra signal",
            client_contra_score,
            f"Client tilt {client_direction:+.1f}%; model treats retail/HNI crowding as contrarian",
        ),
        build_check(
            "FII/DII cash",
            cash_score,
            f"FII cash {money_cr(cash_fii)}, DII cash {money_cr(cash_dii)}",
        ),
        build_check("Fresh OI pressure", oi_score, f"Put-call OI change pressure {oi_shift:+.1f}% and PCR {pcr_value:.2f}"),
        build_check("Wall proximity", wall_score, wall_reading),
        build_check("Volatility filter", volatility_score, f"India VIX {vix:.2f}" if vix else "India VIX unavailable"),
    ]

    final_score = weighted(
        [
            (fii_score, 0.25),
            (pro_score, 0.20),
            (client_contra_score, 0.15),
            (cash_score, 0.14),
            (oi_score, 0.14),
            (wall_score, 0.08),
            (volatility_score, 0.04),
        ]
    )

    traps: list[dict[str, str]] = []
    if fii_direction > 5 and pro_direction > 3 and client_direction < -3:
        traps.append(
            {
                "type": "Retail short trap",
                "side": "Bullish",
                "reading": "FII and Pro are net positive while Clients are net short; upside squeeze risk is elevated.",
            }
        )
    if fii_direction < -5 and pro_direction < -3 and client_direction > 3:
        traps.append(
            {
                "type": "Retail long trap",
                "side": "Bearish",
                "reading": "FII and Pro are net negative while Clients are net long; downside flush risk is elevated.",
            }
        )
    if safe_float(pro.get("callWritingDelta")) > 0 and safe_float(client.get("callLong")) > safe_float(client.get("putLong")):
        traps.append(
            {
                "type": "Call buying trap",
                "side": "Bearish",
                "reading": "Pro call writing is rising while Clients carry more call longs than put longs.",
            }
        )
    if safe_float(pro.get("putWritingDelta")) > 0 and safe_float(client.get("putLong")) > safe_float(client.get("callLong")):
        traps.append(
            {
                "type": "Put buying trap",
                "side": "Bullish",
                "reading": "Pro put writing is rising while Clients carry more put longs than call longs.",
            }
        )
    if cash_fii < -1000 and fii_change > 3:
        traps.append(
            {
                "type": "Cash sell, derivatives hedge",
                "side": "Neutral",
                "reading": "FII cash is negative but derivative tilt improved; avoid reading cash selling alone.",
            }
        )
    if not traps:
        traps.append(
            {
                "type": "No obvious crowd trap",
                "side": "Neutral",
                "reading": "Participant groups are not cleanly opposite yet; wait for stronger divergence.",
            }
        )

    if final_score >= 62:
        prediction = "Next session bias is positive; prefer buy-on-dip until the put base fails."
    elif final_score <= 38:
        prediction = "Next session bias is negative; prefer sell-on-rise until the call wall is reclaimed."
    else:
        prediction = "Next session edge is mixed; trade the range and wait for FII/Pro alignment."

    matrix = []
    for name in ("fii", "pro", "client", "dii"):
        item = details.get(name, {})
        matrix.append(
            {
                "name": name.upper(),
                "directionalPct": item.get("directionalPct", 0),
                "directionalDeltaPct": item.get("directionalDeltaPct", 0),
                "futureNet": item.get("futureNet", 0),
                "futureNetDelta": item.get("futureNetDelta", 0),
                "callWritingDelta": item.get("callWritingDelta", 0),
                "putWritingDelta": item.get("putWritingDelta", 0),
                "longShortRatio": item.get("longShortRatio", 0),
            }
        )

    return {
        "score": round(final_score, 1),
        "bias": bias_label(final_score),
        "action": action_label(final_score),
        "prediction": prediction,
        "date": participant.get("date"),
        "previousDate": participant.get("previousDate"),
        "checks": checks,
        "traps": traps,
        "matrix": matrix,
        "sourceNote": "Modeled from NSE participant-wise OI current day versus previous trading day.",
    }


def bias_label(score: float) -> str:
    if score >= 70:
        return "Strong Bullish"
    if score >= 58:
        return "Bullish"
    if score <= 30:
        return "Strong Bearish"
    if score <= 42:
        return "Bearish"
    return "Neutral"


def action_label(score: float) -> str:
    if score >= 70:
        return "BUY BIAS"
    if score >= 58:
        return "BUY DIPS"
    if score <= 30:
        return "SELL BIAS"
    if score <= 42:
        return "SELL RISES"
    return "WAIT / RANGE"


def confidence_score(sources: dict[str, bool], scores: list[float], vix: float) -> int:
    score = 35
    score += sum(10 for ok in sources.values() if ok)
    if scores:
        avg = sum(scores) / len(scores)
        agreement = sum(1 for item in scores if (item >= 55 and avg >= 55) or (item <= 45 and avg <= 45) or (45 < item < 55 and 45 < avg < 55))
        score += agreement * 4
    if vix > 21:
        score -= 12
    elif vix > 17:
        score -= 6
    return int(clamp(score, 20, 92))


def build_horizon(
    name: str,
    option_view: dict[str, Any],
    idx_score: dict[str, Any],
    cash_score_data: dict[str, Any],
    part_score_data: dict[str, Any],
) -> dict[str, Any]:
    oi = score_oi_view(option_view)
    if name == "daily":
        score = weighted([(oi["score"], 0.42), (cash_score_data["score"], 0.26), (idx_score["score"], 0.22), (part_score_data["score"], 0.10)])
    elif name == "weekly":
        score = weighted([(oi["score"], 0.46), (part_score_data["score"], 0.26), (cash_score_data["score"], 0.14), (idx_score["score"], 0.14)])
    else:
        score = weighted([(oi["score"], 0.34), (part_score_data["score"], 0.36), (cash_score_data["score"], 0.16), (idx_score["score"], 0.14)])
    return {
        "name": name,
        "score": round(score, 1),
        "bias": bias_label(score),
        "action": action_label(score),
        "expiry": option_view.get("expiry"),
        "levels": {
            "support": option_view.get("putBase"),
            "resistance": option_view.get("callWall"),
            "maxPain": option_view.get("maxPain"),
            "expectedLow": option_view.get("expectedLow"),
            "expectedHigh": option_view.get("expectedHigh"),
        },
        "components": {
            "oi": oi,
            "cash": cash_score_data,
            "participants": part_score_data,
            "trend": idx_score,
        },
        "drivers": (oi["drivers"] + cash_score_data["drivers"][:1] + part_score_data["drivers"][:2])[:6],
    }


def load_history() -> list[dict[str, Any]]:
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(snapshot: dict[str, Any]) -> None:
    history = load_history()
    if history and history[-1].get("epoch", 0) > time.time() - 600:
        history[-1] = snapshot
    else:
        history.append(snapshot)
    SNAPSHOT_FILE.write_text(json.dumps(history[-1200:], separators=(",", ":")), encoding="utf-8")


def history_summary() -> dict[str, Any]:
    history = load_history()
    if len(history) < 2:
        return {"available": False, "message": "Trend memory starts after the app collects a few snapshots."}
    latest = history[-1]
    windows = {"day": 24, "week": 24 * 5, "month": 24 * 22}
    output: dict[str, Any] = {"available": True}
    for name, hours in windows.items():
        cutoff = latest["epoch"] - hours * 3600
        base = next((item for item in history if item.get("epoch", 0) >= cutoff), history[0])
        output[name] = {
            "scoreChange": round(latest.get("finalScore", 50) - base.get("finalScore", 50), 1),
            "spotChange": round(latest.get("spot", 0) - base.get("spot", 0), 1),
            "pcrChange": round(latest.get("pcr", 0) - base.get("pcr", 0), 2),
        }
    return output


def build_conclusion(
    indices: dict[str, Any],
    options: dict[str, Any],
    flows: dict[str, Any],
    participant: dict[str, Any],
) -> dict[str, Any]:
    idx = index_score(indices)
    cash = cash_flow_score(flows)
    part = participant_score(participant)
    option_views = options.get("views", {})
    horizons = {
        name: build_horizon(name, option_views.get(name, {}), idx, cash, part)
        for name in ("daily", "weekly", "monthly")
    }
    excel_model = build_big_player_model(indices, options, flows, participant, part)
    final_score = weighted(
        [
            (horizons["daily"]["score"], 0.36),
            (horizons["weekly"]["score"], 0.28),
            (horizons["monthly"]["score"], 0.16),
            (excel_model["score"], 0.20),
        ]
    )
    daily_levels = horizons["daily"]["levels"]
    spot = safe_float(options.get("spot") or indices.get("nifty", {}).get("price"))
    confidence = confidence_score(
        {
            "indices": bool(indices.get("nifty")),
            "optionChain": bool(option_views.get("daily", {}).get("available")),
            "cashFlow": bool(flows.get("fii") or flows.get("dii")),
            "participantOI": bool(participant.get("rows")),
        },
        [idx["score"], cash["score"], part["score"], *(h["score"] for h in horizons.values())],
        safe_float(indices.get("vix", {}).get("price")),
    )
    action = action_label(final_score)
    support = daily_levels.get("support")
    resistance = daily_levels.get("resistance")
    max_pain = daily_levels.get("maxPain")

    if final_score >= 58:
        plan = (
            f"Prefer long setups or buy dips while NIFTY holds above {support or 'nearest put base'}. "
            f"First supply zone is {resistance or 'nearest call wall'}; book faster if spot stalls there."
        )
        invalidation = f"Bias weakens below {support or 'put base'} or if fresh call writing overtakes put writing."
    elif final_score <= 42:
        plan = (
            f"Prefer short setups or sell rises while NIFTY stays below {resistance or 'nearest call wall'}. "
            f"First demand zone is {support or 'nearest put base'}; avoid pressing shorts into that support."
        )
        invalidation = f"Bias weakens above {resistance or 'call wall'} or if FIIs turn cash/derivatives net long."
    else:
        plan = (
            f"No clean directional edge. Treat {support or 'support'} to {resistance or 'resistance'} as the active range "
            f"and wait for a decisive break with OI follow-through."
        )
        invalidation = "Range view ends when spot breaks a wall and OI change confirms the move."

    if max_pain:
        hidden = f"Expiry gravity sits near {max_pain}; price can be pulled toward it unless institutions overpower the options book."
    else:
        hidden = "Expiry gravity is unavailable; rely more on cash flow and participant positioning."

    snapshot = {
        "epoch": time.time(),
        "asOf": ist_stamp(),
        "spot": spot,
        "finalScore": final_score,
        "pcr": safe_float(option_views.get("daily", {}).get("pcr")),
    }
    try:
        save_history(snapshot)
    except Exception:
        pass

    return {
        "score": final_score,
        "bias": bias_label(final_score),
        "action": action,
        "confidence": confidence,
        "plan": plan,
        "invalidation": invalidation,
        "hiddenStory": hidden,
        "spot": spot,
        "horizons": horizons,
        "excelModel": excel_model,
        "componentScores": {"trend": idx, "cash": cash, "participants": part},
        "history": history_summary(),
    }


def build_dashboard_payload() -> dict[str, Any]:
    indices = cached("indices", CACHE_SEC, fetch_indices)
    flows = cached("fii_dii", CACHE_SEC * 4, fetch_fii_dii)
    options = cached("option_chain", CACHE_SEC, fetch_option_chain)
    participant = cached("participant_oi", 900, fetch_participant_oi)
    deals = cached("deals", 300, fetch_deals)

    if isinstance(indices, dict) and "fallback" in indices and indices.get("fallback"):
        indices = indices["fallback"]
    if isinstance(flows, dict) and "fallback" in flows and flows.get("fallback"):
        flows = flows["fallback"]
    if isinstance(options, dict) and "fallback" in options and options.get("fallback"):
        options = options["fallback"]
    if isinstance(participant, dict) and "fallback" in participant and participant.get("fallback"):
        participant = participant["fallback"]
    if isinstance(deals, dict) and "fallback" in deals and deals.get("fallback"):
        deals = deals["fallback"]

    conclusion = build_conclusion(indices or {}, options or {}, flows or {}, participant or {})
    return {
        "status": "ok",
        "asOf": ist_stamp(),
        "marketOpen": is_market_open(),
        "indices": indices or {},
        "fiiDii": flows or {},
        "optionChain": options or {},
        "participantOI": participant or {},
        "bulkDeals": deals or {},
        "conclusion": conclusion,
        "disclaimer": "Research dashboard only. It is not financial advice or an order recommendation.",
    }


TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NIFTY Smart Money Command Center</title>
<style>
:root {
  --bg: #101112;
  --panel: #17191c;
  --panel2: #202327;
  --line: #30343a;
  --text: #eff2f4;
  --muted: #a5acb5;
  --soft: #747d89;
  --green: #25c281;
  --red: #ee5b5b;
  --amber: #e8b14a;
  --cyan: #51b8d9;
  --violet: #a98df0;
  --white: #ffffff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
}
button, input { font: inherit; }
.shell { max-width: 1440px; margin: 0 auto; padding: 18px 18px 34px; }
.topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
  padding: 14px 0 18px;
  border-bottom: 1px solid var(--line);
}
.brand { display: flex; flex-direction: column; gap: 3px; }
.brand h1 { margin: 0; font-size: 22px; letter-spacing: 0; font-weight: 760; }
.brand span { color: var(--muted); font-size: 12px; }
.controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.pill {
  display: inline-flex;
  align-items: center;
  min-height: 30px;
  padding: 5px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
}
.btn {
  min-height: 32px;
  border: 1px solid #3a444d;
  background: #242a2f;
  color: var(--text);
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
}
.btn:hover { border-color: var(--cyan); }
.grid { display: grid; gap: 12px; }
.g4 { grid-template-columns: 1.3fr 1fr 1fr 1fr; }
.g3 { grid-template-columns: repeat(3, 1fr); }
.g2 { grid-template-columns: 1.4fr 1fr; }
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 14px;
  min-width: 0;
}
.card.tight { padding: 12px; }
.label {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.2;
  text-transform: uppercase;
  letter-spacing: 0;
  margin-bottom: 8px;
}
.big { font-size: 36px; font-weight: 780; line-height: 1; letter-spacing: 0; }
.med { font-size: 22px; font-weight: 720; line-height: 1.05; }
.small { color: var(--muted); font-size: 12px; line-height: 1.45; }
.green { color: var(--green); }
.red { color: var(--red); }
.amber { color: var(--amber); }
.cyan { color: var(--cyan); }
.violet { color: var(--violet); }
.scoreband {
  height: 8px;
  border-radius: 4px;
  background: #2b2f34;
  overflow: hidden;
  margin-top: 11px;
}
.scorefill { height: 100%; width: 50%; background: var(--amber); transition: width .3s ease; }
.section { margin-top: 12px; }
.conclusion {
  display: grid;
  grid-template-columns: minmax(260px, .85fr) minmax(300px, 1.4fr);
  gap: 12px;
  margin-top: 14px;
}
.actionBox {
  border-left: 4px solid var(--amber);
  background: var(--panel);
}
.actionText { font-size: 34px; font-weight: 820; line-height: 1; margin-bottom: 8px; letter-spacing: 0; }
.plan { font-size: 14px; line-height: 1.6; color: var(--text); }
.horizon {
  display: grid;
  grid-template-columns: 110px 100px 1fr;
  gap: 12px;
  align-items: start;
  border-top: 1px solid var(--line);
  padding-top: 12px;
  margin-top: 12px;
}
.horizon:first-child { border-top: 0; padding-top: 0; margin-top: 0; }
.tag {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 74px;
  min-height: 26px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 4px 8px;
  background: var(--panel2);
  color: var(--muted);
  font-size: 12px;
}
.kv { display: grid; grid-template-columns: 1fr auto; gap: 8px; padding: 7px 0; border-bottom: 1px solid #25292e; }
.kv:last-child { border-bottom: 0; }
.kv span:first-child { color: var(--muted); }
.tablewrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
table { width: 100%; border-collapse: collapse; min-width: 820px; }
th, td { padding: 8px 9px; border-bottom: 1px solid #292d32; text-align: right; white-space: nowrap; }
th { color: var(--muted); background: #1d2024; font-size: 11px; font-weight: 650; text-transform: uppercase; }
td:first-child, th:first-child { text-align: left; }
tr:last-child td { border-bottom: 0; }
.atm td { background: rgba(232,177,74,.08); }
.drivers { margin: 8px 0 0; padding: 0; list-style: none; }
.drivers li { color: var(--muted); font-size: 12px; padding: 4px 0; line-height: 1.35; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.read-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.read-card { background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-width: 0; }
.read-title { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 7px; }
.read-title strong { color: var(--text); }
.read-card p { margin: 6px 0 0; color: var(--muted); font-size: 12px; line-height: 1.45; }
.guide { color: var(--soft); font-size: 11px; line-height: 1.5; margin: 8px 0 10px; }
.error { color: var(--red); }
.loading { color: var(--muted); padding: 20px; }
footer { color: var(--soft); font-size: 11px; line-height: 1.6; margin-top: 18px; border-top: 1px solid var(--line); padding-top: 14px; }
@media (max-width: 1080px) {
  .g4, .g3, .g2, .conclusion { grid-template-columns: 1fr 1fr; }
  .horizon { grid-template-columns: 1fr; }
  .read-grid { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 720px) {
  .shell { padding: 12px; }
  .topbar, .controls { align-items: stretch; }
  .topbar { flex-direction: column; }
  .controls { justify-content: stretch; }
  .btn, .pill { width: 100%; justify-content: center; }
  .g4, .g3, .g2, .conclusion, .split { grid-template-columns: 1fr; }
  .read-grid { grid-template-columns: 1fr; }
  .big { font-size: 30px; }
  .actionText { font-size: 28px; }
}
</style>
</head>
<body>
<main class="shell">
  <header class="topbar">
    <div class="brand">
      <h1>NIFTY Smart Money Command Center</h1>
      <span>FII/DII cash, participant OI, option walls, OI change, max pain, volatility and final bias</span>
    </div>
    <div class="controls">
      <span class="pill" id="marketState">Checking market</span>
      <span class="pill" id="asOf">Loading</span>
      <button class="btn" id="refreshBtn" type="button">Refresh</button>
    </div>
  </header>

  <section class="conclusion">
    <div class="card actionBox" id="actionBox">
      <div class="label">Final conclusion</div>
      <div class="actionText" id="actionText">Loading</div>
      <div class="med" id="biasText">--</div>
      <div class="scoreband"><div class="scorefill" id="scoreFill"></div></div>
      <div class="small" style="margin-top:9px" id="scoreMeta">Score -- / 100</div>
    </div>
    <div class="card">
      <div class="label">Hidden story and trade posture</div>
      <div class="plan" id="planText">Fetching NSE data and building the institutional map.</div>
      <div class="split section">
        <div class="card tight">
          <div class="label">Invalidation</div>
          <div class="small" id="invalidText">--</div>
        </div>
        <div class="card tight">
          <div class="label">Expiry gravity</div>
          <div class="small" id="storyText">--</div>
        </div>
      </div>
    </div>
  </section>

  <section class="grid g4 section">
    <div class="card">
      <div class="label">NIFTY spot</div>
      <div class="big" id="niftySpot">--</div>
      <div class="small" id="niftyChange">--</div>
    </div>
    <div class="card">
      <div class="label">Daily OI pressure</div>
      <div class="big" id="dailyPCR">--</div>
      <div class="small" id="dailyOI">--</div>
    </div>
    <div class="card">
      <div class="label">FII cash</div>
      <div class="big" id="fiiCash">--</div>
      <div class="small" id="diiCash">DII --</div>
    </div>
    <div class="card">
      <div class="label">Confidence</div>
      <div class="big" id="confidence">--</div>
      <div class="small" id="dataQuality">Live data quality</div>
    </div>
  </section>

  <section class="grid g3 section" id="horizonCards"></section>

  <section class="grid g2 section">
    <div class="card">
      <div class="label">Daily strike map near ATM</div>
      <div class="tablewrap">
        <table>
          <thead>
            <tr>
              <th>Strike</th><th>Call OI L</th><th>Call OI%</th><th>Call dOI L</th><th>Put dOI L</th><th>Put OI%</th><th>Put OI L</th><th>Signal</th>
            </tr>
          </thead>
          <tbody id="strikeRows"><tr><td colspan="8" class="loading">Loading option chain</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="label">Key levels</div>
      <div id="levelsPanel"></div>
      <div class="section">
        <div class="label">Index confirmation</div>
        <div id="indexPanel"></div>
      </div>
    </div>
  </section>

  <section class="grid g2 section">
    <div class="card">
      <div class="label">Participant OI by big players</div>
      <div id="participantPanel"></div>
    </div>
    <div class="card">
      <div class="label">Bulk / block deal tape</div>
      <div id="dealPanel"></div>
    </div>
  </section>

  <section class="grid g2 section">
    <div class="card">
      <div class="label">Excel-style next session model</div>
      <div id="excelPanel"></div>
    </div>
    <div class="card">
      <div class="label">Trap detector</div>
      <div id="trapPanel"></div>
    </div>
  </section>

  <section class="card section">
    <div class="label">FII / PRO / CLIENT / DII delta matrix</div>
    <div class="guide">
      Directional % shows current net bias. 1D Change shows whether that bias improved or worsened.
      Call Writing d above zero usually creates resistance; Put Writing d above zero usually creates support.
      Client data is treated with a contrarian lens when it sharply disagrees with FII/Pro.
    </div>
    <div id="playerReadPanel" class="read-grid"></div>
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th>Player</th><th>Directional %</th><th>1D Change</th><th>Future Net</th><th>Future Net d</th><th>Call Writing d</th><th>Put Writing d</th><th>L/S</th>
          </tr>
        </thead>
        <tbody id="playerMatrix"><tr><td colspan="8" class="loading">Loading participant matrix</td></tr></tbody>
      </table>
    </div>
  </section>

  <footer>
    Research dashboard only. This is not financial advice, investment advice, or a recommendation to buy or sell.
    NSE public APIs can be delayed or temporarily unavailable; verify critical data on NSE before acting.
  </footer>
</main>

<script>
const fmt = (n, d=2) => Number(n || 0).toLocaleString("en-IN", {maximumFractionDigits:d});
const signed = (n, d=2) => `${Number(n || 0) >= 0 ? "+" : ""}${fmt(n, d)}`;
const cr = n => `${Number(n || 0) >= 0 ? "+" : "-"}Rs.${Math.abs(Number(n || 0)).toLocaleString("en-IN", {maximumFractionDigits:0})} Cr`;
const cls = v => Number(v || 0) >= 0 ? "green" : "red";
const tone = score => score >= 58 ? "green" : score <= 42 ? "red" : "amber";
const fillColor = score => score >= 58 ? "var(--green)" : score <= 42 ? "var(--red)" : "var(--amber)";
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
}[ch]));

function kv(label, value, klass="") {
  return `<div class="kv"><span>${esc(label)}</span><strong class="${klass}">${esc(value)}</strong></div>`;
}

function driverList(items) {
  return `<ul class="drivers">${(items || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
}

async function refresh() {
  const btn = document.getElementById("refreshBtn");
  btn.disabled = true;
  btn.textContent = "Refreshing";
  try {
    const res = await fetch("/api/dashboard", {cache:"no-store"});
    const data = await res.json();
    render(data);
  } catch (err) {
    document.getElementById("planText").innerHTML = `<span class="error">Could not load dashboard: ${err}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Refresh";
  }
}

function render(data) {
  const c = data.conclusion || {};
  const indices = data.indices || {};
  const nifty = indices.nifty || {};
  const daily = data.optionChain?.views?.daily || {};
  const flows = data.fiiDii || {};
  const fii = flows.fii || {};
  const dii = flows.dii || {};

  document.getElementById("marketState").textContent = data.marketOpen ? "Market open" : "Market closed";
  document.getElementById("asOf").textContent = data.asOf || "--";
  document.getElementById("actionText").textContent = c.action || "--";
  document.getElementById("biasText").textContent = c.bias || "--";
  document.getElementById("biasText").className = `med ${tone(c.score)}`;
  document.getElementById("scoreFill").style.width = `${Math.max(0, Math.min(100, c.score || 0))}%`;
  document.getElementById("scoreFill").style.background = fillColor(c.score || 50);
  document.getElementById("scoreMeta").textContent = `Score ${fmt(c.score, 1)} / 100 with ${c.confidence || "--"}% confidence`;
  document.getElementById("planText").textContent = c.plan || "--";
  document.getElementById("invalidText").textContent = c.invalidation || "--";
  document.getElementById("storyText").textContent = c.hiddenStory || "--";

  document.getElementById("niftySpot").textContent = fmt(nifty.price, 2);
  document.getElementById("niftySpot").className = `big ${cls(nifty.change)}`;
  document.getElementById("niftyChange").textContent = `${signed(nifty.change, 2)} (${signed(nifty.changePct, 2)}%) | Breadth ${fmt(indices.breadthPct, 1)}%`;
  document.getElementById("dailyPCR").textContent = daily.pcr ? fmt(daily.pcr, 2) : "--";
  document.getElementById("dailyPCR").className = `big ${tone(c.horizons?.daily?.score || 50)}`;
  document.getElementById("dailyOI").textContent = `Put dOI ${signed(daily.totalPutChg, 1)}L vs Call dOI ${signed(daily.totalCallChg, 1)}L`;
  document.getElementById("fiiCash").textContent = cr(fii.net);
  document.getElementById("fiiCash").className = `big ${cls(fii.net)}`;
  document.getElementById("diiCash").textContent = `DII ${cr(dii.net)}`;
  document.getElementById("confidence").textContent = `${c.confidence || "--"}%`;
  document.getElementById("dataQuality").textContent = data.participantOI?.rows ? `Participant OI date ${data.participantOI.date}` : "Participant OI unavailable";

  renderHorizons(c.horizons || {});
  renderStrikes(daily);
  renderLevels(daily);
  renderIndexPanel(indices);
  renderParticipants(data.participantOI || {}, c.componentScores?.participants || {});
  renderDeals(data.bulkDeals || {});
  renderExcelModel(c.excelModel || {});
}

function renderHorizons(horizons) {
  const order = ["daily", "weekly", "monthly"];
  document.getElementById("horizonCards").innerHTML = order.map(name => {
    const h = horizons[name] || {};
    const levels = h.levels || {};
    return `<div class="card">
      <div class="label">${esc(name)} view ${h.expiry ? `<span class="tag">${esc(h.expiry)}</span>` : ""}</div>
      <div class="med ${tone(h.score || 50)}">${esc(h.action || "--")}</div>
      <div class="scoreband"><div class="scorefill" style="width:${h.score || 0}%;background:${fillColor(h.score || 50)}"></div></div>
      <div class="small" style="margin-top:8px">Score ${fmt(h.score,1)} | ${esc(h.bias || "--")}</div>
      <div class="section">
        ${kv("Support", levels.support ? fmt(levels.support,0) : "--", "green")}
        ${kv("Resistance", levels.resistance ? fmt(levels.resistance,0) : "--", "red")}
        ${kv("Max pain", levels.maxPain ? fmt(levels.maxPain,0) : "--", "amber")}
      </div>
      ${driverList(h.drivers)}
    </div>`;
  }).join("");
}

function renderStrikes(view) {
  const atm = view.atm;
  const rows = (view.strikes || []).slice().sort((a,b) => a.strike - b.strike);
  if (!rows.length) {
    document.getElementById("strikeRows").innerHTML = `<tr><td colspan="8" class="loading">No strike data available</td></tr>`;
    return;
  }
  document.getElementById("strikeRows").innerHTML = rows.map(row => {
    const signal = row.strike === view.callWall ? "Call wall" : row.strike === view.putBase ? "Put base" : row.strike === atm ? "ATM" : "";
    const tr = row.strike === atm ? " class='atm'" : "";
    return `<tr${tr}>
      <td><strong>${fmt(row.strike,0)}</strong></td>
      <td class="red">${fmt(row.callOI,2)}</td>
      <td class="${cls(row.callChgPct)}">${signed(row.callChgPct,1)}%</td>
      <td class="${cls(row.callChg)}">${signed(row.callChg,2)}</td>
      <td class="${cls(row.putChg)}">${signed(row.putChg,2)}</td>
      <td class="${cls(row.putChgPct)}">${signed(row.putChgPct,1)}%</td>
      <td class="green">${fmt(row.putOI,2)}</td>
      <td>${esc(signal)}</td>
    </tr>`;
  }).join("");
}

function renderLevels(view) {
  document.getElementById("levelsPanel").innerHTML = [
    kv("Spot", view.spot ? fmt(view.spot, 2) : "--"),
    kv("ATM", view.atm ? fmt(view.atm, 0) : "--", "amber"),
    kv("Put base", view.putBase ? fmt(view.putBase, 0) : "--", "green"),
    kv("Call wall", view.callWall ? fmt(view.callWall, 0) : "--", "red"),
    kv("Max pain", view.maxPain ? fmt(view.maxPain, 0) : "--", "amber"),
    kv("Expected range", view.expectedLow ? `${fmt(view.expectedLow,0)} - ${fmt(view.expectedHigh,0)}` : "--", "cyan"),
    kv("ATM IV", view.avgATMIV ? `${fmt(view.avgATMIV,1)}%` : "--"),
  ].join("");
}

function renderIndexPanel(indices) {
  const items = [
    ["Bank Nifty", indices.banknifty],
    ["Midcap 100", indices.midcap],
    ["Next 50", indices.next50],
    ["India VIX", indices.vix],
  ];
  document.getElementById("indexPanel").innerHTML = items.map(([name, row]) => {
    if (!row) return kv(name, "--");
    return kv(name, `${fmt(row.price, row.name === "INDIA VIX" ? 2 : 0)} (${signed(row.changePct,2)}%)`, cls(row.changePct));
  }).join("");
}

function renderParticipants(participant, scoreData) {
  const details = scoreData.details || {};
  const names = ["fii", "pro", "client", "dii"];
  if (!Object.keys(details).length) {
    document.getElementById("participantPanel").innerHTML = `<div class="small">Participant OI archive not available right now.</div>${driverList(scoreData.drivers)}`;
    return;
  }
  document.getElementById("participantPanel").innerHTML = `
    ${names.map(name => {
      const d = details[name] || {};
      return `<div class="horizon">
        <span class="tag">${esc(name.toUpperCase())}</span>
        <div class="med ${tone((d.directionalPct || 0) + 50)}">${signed(d.directionalPct,1)}%</div>
        <div>
          ${kv("Index futures net", fmt(d.futureNet,0), cls(d.futureNet))}
          ${kv("Fut long/short", fmt(d.longShortRatio,2))}
          ${kv("Option directional net", fmt(d.optionNet,0), cls(d.optionNet))}
        </div>
      </div>`;
    }).join("")}
    ${driverList(scoreData.drivers)}
  `;
}

function renderDeals(data) {
  const deals = (data.deals || []).filter(x => x.stock && x.stock !== "N/A").slice(0, 8);
  if (!deals.length) {
    document.getElementById("dealPanel").innerHTML = `<div class="small">No current bulk/block deal tape returned by NSE.</div>`;
    return;
  }
  document.getElementById("dealPanel").innerHTML = deals.map(d => `
    <div class="kv">
      <span><strong>${esc(d.stock)}</strong><br><small>${esc(d.name)}${d.block ? " | block" : ""}</small></span>
      <strong class="${d.side === "BUY" ? "green" : "red"}">${esc(d.side)} Rs.${fmt(d.valueCr,1)} Cr</strong>
    </div>
  `).join("");
}

function rowByName(matrix, name) {
  return (matrix || []).find(row => String(row.name || "").toUpperCase() === name) || {};
}

function playerStance(row) {
  const dir = Number(row.directionalPct || 0);
  const chg = Number(row.directionalDeltaPct || 0);
  const side = dir > 8 ? "net bullish" : dir < -8 ? "net bearish" : "mixed";
  const move = chg > 2 ? "improving" : chg < -2 ? "deteriorating" : "steady";
  return `${side}, ${move}`;
}

function optionRead(row) {
  const callWrite = Number(row.callWritingDelta || 0);
  const putWrite = Number(row.putWritingDelta || 0);
  if (callWrite > 0 && putWrite > 0) {
    return callWrite > putWrite
      ? "writing both sides, but calls more; resistance is heavier"
      : "writing both sides, but puts more; support is stronger";
  }
  if (callWrite < 0 && putWrite > 0) return "reducing call pressure and adding put support";
  if (callWrite > 0 && putWrite < 0) return "adding call pressure and reducing put support";
  if (callWrite < 0 && putWrite < 0) return "reducing option writing; option buyers/hedges are active";
  return "options change is not decisive";
}

function playerMeaning(row, allRows) {
  const name = String(row.name || "").toUpperCase();
  const dir = Number(row.directionalPct || 0);
  const fut = Number(row.futureNet || 0);
  const fii = rowByName(allRows, "FII");
  const pro = rowByName(allRows, "PRO");
  if (name === "FII") {
    if (dir < -8 && fut < 0) return "Foreign institutions are still carrying a short/bearish book. Rallies need FII short covering to sustain.";
    if (dir > 8 && fut > 0) return "Foreign institutions are carrying a long/bullish book. Dips have better odds of being bought.";
    return "FII book is hedged or mixed. Do not read one column alone; wait for futures plus options to align.";
  }
  if (name === "PRO") {
    return "Pro desks are expiry-sensitive. Their call/put writing often marks near-term resistance/support.";
  }
  if (name === "CLIENT") {
    const fiiDir = Number(fii.directionalPct || 0);
    const proDir = Number(pro.directionalPct || 0);
    if (dir > 5 && (fiiDir < -5 || proDir < -5)) return "Clients are long while smart money is weak; this can become a retail long trap.";
    if (dir < -5 && (fiiDir > 5 || proDir > 5)) return "Clients are short while smart money is strong; upside squeeze risk rises.";
    return "Client positioning is useful mainly as a crowd/contrarian signal.";
  }
  if (name === "DII") {
    return "DII derivative data is supportive context, but slower than FII/Pro for short-term NIFTY direction.";
  }
  return "No interpretation available.";
}

function playerAction(row, allRows) {
  const name = String(row.name || "").toUpperCase();
  const dir = Number(row.directionalPct || 0);
  const futDelta = Number(row.futureNetDelta || 0);
  const callWrite = Number(row.callWritingDelta || 0);
  const putWrite = Number(row.putWritingDelta || 0);
  const fii = rowByName(allRows, "FII");
  const pro = rowByName(allRows, "PRO");
  if (name === "FII") {
    if (dir < -8 && futDelta < 0) return "Action: avoid aggressive longs; sell-rise bias until FII shorts reduce.";
    if (dir < -8 && putWrite > 0 && callWrite < 0) return "Action: bearish carry remains, but downside may pause near put support.";
    if (dir > 8) return "Action: buy-dip bias while FII stays net long.";
    return "Action: wait for clearer FII alignment.";
  }
  if (name === "PRO") {
    if (callWrite > putWrite && callWrite > 0) return "Action: respect resistance/call wall; upside may be capped.";
    if (putWrite > callWrite && putWrite > 0) return "Action: respect support/put base; dips may be defended.";
    return "Action: range trading is safer than chasing.";
  }
  if (name === "CLIENT") {
    const fiiDir = Number(fii.directionalPct || 0);
    const proDir = Number(pro.directionalPct || 0);
    if (dir > 5 && (fiiDir < -5 || proDir < -5)) return "Action: beware long trap; do not chase upside without FII/Pro confirmation.";
    if (dir < -5 && (fiiDir > 5 || proDir > 5)) return "Action: beware short trap; breakout can squeeze quickly.";
    return "Action: use as contrarian only when it opposes FII/Pro.";
  }
  if (name === "DII") {
    if (dir > 8) return "Action: background support is present, but confirm with FII/Pro before directional trades.";
    if (dir < -8) return "Action: background support is weak; reduce long conviction.";
    return "Action: secondary confirmation only.";
  }
  return "Action: no signal.";
}

function renderExcelModel(model) {
  const checks = model.checks || [];
  const traps = model.traps || [];
  const matrix = model.matrix || [];
  document.getElementById("excelPanel").innerHTML = `
    <div class="med ${tone(model.score || 50)}">${esc(model.action || "--")}</div>
    <div class="scoreband"><div class="scorefill" style="width:${model.score || 0}%;background:${fillColor(model.score || 50)}"></div></div>
    <div class="small" style="margin-top:8px">Excel model score ${fmt(model.score,1)} | ${esc(model.bias || "--")}</div>
    <div class="small" style="margin-top:8px">${esc(model.prediction || "--")}</div>
    <div class="small" style="margin-top:8px">OI date ${esc(model.date || "--")} vs ${esc(model.previousDate || "--")}</div>
    <div class="section">
      ${checks.map(ch => kv(`${ch.factor} (${ch.stance})`, `${fmt(ch.score,1)} - ${ch.reading}`, tone(ch.score))).join("")}
    </div>
  `;
  document.getElementById("trapPanel").innerHTML = traps.length
    ? traps.map(t => `
      <div class="kv">
        <span><strong>${esc(t.type)}</strong><br><small>${esc(t.reading)}</small></span>
        <strong class="${t.side === "Bullish" ? "green" : t.side === "Bearish" ? "red" : "amber"}">${esc(t.side)}</strong>
      </div>
    `).join("")
    : `<div class="small">No trap model output.</div>`;
  document.getElementById("playerReadPanel").innerHTML = matrix.length
    ? matrix.map(row => `
      <div class="read-card">
        <div class="read-title">
          <strong>${esc(row.name)}</strong>
          <span class="${tone(Number(row.directionalPct || 0) + 50)}">${esc(playerStance(row))}</span>
        </div>
        <p><strong>Doing:</strong> ${esc(optionRead(row))}</p>
        <p><strong>Meaning:</strong> ${esc(playerMeaning(row, matrix))}</p>
        <p><strong>${esc(playerAction(row, matrix))}</strong></p>
      </div>
    `).join("")
    : `<div class="small">Participant interpretation unavailable.</div>`;
  document.getElementById("playerMatrix").innerHTML = matrix.length
    ? matrix.map(row => `
      <tr>
        <td><strong>${esc(row.name)}</strong></td>
        <td class="${tone(Number(row.directionalPct || 0) + 50)}">${signed(row.directionalPct,1)}%</td>
        <td class="${tone(Number(row.directionalDeltaPct || 0) + 50)}">${signed(row.directionalDeltaPct,1)}%</td>
        <td class="${cls(row.futureNet)}">${fmt(row.futureNet,0)}</td>
        <td class="${cls(row.futureNetDelta)}">${signed(row.futureNetDelta,0)}</td>
        <td class="${cls(-Number(row.callWritingDelta || 0))}">${signed(row.callWritingDelta,0)}</td>
        <td class="${cls(row.putWritingDelta)}">${signed(row.putWritingDelta,0)}</td>
        <td>${fmt(row.longShortRatio,2)}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="8" class="loading">Participant deltas unavailable</td></tr>`;
}

document.getElementById("refreshBtn").addEventListener("click", refresh);
refresh();
setInterval(refresh, 15 * 60 * 1000);
</script>
</body>
</html>
"""


@app.get("/")
def index() -> str:
    return render_template_string(TEMPLATE)


@app.get("/api/status")
def api_status():
    return jsonify({"status": "ok", "marketOpen": is_market_open(), "serverTime": ist_stamp(), "nseReady": get_nse() is not None})


@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(build_dashboard_payload())


@app.get("/api/all")
def api_all():
    return jsonify(build_dashboard_payload())


@app.get("/api/indices")
def api_indices():
    return jsonify(cached("indices", CACHE_SEC, fetch_indices))


@app.get("/api/fii-dii")
def api_fii_dii():
    return jsonify(cached("fii_dii", CACHE_SEC * 4, fetch_fii_dii))


@app.get("/api/option-chain")
def api_option_chain():
    return jsonify(cached("option_chain", CACHE_SEC, fetch_option_chain))


@app.get("/api/participant-oi")
def api_participant_oi():
    return jsonify(cached("participant_oi", 900, fetch_participant_oi))


@app.get("/api/bulk-deals")
def api_bulk_deals():
    return jsonify(cached("deals", 300, fetch_deals))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
