import csv
import io
import json
import os
import re
import secrets
import time
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, render_template, request, make_response
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)
IST = ZoneInfo("Asia/Kolkata")

LOGIN_URL = "https://signin.definedgesecurities.com/auth/realms/debroking/dsbpkc/login/"
TOKEN_URL = "https://signin.definedgesecurities.com/auth/realms/debroking/dsbpkc/token"
OMS_TOKEN_URL = "https://integrate.definedgesecurities.com/dart/v1/token"
API_BASE = "https://integrate.definedgesecurities.com/dart/v1"
HISTORY_BASE = "https://data.definedgesecurities.com/sds/history"
NFO_MASTER_URL = "https://app.definedgesecurities.com/public/nsefno.zip"
NSE_MASTER_URL = "https://app.definedgesecurities.com/public/nsecash.zip"

API_TOKEN = os.getenv("DEFINEDGE_API_TOKEN", "").strip()
API_SECRET = os.getenv("DEFINEDGE_API_SECRET", "").strip()
CLIENT_SECRET = os.getenv("DEFINEDGE_CLIENT_SECRET", "").strip() or API_SECRET
COLLECTOR_PASSWORD = os.getenv("COLLECTOR_PASSWORD", "").strip()

OTP_STATES = {}
OTP_TTL = 10 * 60
MAX_TRADES = 40
BROKER_MATCH_TOLERANCE_SECONDS = 180

ALIASES = {
    "segment": ["segment", "exchange", "exch", "exch_seg"],
    "token": ["token", "symboltoken", "instrument_token", "instrumenttoken"],
    "tradingsymbol": ["tradingsymbol", "trading_symbol", "contract", "option_symbol"],
    "underlying": ["underlying", "underlying_symbol", "stock", "symbol", "scrip"],
    "expiry": ["expiry", "expiry_date", "expdate", "expiration"],
    "strike": ["strike", "strike_price", "strikeprice"],
    "optiontype": ["optiontype", "option_type", "cp", "right", "ce_pe", "type"],
    "strategy": ["strategy", "strategy_name", "algo", "system"],
    "entry_side": ["entry_side", "side", "order_type", "entry_order_type"],
}

ENTRY_TIME_KEYS = [
    "entry_time", "entrytime", "entry datetime", "entry_datetime",
    "entry date time", "entry time 1", "entrytime1", "buy_time",
    "fill_time", "exchange_time", "time"
]
EXIT_TIME_KEYS = [
    "exit_time", "exittime", "exit datetime", "exit_datetime",
    "exit date time", "exit time 1", "exittime1", "close_time"
]

def http():
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20))
    s.headers.update({"User-Agent": "DefinedgeMomentumCollector/2.0"})
    return s

HTTP = http()

def norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())

def require_config():
    missing = []
    if not API_TOKEN:
        missing.append("DEFINEDGE_API_TOKEN")
    if not API_SECRET:
        missing.append("DEFINEDGE_API_SECRET")
    if missing:
        raise RuntimeError("Missing Render environment variable(s): " + ", ".join(missing))

def require_password():
    if not COLLECTOR_PASSWORD:
        return None
    supplied = request.form.get("collector_password", "") or request.args.get("collector_password", "")
    if supplied != COLLECTOR_PASSWORD:
        raise RuntimeError("Collector password is incorrect.")

def cleanup_states():
    now = time.time()
    for key in [k for k, v in OTP_STATES.items() if now - v["created"] > OTP_TTL]:
        OTP_STATES.pop(key, None)

def request_otp():
    require_config()
    r = HTTP.get(LOGIN_URL + API_TOKEN, headers={"api_secret": API_SECRET}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"OTP request failed ({r.status_code}): {r.text[:500]}")
    data = r.json()
    otp_token = data.get("otp_token")
    if not otp_token:
        raise RuntimeError("Definedge returned HTTP success but no otp_token.")
    cleanup_states()
    state_id = secrets.token_urlsafe(24)
    OTP_STATES[state_id] = {"otp_token": otp_token, "created": time.time()}
    return state_id

def _extract_session_key(data):
    if isinstance(data, dict) and data.get("api_session_key"):
        return data["api_session_key"], data
    if isinstance(data, dict) and data.get("access_token"):
        r = HTTP.post(OMS_TOKEN_URL, json=data, timeout=30)
        if r.ok:
            oms = r.json()
            if oms.get("api_session_key"):
                return oms["api_session_key"], oms
    return None, data

def authenticate(state_id, otp):
    cleanup_states()
    state = OTP_STATES.get(state_id)
    if not state:
        raise RuntimeError("OTP session expired. Click Send OTP again.")

    otp_token = state["otp_token"]

    # First use the minimal JSON flow that already worked in the user's GitHub test.
    r = HTTP.post(TOKEN_URL, json={"otp_token": otp_token, "otp": otp.strip()}, timeout=30)
    if r.ok:
        try:
            key, data = _extract_session_key(r.json())
            if key:
                OTP_STATES.pop(state_id, None)
                return key
        except Exception:
            pass

    # Fallback to the fuller form fields shown in Definedge's current documentation.
    payload = {
        "client_id": "TRTP",
        "grant_type": "password",
        "client_secret": CLIENT_SECRET,
        "otp_token": otp_token,
        "otp": otp.strip(),
    }
    r2 = HTTP.post(TOKEN_URL, data=payload, timeout=30)
    if not r2.ok:
        raise RuntimeError(
            f"Definedge OTP authentication failed. "
            f"JSON attempt={r.status_code}; form attempt={r2.status_code}: {r2.text[:500]}"
        )
    key, data = _extract_session_key(r2.json())
    if not key:
        raise RuntimeError("Authentication response did not contain a usable api_session_key.")
    OTP_STATES.pop(state_id, None)
    return key

def broker_get(session_key, endpoint):
    r = HTTP.get(API_BASE + endpoint, headers={"Authorization": session_key}, timeout=30)
    if not r.ok:
        return {"_http_status": r.status_code, "_error": r.text[:2000]}
    try:
        return r.json()
    except Exception:
        return {"_http_status": r.status_code, "_raw_text": r.text}

def rows_from_payload(payload, preferred):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        val = payload.get(preferred)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            return [val]
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    return []

def payload_csv(payload, preferred):
    rows = rows_from_payload(payload, preferred)
    if not rows:
        return ""
    fields = []
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            row = {"value": row}
        out = {}
        for k, v in row.items():
            if k not in fields:
                fields.append(k)
            out[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        clean.append(out)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(clean)
    return buf.getvalue()

def download_master(url):
    r = HTTP.get(url, timeout=45)
    if not r.ok:
        raise RuntimeError(f"Master download failed ({r.status_code})")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError("Master ZIP did not contain a CSV.")
        return z.read(names[0]).decode("utf-8-sig", errors="replace")

def parse_master(raw):
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return []
    names = [
        "segment", "token", "symbol", "tradingsymbol", "instrument_type",
        "expiry", "ticksize", "lotsize", "optiontype", "strike_raw",
        "priceprec", "multiplier", "isin", "pricemult", "company"
    ]
    first = [norm(x) for x in rows[0]]
    start = 1 if first and ("segment" in first[0] or "token" in first[:2]) else 0
    out = []
    for row in rows[start:]:
        if len(row) < 4:
            continue
        row = row + [""] * max(0, len(names) - len(row))
        d = dict(zip(names, [x.strip() for x in row[:len(names)]]))
        try:
            raw_strike = float(d["strike_raw"] or 0)
            multiplier = float(d["multiplier"] or 1)
            priceprec = int(float(d["priceprec"] or 0))
            denom = multiplier * (10 ** priceprec)
            d["strike"] = raw_strike / denom if denom else raw_strike
        except Exception:
            d["strike"] = None
        out.append(d)
    return out

def pexp(value):
    if not value:
        return None
    s = str(value).strip().upper()
    for fmt in ("%d%m%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d%b%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None

def pdt(value, fallback_date=None):
    if not value:
        return None
    s = str(value).strip()
    for fmt in (
        "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt in ("%H:%M:%S", "%H:%M"):
                dt = datetime.combine(fallback_date or datetime.now(IST).date(), dt.time())
            return dt.replace(tzinfo=IST)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except Exception:
        return None

def otype(value):
    s = str(value or "").upper().strip()
    if "CE" in s or s in ("C", "CALL"):
        return "CE"
    if "PE" in s or s in ("P", "PUT"):
        return "PE"
    return s

def pick(row, aliases):
    n = {norm(k): v for k, v in row.items()}
    for a in aliases:
        k = norm(a)
        if k in n and str(n[k]).strip():
            return str(n[k]).strip()
    return ""

def canonical(row, key):
    return pick(row, ALIASES[key])

def read_trade_csv(fs):
    raw = fs.read()
    if not raw:
        raise RuntimeError("Uploaded trade log is empty.")
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8000], delimiters=",;\t")
    except Exception:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    if not rows:
        raise RuntimeError("No data rows found in uploaded CSV.")
    return rows, text

def expand_rows(rows):
    """
    Supports ordinary one-row-per-trade CSVs and common AlgoStra columns
    such as Entry Time 1 / Exit Time 1 and Entry Time 2 / Exit Time 2.
    """
    expanded = []
    for src_i, row in enumerate(rows, 1):
        n = {norm(k): k for k in row.keys()}
        emitted = False
        for leg in (1, 2):
            ek = n.get(norm(f"Entry Time {leg}"))
            xk = n.get(norm(f"Exit Time {leg}"))
            if ek and str(row.get(ek, "")).strip():
                r = dict(row)
                r["_source_row"] = src_i
                r["_leg"] = leg
                r["_entry_time_norm"] = str(row.get(ek, "")).strip()
                r["_exit_time_norm"] = str(row.get(xk, "")).strip() if xk else ""
                expanded.append(r)
                emitted = True
        if not emitted:
            r = dict(row)
            r["_source_row"] = src_i
            r["_leg"] = 1
            r["_entry_time_norm"] = pick(row, ENTRY_TIME_KEYS)
            r["_exit_time_norm"] = pick(row, EXIT_TIME_KEYS)
            expanded.append(r)
    return expanded

def resolve_exact(row, nfo):
    seg = (canonical(row, "segment") or "NFO").upper()
    token = canonical(row, "token")
    ts = canonical(row, "tradingsymbol").upper()

    if token:
        hits = [r for r in nfo if r["token"] == token and (not seg or r["segment"].upper() == seg)]
        if len(hits) == 1:
            return hits[0], "token", []

    if ts:
        hits = [r for r in nfo if r["tradingsymbol"].upper() == ts]
        if len(hits) == 1:
            return hits[0], "tradingsymbol", []

    underlying = canonical(row, "underlying").upper().replace("-EQ", "")
    expiry = pexp(canonical(row, "expiry"))
    opt = otype(canonical(row, "optiontype"))
    try:
        strike = float(canonical(row, "strike").replace(",", ""))
    except Exception:
        strike = None

    candidates = []
    for r in nfo:
        if underlying and r["symbol"].upper().replace("-EQ", "") != underlying:
            continue
        if expiry and pexp(r["expiry"]) != expiry:
            continue
        if opt and otype(r["optiontype"]) != opt:
            continue
        if strike is not None and r["strike"] is not None:
            if abs(float(r["strike"]) - strike) > max(0.01, abs(strike) * 0.0001):
                continue
        candidates.append(r)

    if len(candidates) == 1:
        return candidates[0], "components", []
    return None, "ambiguous" if candidates else "unresolved", candidates[:20]

def nfo_master_by_token(nfo):
    return {r["token"]: r for r in nfo}

def match_broker_trade(row, entry_dt, broker_trades, by_token):
    underlying = canonical(row, "underlying").upper().replace("-EQ", "")
    wanted_side = canonical(row, "entry_side").upper()
    candidates = []
    for t in broker_trades:
        tok = str(t.get("token", "")).strip()
        master = by_token.get(tok)
        if not master:
            continue
        if master.get("segment", "").upper() != "NFO":
            continue
        if underlying and master.get("symbol", "").upper().replace("-EQ", "") != underlying:
            continue
        if wanted_side in ("BUY", "SELL") and str(t.get("order_type", "")).upper() != wanted_side:
            continue
        tdt = pdt(t.get("fill_time") or t.get("exchange_time"))
        if not tdt:
            continue
        diff = abs((tdt - entry_dt).total_seconds())
        if diff <= BROKER_MATCH_TOLERANCE_SECONDS:
            candidates.append((diff, tok, master, t))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    best_diff = candidates[0][0]
    same_token = {c[1] for c in candidates if c[0] <= best_diff + 2}
    if len(same_token) != 1:
        return None, [{"seconds": c[0], "token": c[1], "tradingsymbol": c[2]["tradingsymbol"]} for c in candidates[:10]]
    best = candidates[0]
    return (best[2], best[3], best[0]), None

def resolve_underlying(symbol, nse):
    target = symbol.upper().replace("-EQ", "")
    candidates = [
        r for r in nse
        if target in (
            r["symbol"].upper().replace("-EQ", ""),
            r["tradingsymbol"].upper().replace("-EQ", "")
        )
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: 0 if r["tradingsymbol"].upper().endswith("-EQ") else 1)
    return candidates[0]

def history_raw(sk, segment, token, timeframe, start, end):
    frm = start.astimezone(IST).strftime("%d%m%Y%H%M")
    to = end.astimezone(IST).strftime("%d%m%Y%H%M")
    url = f"{HISTORY_BASE}/{segment}/{token}/{timeframe}/{frm}/{to}"
    r = HTTP.get(url, headers={"Authorization": sk}, timeout=45)
    if not r.ok:
        raise RuntimeError(f"History API {r.status_code}: {r.text[:500]}")
    if not r.text.strip():
        return ""
    return r.text

def parse_ticks(raw):
    result = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            cols = next(csv.reader([line]))
        except Exception:
            continue
        if len(cols) < 2:
            continue
        try:
            epoch = float(cols[0])
            ltp = float(cols[1])
        except Exception:
            continue
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(IST)
        result.append({
            "utc_seconds": cols[0],
            "timestamp_ist": dt.isoformat(),
            "_dt": dt,
            "ltp": ltp,
            "ltq": cols[2] if len(cols) > 2 else "",
            "open_interest": cols[3] if len(cols) > 3 else "",
        })
    return result

def ticks_csv(ticks):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["utc_seconds", "timestamp_ist", "ltp", "ltq", "open_interest"])
    w.writeheader()
    for r in ticks:
        w.writerow({k: r[k] for k in w.fieldnames})
    return buf.getvalue()

def minute_csv(raw):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["datetime", "open", "high", "low", "close", "volume", "open_interest"])
    for line in raw.splitlines():
        if not line.strip():
            continue
        cols = next(csv.reader([line]))
        w.writerow((cols + [""] * 7)[:7])
    return buf.getvalue()

def nearest_tick(ticks, dt):
    if not ticks:
        return None
    return min(ticks, key=lambda r: abs((r["_dt"] - dt).total_seconds()))

def metrics_for_trade(ticks, entry_dt, exit_dt, after_end, direction="LONG"):
    inside = [r for r in ticks if entry_dt <= r["_dt"] <= exit_dt]
    after = [r for r in ticks if exit_dt < r["_dt"] <= after_end]
    ent = nearest_tick(ticks, entry_dt)
    ext = nearest_tick(ticks, exit_dt)
    if not ent:
        return {}
    entry = ent["ltp"]
    exitp = ext["ltp"] if ext else None
    out = {
        "direction": direction,
        "entry_tick_time": ent["timestamp_ist"],
        "entry_tick_ltp": entry,
        "exit_tick_time": ext["timestamp_ist"] if ext else "",
        "exit_tick_ltp": exitp if exitp is not None else "",
    }
    if inside:
        hi = max(inside, key=lambda r: r["ltp"])
        lo = min(inside, key=lambda r: r["ltp"])
        out.update({
            "max_ltp_during_trade": hi["ltp"],
            "max_ltp_time": hi["timestamp_ist"],
            "min_ltp_during_trade": lo["ltp"],
            "min_ltp_time": lo["timestamp_ist"],
            "long_mfe_pct": round((hi["ltp"] / entry - 1) * 100, 4) if entry else "",
            "long_mae_pct": round((lo["ltp"] / entry - 1) * 100, 4) if entry else "",
            "short_mfe_pct": round((1 - lo["ltp"] / entry) * 100, 4) if entry else "",
            "short_mae_pct": round((1 - hi["ltp"] / entry) * 100, 4) if entry else "",
        })
        if exitp is not None and hi["ltp"]:
            out["exit_below_peak_pct"] = round((1 - exitp / hi["ltp"]) * 100, 4)
    if after:
        out["post_exit_max_ltp"] = max(r["ltp"] for r in after)
        out["post_exit_min_ltp"] = min(r["ltp"] for r in after)
    return out

def chain_contracts(contract, nfo, underlying_entry_ltp):
    expiry = pexp(contract["expiry"])
    symbol = contract["symbol"].upper()
    same_expiry_options = [
        r for r in nfo
        if r["symbol"].upper() == symbol
        and pexp(r["expiry"]) == expiry
        and otype(r["optiontype"]) in ("CE", "PE")
        and r["strike"] is not None
    ]
    strikes = sorted({float(r["strike"]) for r in same_expiry_options})
    if not strikes or underlying_entry_ltp is None:
        return []
    atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying_entry_ltp))
    chosen = strikes[max(0, atm_index - 1): min(len(strikes), atm_index + 2)]
    results = []
    for strike in chosen:
        for side in ("CE", "PE"):
            hits = [
                r for r in same_expiry_options
                if abs(float(r["strike"]) - strike) < 1e-9 and otype(r["optiontype"]) == side
            ]
            if hits:
                results.append(hits[0])
    return results

def safe(s):
    return (re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s)).strip("._") or "item")[:120]

def build_package(sk, fs, before_min, after_min, include_chain):
    raw_rows, original_text = read_trade_csv(fs)
    rows = expand_rows(raw_rows)
    if len(rows) > MAX_TRADES:
        raise RuntimeError(f"Trade log expands to {len(rows)} legs; maximum supported per run is {MAX_TRADES}.")

    # Broker context: extremely useful to identify the actual NFO token when AlgoStra log lacks strike/expiry.
    broker_payloads = {
        "orders": broker_get(sk, "/orders"),
        "trades": broker_get(sk, "/trades"),
        "positions": broker_get(sk, "/positions"),
        "limits": broker_get(sk, "/limits"),
    }
    broker_trades = rows_from_payload(broker_payloads["trades"], "trades")

    nfo_raw = download_master(NFO_MASTER_URL)
    nse_raw = download_master(NSE_MASTER_URL)
    nfo = parse_master(nfo_raw)
    nse = parse_master(nse_raw)
    by_token = nfo_master_by_token(nfo)

    mem = io.BytesIO()
    manifest = []
    unresolved = []
    metrics_rows = []
    history_cache = {}

    def get_history(segment, token, timeframe, start, end):
        key = (segment, token, timeframe, start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M"))
        if key not in history_cache:
            history_cache[key] = history_raw(sk, segment, token, timeframe, start, end)
        return history_cache[key]

    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("input/original_trade_log.csv", original_text)
        z.writestr("reference/nfo_master_snapshot.csv", nfo_raw)
        z.writestr("reference/nse_master_snapshot.csv", nse_raw)

        for name, payload in broker_payloads.items():
            z.writestr(f"broker/{name}.json", json.dumps(payload, indent=2, ensure_ascii=False))
            z.writestr(f"broker/{name}.csv", payload_csv(payload, name))

        for i, row in enumerate(rows, 1):
            entry_dt = pdt(row.get("_entry_time_norm", ""))
            exit_dt = pdt(row.get("_exit_time_norm", ""), entry_dt.date() if entry_dt else None)

            if not entry_dt:
                unresolved.append({
                    "expanded_row": i,
                    "source_row": row.get("_source_row"),
                    "leg": row.get("_leg"),
                    "reason": "Could not parse entry time",
                    "row_data": row,
                })
                continue
            if not exit_dt:
                exit_dt = entry_dt

            contract, method, candidates = resolve_exact(row, nfo)
            broker_match = None
            broker_ambiguity = None

            if contract is None:
                broker_match, broker_ambiguity = match_broker_trade(row, entry_dt, broker_trades, by_token)
                if broker_match:
                    contract, matched_trade, seconds = broker_match
                    method = "broker_trade_time_match"
                else:
                    unresolved.append({
                        "expanded_row": i,
                        "source_row": row.get("_source_row"),
                        "leg": row.get("_leg"),
                        "reason": "Exact option contract could not be resolved safely",
                        "component_candidates": [
                            {"token": c["token"], "tradingsymbol": c["tradingsymbol"], "strike": c["strike"], "optiontype": c["optiontype"], "expiry": c["expiry"]}
                            for c in candidates[:20]
                        ],
                        "broker_candidates": broker_ambiguity or [],
                        "row_data": row,
                    })
                    continue

            start = entry_dt - timedelta(minutes=before_min)
            end = exit_dt + timedelta(minutes=after_min)
            base = f"trades/{i:03d}_{safe(contract['tradingsymbol'])}"

            metadata = {
                "expanded_row": i,
                "source_row": row.get("_source_row"),
                "leg": row.get("_leg"),
                "resolution_method": method,
                "strategy": canonical(row, "strategy"),
                "segment": contract["segment"],
                "token": contract["token"],
                "symbol": contract["symbol"],
                "tradingsymbol": contract["tradingsymbol"],
                "expiry": contract["expiry"],
                "optiontype": contract["optiontype"],
                "strike": contract["strike"],
                "entry_time_ist": entry_dt.isoformat(),
                "exit_time_ist": exit_dt.isoformat(),
                "window_from_ist": start.isoformat(),
                "window_to_ist": end.isoformat(),
                "files": [],
            }

            if broker_match:
                _, matched_trade, seconds = broker_match
                metadata["broker_match_seconds"] = seconds
                metadata["broker_fill_price"] = matched_trade.get("fill_price", "")
                metadata["broker_order_type"] = matched_trade.get("order_type", "")
                metadata["broker_fill_time"] = matched_trade.get("fill_time", "")
                metadata["broker_order_id"] = matched_trade.get("order_id", "")

            # Exact option tick + minute data.
            option_ticks = []
            try:
                raw_tick = get_history(contract["segment"], contract["token"], "tick", start, end)
                option_ticks = parse_ticks(raw_tick)
                if not option_ticks:
                    raise RuntimeError("Tick endpoint returned no valid tick rows.")
                p = f"{base}/traded_option_ticks.csv"
                z.writestr(p, ticks_csv(option_ticks))
                metadata["files"].append(p)
            except Exception as e:
                metadata["traded_option_tick_error"] = str(e)

            try:
                raw_min = get_history(contract["segment"], contract["token"], "minute", start, end)
                p = f"{base}/traded_option_1min.csv"
                z.writestr(p, minute_csv(raw_min))
                metadata["files"].append(p)
            except Exception as e:
                metadata["traded_option_1min_error"] = str(e)

            # Underlying
            underlying_rec = resolve_underlying(contract["symbol"], nse)
            underlying_ticks = []
            underlying_entry_ltp = None
            if underlying_rec:
                try:
                    raw_tick = get_history(underlying_rec["segment"], underlying_rec["token"], "tick", start, end)
                    underlying_ticks = parse_ticks(raw_tick)
                    if underlying_ticks:
                        p = f"{base}/underlying_ticks.csv"
                        z.writestr(p, ticks_csv(underlying_ticks))
                        metadata["files"].append(p)
                        nt = nearest_tick(underlying_ticks, entry_dt)
                        if nt:
                            underlying_entry_ltp = nt["ltp"]
                            metadata["underlying_entry_ltp"] = underlying_entry_ltp
                except Exception as e:
                    metadata["underlying_tick_error"] = str(e)

                try:
                    raw_min = get_history(underlying_rec["segment"], underlying_rec["token"], "minute", start, end)
                    p = f"{base}/underlying_1min.csv"
                    z.writestr(p, minute_csv(raw_min))
                    metadata["files"].append(p)
                except Exception as e:
                    metadata["underlying_1min_error"] = str(e)

                metadata["underlying_token"] = underlying_rec["token"]
                metadata["underlying_tradingsymbol"] = underlying_rec["tradingsymbol"]

            # Metrics
            direction = "LONG"
            if metadata.get("broker_order_type", "").upper() == "SELL":
                direction = "SHORT"
            m = metrics_for_trade(option_ticks, entry_dt, exit_dt, end, direction)
            if m:
                metric_row = {
                    "expanded_row": i,
                    "source_row": row.get("_source_row"),
                    "leg": row.get("_leg"),
                    "strategy": canonical(row, "strategy"),
                    "tradingsymbol": contract["tradingsymbol"],
                    "token": contract["token"],
                    "underlying": contract["symbol"],
                    "expiry": contract["expiry"],
                    "strike": contract["strike"],
                    "optiontype": contract["optiontype"],
                    "resolution_method": method,
                    **m,
                }
                metrics_rows.append(metric_row)
                metadata["metrics"] = m

            # Entry-time ATM chain: ATM-1, ATM, ATM+1 for BOTH CE and PE.
            if include_chain and underlying_entry_ltp is not None:
                for chain_rec in chain_contracts(contract, nfo, underlying_entry_ltp):
                    try:
                        raw_tick = get_history(chain_rec["segment"], chain_rec["token"], "tick", start, end)
                        ticks = parse_ticks(raw_tick)
                        if not ticks:
                            continue
                        p = (
                            f"{base}/entry_atm_chain/"
                            f"{safe(chain_rec['tradingsymbol'])}_"
                            f"{chain_rec['strike']}_{otype(chain_rec['optiontype'])}_ticks.csv"
                        )
                        z.writestr(p, ticks_csv(ticks))
                        metadata["files"].append(p)
                    except Exception as e:
                        metadata.setdefault("chain_errors", []).append({
                            "tradingsymbol": chain_rec["tradingsymbol"],
                            "error": str(e),
                        })

            z.writestr(f"{base}/trade_metadata.json", json.dumps(metadata, indent=2, ensure_ascii=False))
            manifest.append(metadata)

        # Summary metrics CSV
        if metrics_rows:
            fields = []
            for r in metrics_rows:
                for k in r:
                    if k not in fields:
                        fields.append(k)
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=fields)
            w.writeheader()
            w.writerows(metrics_rows)
            z.writestr("analysis/trade_metrics.csv", buf.getvalue())
        else:
            z.writestr("analysis/trade_metrics.csv", "")

        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        z.writestr("unresolved_rows.json", json.dumps(unresolved, indent=2, ensure_ascii=False))
        z.writestr(
            "README.txt",
            """DEFINEDGE MOMENTUM TICK COLLECTOR V2

WHAT IT DOES
- Upload a momentum / AlgoStra trade-log CSV.
- Resolves the exact NFO option safely.
- If the log lacks strike/expiry/token, tries to reconcile against today's Definedge broker trade book by underlying and fill time.
- Never silently picks an ambiguous option contract.
- Downloads exact option tick data (LTP, LTQ, OI).
- Downloads exact option 1-minute bars as a backup.
- Downloads underlying tick + 1-minute data.
- Optionally downloads entry-time ATM-1 / ATM / ATM+1 for BOTH CE and PE.
- Saves broker orders/trades/positions/limits.
- Creates analysis/trade_metrics.csv with entry/exit tick, MFE, MAE, peak give-back and post-exit extremes.

IMPORTANT
- Definedge documents tick history as available for the last 2 trading sessions.
- Intraday minute history is documented as available for the last 6 months.
- Run this daily / by the next session and retain the ZIP.
- If a virtual AlgoStra trade has no exact contract details and no broker execution, it may remain unresolved. This is deliberate: the app will not guess.
- Personal-use only. Do not redistribute Definedge market data.
""",
        )

    mem.seek(0)
    stamp = datetime.now(IST).strftime("%Y-%m-%d_%H%M%S")
    return mem, f"Momentum_Tick_Data_V2_{stamp}.zip", len(manifest), len(unresolved)

@app.get("/")
def home():
    return render_template(
        "index.html",
        password_enabled=bool(COLLECTOR_PASSWORD),
    )

@app.post("/send-otp")
def send_otp_route():
    try:
        require_password()
        state_id = request_otp()
        resp = make_response(render_template(
            "index.html",
            message="OTP sent. Enter it below, attach the trade log and download.",
            otp_sent=True,
            password_enabled=bool(COLLECTOR_PASSWORD),
        ))
        resp.set_cookie(
            "de_state", state_id, max_age=OTP_TTL,
            httponly=True, secure=True, samesite="Lax"
        )
        return resp
    except Exception as e:
        return render_template(
            "index.html", error=str(e),
            password_enabled=bool(COLLECTOR_PASSWORD)
        ), 500

@app.post("/collect")
def collect_route():
    try:
        require_password()
        otp = request.form.get("otp", "").strip()
        fs = request.files.get("trade_log")
        if not otp:
            raise RuntimeError("Enter the Definedge OTP.")
        if not fs or not fs.filename:
            raise RuntimeError("Attach the momentum / AlgoStra trade-log CSV.")

        before = max(0, min(60, int(request.form.get("before_min", "3"))))
        after = max(0, min(180, int(request.form.get("after_min", "15"))))
        include_chain = request.form.get("include_chain") == "yes"

        sk = authenticate(request.cookies.get("de_state", ""), otp)
        mem, name, resolved, unresolved = build_package(sk, fs, before, after, include_chain)

        resp = Response(
            mem.getvalue(),
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
                "X-Resolved-Trades": str(resolved),
                "X-Unresolved-Trades": str(unresolved),
            },
        )
        resp.delete_cookie("de_state")
        return resp
    except Exception as e:
        return render_template(
            "index.html", error=str(e), otp_sent=True,
            password_enabled=bool(COLLECTOR_PASSWORD)
        ), 500

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
