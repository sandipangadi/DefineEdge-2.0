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
from flask import Flask, Response, make_response, render_template, request
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
COLLECTOR_PASSWORD = os.getenv("COLLECTOR_PASSWORD", "").strip()

OTP_STATES = {}
OTP_TTL_SECONDS = 10 * 60
MAX_TRADES_PER_RUN = 50

def new_http_session():
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
    s.headers.update({"User-Agent": "DefinedgeMomentumCollector/3.0"})
    return s

HTTP = new_http_session()

def require_config():
    missing = []
    if not API_TOKEN:
        missing.append("DEFINEDGE_API_TOKEN")
    if not API_SECRET:
        missing.append("DEFINEDGE_API_SECRET")
    if not COLLECTOR_PASSWORD:
        missing.append("COLLECTOR_PASSWORD")
    if missing:
        raise RuntimeError("Missing Render environment variable(s): " + ", ".join(missing))

def cleanup_states():
    now = time.time()
    stale = [k for k, v in OTP_STATES.items() if now - v["created"] > OTP_TTL_SECONDS]
    for k in stale:
        OTP_STATES.pop(k, None)

def send_otp(password):
    require_config()
    if password != COLLECTOR_PASSWORD:
        raise RuntimeError("Collector password is incorrect.")

    r = HTTP.get(
        LOGIN_URL + API_TOKEN,
        headers={"api_secret": API_SECRET},
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Definedge OTP request failed ({r.status_code}): {r.text[:500]}")

    data = r.json()
    otp_token = data.get("otp_token")
    if not otp_token:
        raise RuntimeError("Definedge returned success but no otp_token.")

    cleanup_states()
    state_id = secrets.token_urlsafe(32)
    OTP_STATES[state_id] = {
        "otp_token": otp_token,
        "created": time.time(),
        "password_verified": True,
    }
    return state_id

def extract_session_key(data):
    if isinstance(data, dict) and data.get("api_session_key"):
        return data["api_session_key"]

    # Some versions of the auth flow may return a JWT first.
    if isinstance(data, dict) and data.get("access_token"):
        r = HTTP.post(OMS_TOKEN_URL, json=data, timeout=30)
        if r.ok:
            oms = r.json()
            if oms.get("api_session_key"):
                return oms["api_session_key"]
    return None

def authenticate(state_id, otp):
    cleanup_states()
    state = OTP_STATES.get(state_id)
    if not state or not state.get("password_verified"):
        raise RuntimeError("OTP session expired. Click Send Definedge OTP again.")

    otp_token = state["otp_token"]

    # This minimal JSON form already worked in the user's GitHub Actions test.
    r = HTTP.post(
        TOKEN_URL,
        json={"otp_token": otp_token, "otp": otp.strip()},
        timeout=30,
    )
    if r.ok:
        try:
            key = extract_session_key(r.json())
            if key:
                OTP_STATES.pop(state_id, None)
                return key
        except Exception:
            pass

    # Fallback matching the fuller form documented by Definedge.
    r2 = HTTP.post(
        TOKEN_URL,
        data={
            "client_id": "TRTP",
            "grant_type": "password",
            "client_secret": API_SECRET,
            "otp_token": otp_token,
            "otp": otp.strip(),
        },
        timeout=30,
    )
    if not r2.ok:
        raise RuntimeError(
            f"Definedge OTP authentication failed. "
            f"JSON={r.status_code}, form={r2.status_code}: {r2.text[:500]}"
        )

    key = extract_session_key(r2.json())
    if not key:
        raise RuntimeError("Definedge authentication did not return a usable api_session_key.")

    OTP_STATES.pop(state_id, None)
    return key

def broker_get(session_key, endpoint):
    r = HTTP.get(
        API_BASE + endpoint,
        headers={"Authorization": session_key},
        timeout=30,
    )
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
        for key in (preferred, "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
    return []

def payload_to_csv(payload, preferred):
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
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(clean)
    return buf.getvalue()

def download_master(url):
    r = HTTP.get(url, timeout=45)
    if not r.ok:
        raise RuntimeError(f"Master-file download failed ({r.status_code})")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError("Master ZIP contained no CSV.")
        return z.read(csv_names[0]).decode("utf-8-sig", errors="replace")

def parse_master(raw):
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows:
        return []
    names = [
        "segment", "token", "symbol", "tradingsymbol", "instrument_type",
        "expiry", "ticksize", "lotsize", "optiontype", "strike_raw",
        "priceprec", "multiplier", "isin", "pricemult", "company"
    ]
    start = 0
    if rows:
        first = [re.sub(r"[^a-z0-9]+", "", x.lower()) for x in rows[0]]
        if "segment" in first[:2] or "token" in first[:3]:
            start = 1

    out = []
    for row in rows[start:]:
        if len(row) < 4:
            continue
        row = row + [""] * max(0, len(names) - len(row))
        d = dict(zip(names, [x.strip() for x in row[:len(names)]]))
        try:
            raw_strike = float(d["strike_raw"] or 0)
            priceprec = int(float(d["priceprec"] or 0))
            multiplier = float(d["multiplier"] or 1)
            denominator = multiplier * (10 ** priceprec)
            d["strike"] = raw_strike / denominator if denominator else raw_strike
        except Exception:
            d["strike"] = None
        out.append(d)
    return out

def parse_datetime(date_text, time_text):
    combo = f"{str(date_text).strip()} {str(time_text).strip()}".strip()
    for fmt in (
        "%d/%m/%Y %I:%M:%S %p",
        "%d-%m-%Y %I:%M:%S %p",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(combo, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None

def parse_algostra_event_log(text):
    """
    Parses the actual AlgoStra strategy-log export seen in LIQ20 BEAR AP V1.

    File header:
        Sr.,Timestamp,Category,Message

    Actual rows contain five logical values:
        Sr., Date, Time, Event, Details

    Example:
        10,04/09/2026, 09:52:00 am,ENTRY1,
        ANGELONE:ANGELONE29SEP26P300:2500:299.6000
    """
    raw_rows = list(csv.reader(io.StringIO(text)))
    if len(raw_rows) < 2:
        return None

    header = [x.strip().lower() for x in raw_rows[0]]
    if not ("timestamp" in header and "category" in header and "message" in header):
        return None

    events = []
    for row in raw_rows[1:]:
        if len(row) < 4:
            continue

        # The export's header has 4 labels but actual event rows carry 5 fields.
        if len(row) >= 5:
            sr, date_text, time_text, event_type = row[:4]
            details = ",".join(row[4:]).strip()
        else:
            # Defensive fallback.
            sr, date_text, event_type, details = row[:4]
            time_text = ""

        dt = parse_datetime(date_text, time_text)
        event = str(event_type).strip().upper()

        events.append({
            "sr": str(sr).strip(),
            "date": str(date_text).strip(),
            "time": str(time_text).strip(),
            "datetime": dt,
            "event": event,
            "details": details,
        })

    if not events:
        return None

    # Work chronologically even though AlgoStra export is newest-first.
    events.sort(key=lambda e: e["datetime"] or datetime.max.replace(tzinfo=IST))

    entries = []
    exits_by_symbol = {}

    for event in events:
        dt = event["datetime"]
        if not dt:
            continue

        etype = event["event"]
        details = event["details"]

        # ENTRY1 / ENTRY2 etc.  Ignore "ENTRY QUAL".
        if re.fullmatch(r"ENTRY\d*", etype):
            # underlying:tradingsymbol:qty:underlying/reference price
            m = re.match(r"^\s*([^:]+):([^:]+):([^:]+):([^:]+)", details)
            if not m:
                continue
            underlying, tradingsymbol, qty, ref_price = [x.strip() for x in m.groups()]
            entries.append({
                "source": "algostra_event_log",
                "strategy_event": etype,
                "underlying": underlying,
                "tradingsymbol": tradingsymbol,
                "quantity": qty,
                "reference_underlying_price": ref_price,
                "entry_time": dt,
                "exit_time": None,
                "exit_event": "",
                "exit_details": "",
            })

        # EXIT rows.  Ignore "EXIT QUAL" / "EXIT1 QUAL".
        elif etype == "EXIT":
            # Example:
            # RVNL:RVNL29SEP26P210/null:1925/0:212.1300/0.0000
            m = re.match(r"^\s*([^:]+):([^/:]+)", details)
            if not m:
                continue
            underlying, tradingsymbol = [x.strip() for x in m.groups()]
            exits_by_symbol.setdefault(tradingsymbol.upper(), []).append({
                "underlying": underlying,
                "tradingsymbol": tradingsymbol,
                "datetime": dt,
                "event": etype,
                "details": details,
            })

    # Pair each entry to the first later EXIT for the same exact option symbol.
    for entry in entries:
        candidates = exits_by_symbol.get(entry["tradingsymbol"].upper(), [])
        later = [x for x in candidates if x["datetime"] >= entry["entry_time"]]
        if later:
            chosen = min(later, key=lambda x: x["datetime"])
            entry["exit_time"] = chosen["datetime"]
            entry["exit_event"] = chosen["event"]
            entry["exit_details"] = chosen["details"]

    return entries

def generic_trade_rows(text):
    """Fallback for ordinary one-row-per-trade CSV exports."""
    try:
        dialect = csv.Sniffer().sniff(text[:8000], delimiters=",;\t")
    except Exception:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    if not rows:
        return []

    def norm(s):
        return re.sub(r"[^a-z0-9]+", "", str(s).lower())

    def pick(row, names):
        n = {norm(k): v for k, v in row.items()}
        for name in names:
            key = norm(name)
            if key in n and str(n[key]).strip():
                return str(n[key]).strip()
        return ""

    out = []
    for row in rows:
        entry_text = pick(row, ["entry_time", "entrytime", "entry time 1", "entry datetime"])
        exit_text = pick(row, ["exit_time", "exittime", "exit time 1", "exit datetime"])

        def pdt(s):
            s = str(s).strip()
            for fmt in (
                "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%d/%m/%Y %I:%M:%S %p", "%d-%m-%Y %I:%M:%S %p",
                "%Y-%m-%d %H:%M:%S"
            ):
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=IST)
                except ValueError:
                    pass
            return None

        entry_dt = pdt(entry_text)
        if not entry_dt:
            continue

        out.append({
            "source": "generic_trade_csv",
            "strategy_event": "",
            "underlying": pick(row, ["underlying", "underlying_symbol", "stock", "symbol", "scrip"]),
            "tradingsymbol": pick(row, ["tradingsymbol", "trading_symbol", "option_symbol", "contract"]),
            "quantity": pick(row, ["quantity", "qty", "entry qty 1"]),
            "reference_underlying_price": "",
            "entry_time": entry_dt,
            "exit_time": pdt(exit_text),
            "exit_event": "",
            "exit_details": "",
            "raw_row": row,
        })
    return out

def load_trade_log(file_storage):
    raw = file_storage.read()
    if not raw:
        raise RuntimeError("Uploaded trade log is empty.")
    text = raw.decode("utf-8-sig", errors="replace")

    trades = parse_algostra_event_log(text)
    if trades is not None:
        return trades, text, "algostra_event_log"

    trades = generic_trade_rows(text)
    if not trades:
        raise RuntimeError(
            "Could not recognise the uploaded CSV as an AlgoStra strategy log "
            "or a generic trade CSV."
        )
    return trades, text, "generic_trade_csv"

def resolve_by_tradingsymbol(tradingsymbol, nfo):
    ts = str(tradingsymbol).strip().upper()
    if not ts:
        return None
    hits = [r for r in nfo if r["tradingsymbol"].upper() == ts]
    if len(hits) == 1:
        return hits[0]
    return None

def resolve_underlying(symbol, nse):
    target = str(symbol).upper().replace("-EQ", "").strip()
    candidates = [
        r for r in nse
        if target in (
            r["symbol"].upper().replace("-EQ", ""),
            r["tradingsymbol"].upper().replace("-EQ", ""),
        )
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: 0 if r["tradingsymbol"].upper().endswith("-EQ") else 1)
    return candidates[0]

def history_raw(session_key, segment, token, timeframe, start_dt, end_dt):
    frm = start_dt.astimezone(IST).strftime("%d%m%Y%H%M")
    to = end_dt.astimezone(IST).strftime("%d%m%Y%H%M")
    url = f"{HISTORY_BASE}/{segment}/{token}/{timeframe}/{frm}/{to}"

    r = HTTP.get(
        url,
        headers={"Authorization": session_key},
        timeout=45,
    )
    if not r.ok:
        raise RuntimeError(f"History API {r.status_code}: {r.text[:500]}")
    return r.text

def parse_ticks(raw):
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            cols = next(csv.reader([line]))
            epoch = float(cols[0])
            ltp = float(cols[1])
        except Exception:
            continue

        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(IST)
        rows.append({
            "utc_seconds": cols[0],
            "timestamp_ist": dt.isoformat(),
            "_dt": dt,
            "ltp": ltp,
            "ltq": cols[2] if len(cols) > 2 else "",
            "open_interest": cols[3] if len(cols) > 3 else "",
        })
    return rows

def ticks_to_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["utc_seconds", "timestamp_ist", "ltp", "ltq", "open_interest"],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row[k] for k in writer.fieldnames})
    return buf.getvalue()

def minute_to_csv(raw):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["datetime", "open", "high", "low", "close", "volume", "open_interest"])
    for line in raw.splitlines():
        if line.strip():
            cols = next(csv.reader([line]))
            writer.writerow((cols + [""] * 7)[:7])
    return buf.getvalue()

def nearest_tick(ticks, target_dt):
    if not ticks:
        return None
    return min(ticks, key=lambda r: abs((r["_dt"] - target_dt).total_seconds()))

def compute_metrics(ticks, entry_dt, exit_dt, after_end):
    if not ticks:
        return {}

    entry_tick = nearest_tick(ticks, entry_dt)
    if not entry_tick:
        return {}

    entry_price = entry_tick["ltp"]
    exit_tick = nearest_tick(ticks, exit_dt) if exit_dt else None

    during = [
        r for r in ticks
        if r["_dt"] >= entry_dt and (exit_dt is None or r["_dt"] <= exit_dt)
    ]
    after = [
        r for r in ticks
        if exit_dt is not None and exit_dt < r["_dt"] <= after_end
    ]

    out = {
        "entry_tick_time": entry_tick["timestamp_ist"],
        "entry_option_ltp": entry_price,
        "exit_tick_time": exit_tick["timestamp_ist"] if exit_tick else "",
        "exit_option_ltp": exit_tick["ltp"] if exit_tick else "",
    }

    if during:
        hi = max(during, key=lambda r: r["ltp"])
        lo = min(during, key=lambda r: r["ltp"])
        out.update({
            "max_option_ltp_during_trade": hi["ltp"],
            "max_option_ltp_time": hi["timestamp_ist"],
            "min_option_ltp_during_trade": lo["ltp"],
            "min_option_ltp_time": lo["timestamp_ist"],
            "mfe_pct_long_option": round((hi["ltp"] / entry_price - 1) * 100, 4) if entry_price else "",
            "mae_pct_long_option": round((lo["ltp"] / entry_price - 1) * 100, 4) if entry_price else "",
        })
        if exit_tick and hi["ltp"]:
            out["peak_to_exit_giveback_pct"] = round(
                (1 - exit_tick["ltp"] / hi["ltp"]) * 100, 4
            )

    if after:
        out["post_exit_max_option_ltp"] = max(r["ltp"] for r in after)
        out["post_exit_min_option_ltp"] = min(r["ltp"] for r in after)

    return out

def entry_atm_chain(contract, nfo, underlying_entry_ltp):
    if underlying_entry_ltp is None:
        return []

    symbol = contract["symbol"].upper()
    expiry = contract["expiry"]

    options = [
        r for r in nfo
        if r["symbol"].upper() == symbol
        and r["expiry"] == expiry
        and r.get("strike") is not None
        and r["optiontype"].upper() in ("CE", "PE")
    ]
    strikes = sorted({float(r["strike"]) for r in options})
    if not strikes:
        return []

    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying_entry_ltp))
    chosen_strikes = strikes[max(0, atm_i - 1): min(len(strikes), atm_i + 2)]

    selected = []
    for strike in chosen_strikes:
        for side in ("CE", "PE"):
            hits = [
                r for r in options
                if abs(float(r["strike"]) - strike) < 1e-9
                and r["optiontype"].upper() == side
            ]
            if hits:
                selected.append(hits[0])
    return selected

def safe_name(value):
    return (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._") or "item"
    )[:120]

def build_package(session_key, file_storage, before_min, after_min, include_chain):
    trades, original_text, detected_format = load_trade_log(file_storage)

    if len(trades) > MAX_TRADES_PER_RUN:
        raise RuntimeError(
            f"Found {len(trades)} trade entries; maximum per run is {MAX_TRADES_PER_RUN}."
        )

    nfo_raw = download_master(NFO_MASTER_URL)
    nse_raw = download_master(NSE_MASTER_URL)
    nfo = parse_master(nfo_raw)
    nse = parse_master(nse_raw)

    broker_payloads = {
        "orders": broker_get(session_key, "/orders"),
        "trades": broker_get(session_key, "/trades"),
        "positions": broker_get(session_key, "/positions"),
        "limits": broker_get(session_key, "/limits"),
    }

    history_cache = {}

    def cached_history(segment, token, timeframe, start_dt, end_dt):
        key = (
            segment, token, timeframe,
            start_dt.strftime("%Y%m%d%H%M"),
            end_dt.strftime("%Y%m%d%H%M"),
        )
        if key not in history_cache:
            history_cache[key] = history_raw(
                session_key, segment, token, timeframe, start_dt, end_dt
            )
        return history_cache[key]

    mem = io.BytesIO()
    manifest = []
    unresolved = []
    metrics_rows = []
    summary_rows = []

    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("input/original_trade_log.csv", original_text)
        z.writestr("reference/nfo_master_snapshot.csv", nfo_raw)
        z.writestr("reference/nse_master_snapshot.csv", nse_raw)

        for name, payload in broker_payloads.items():
            z.writestr(f"broker/{name}.json", json.dumps(payload, indent=2, ensure_ascii=False))
            z.writestr(f"broker/{name}.csv", payload_to_csv(payload, name))

        for i, trade in enumerate(trades, start=1):
            contract = resolve_by_tradingsymbol(trade["tradingsymbol"], nfo)
            if not contract:
                unresolved.append({
                    "trade_number": i,
                    "reason": "Exact AlgoStra option trading symbol not found in current NFO master.",
                    "underlying": trade.get("underlying", ""),
                    "tradingsymbol": trade.get("tradingsymbol", ""),
                    "entry_time": trade.get("entry_time").isoformat() if trade.get("entry_time") else "",
                })
                continue

            entry_dt = trade["entry_time"]
            exit_dt = trade.get("exit_time")

            # If no EXIT was logged, collect through the latest log / after window,
            # but do not pretend this is an actual exit.
            effective_exit = exit_dt or entry_dt
            start_dt = entry_dt - timedelta(minutes=before_min)
            end_dt = effective_exit + timedelta(minutes=after_min)

            base = f"trades/{i:03d}_{safe_name(contract['tradingsymbol'])}"

            metadata = {
                "trade_number": i,
                "input_format": detected_format,
                "underlying": trade["underlying"],
                "tradingsymbol": contract["tradingsymbol"],
                "token": contract["token"],
                "segment": contract["segment"],
                "instrument_type": contract["instrument_type"],
                "expiry": contract["expiry"],
                "strike": contract["strike"],
                "optiontype": contract["optiontype"],
                "quantity": trade.get("quantity", ""),
                "reference_underlying_price_from_algostra": trade.get("reference_underlying_price", ""),
                "entry_time_ist": entry_dt.isoformat(),
                "exit_time_ist": exit_dt.isoformat() if exit_dt else "",
                "exit_was_found_in_algostra_log": bool(exit_dt),
                "exit_details": trade.get("exit_details", ""),
                "window_from_ist": start_dt.isoformat(),
                "window_to_ist": end_dt.isoformat(),
                "files": [],
            }

            summary_rows.append({
                "trade_number": i,
                "underlying": trade["underlying"],
                "tradingsymbol": contract["tradingsymbol"],
                "token": contract["token"],
                "expiry": contract["expiry"],
                "strike": contract["strike"],
                "optiontype": contract["optiontype"],
                "quantity": trade.get("quantity", ""),
                "entry_time_ist": entry_dt.isoformat(),
                "exit_time_ist": exit_dt.isoformat() if exit_dt else "",
                "reference_underlying_price_from_algostra": trade.get("reference_underlying_price", ""),
            })

            option_ticks = []
            try:
                raw_tick = cached_history(
                    contract["segment"], contract["token"], "tick", start_dt, end_dt
                )
                option_ticks = parse_ticks(raw_tick)
                if not option_ticks:
                    raise RuntimeError("Tick endpoint returned no valid rows.")
                path = f"{base}/traded_option_ticks.csv"
                z.writestr(path, ticks_to_csv(option_ticks))
                metadata["files"].append(path)
            except Exception as exc:
                metadata["traded_option_tick_error"] = str(exc)

            # 1-minute fallback
            try:
                raw_min = cached_history(
                    contract["segment"], contract["token"], "minute", start_dt, end_dt
                )
                path = f"{base}/traded_option_1min.csv"
                z.writestr(path, minute_to_csv(raw_min))
                metadata["files"].append(path)
            except Exception as exc:
                metadata["traded_option_1min_error"] = str(exc)

            # Underlying
            underlying_rec = resolve_underlying(contract["symbol"], nse)
            underlying_ticks = []
            underlying_entry_ltp = None

            if underlying_rec:
                metadata["underlying_token"] = underlying_rec["token"]
                metadata["underlying_tradingsymbol"] = underlying_rec["tradingsymbol"]

                try:
                    raw_u_tick = cached_history(
                        underlying_rec["segment"], underlying_rec["token"],
                        "tick", start_dt, end_dt
                    )
                    underlying_ticks = parse_ticks(raw_u_tick)
                    if underlying_ticks:
                        path = f"{base}/underlying_ticks.csv"
                        z.writestr(path, ticks_to_csv(underlying_ticks))
                        metadata["files"].append(path)

                        ent = nearest_tick(underlying_ticks, entry_dt)
                        if ent:
                            underlying_entry_ltp = ent["ltp"]
                            metadata["underlying_entry_ltp_from_ticks"] = underlying_entry_ltp
                except Exception as exc:
                    metadata["underlying_tick_error"] = str(exc)

                try:
                    raw_u_min = cached_history(
                        underlying_rec["segment"], underlying_rec["token"],
                        "minute", start_dt, end_dt
                    )
                    path = f"{base}/underlying_1min.csv"
                    z.writestr(path, minute_to_csv(raw_u_min))
                    metadata["files"].append(path)
                except Exception as exc:
                    metadata["underlying_1min_error"] = str(exc)

            # Metrics use actual EXIT when present.
            if exit_dt and option_ticks:
                metrics = compute_metrics(option_ticks, entry_dt, exit_dt, end_dt)
                if metrics:
                    metrics_row = {
                        "trade_number": i,
                        "underlying": trade["underlying"],
                        "tradingsymbol": contract["tradingsymbol"],
                        "token": contract["token"],
                        "expiry": contract["expiry"],
                        "strike": contract["strike"],
                        "optiontype": contract["optiontype"],
                        "quantity": trade.get("quantity", ""),
                        "entry_time_ist": entry_dt.isoformat(),
                        "exit_time_ist": exit_dt.isoformat(),
                        **metrics,
                    }
                    metrics_rows.append(metrics_row)
                    metadata["metrics"] = metrics

            # ATM-1 / ATM / ATM+1, both CE and PE
            if include_chain and underlying_entry_ltp is not None:
                for chain_rec in entry_atm_chain(contract, nfo, underlying_entry_ltp):
                    try:
                        raw_chain = cached_history(
                            chain_rec["segment"], chain_rec["token"],
                            "tick", start_dt, end_dt
                        )
                        ticks = parse_ticks(raw_chain)
                        if not ticks:
                            continue
                        path = (
                            f"{base}/entry_atm_chain/"
                            f"{safe_name(chain_rec['tradingsymbol'])}_ticks.csv"
                        )
                        z.writestr(path, ticks_to_csv(ticks))
                        metadata["files"].append(path)
                    except Exception as exc:
                        metadata.setdefault("chain_errors", []).append({
                            "tradingsymbol": chain_rec["tradingsymbol"],
                            "error": str(exc),
                        })

            z.writestr(
                f"{base}/trade_metadata.json",
                json.dumps(metadata, indent=2, ensure_ascii=False),
            )
            manifest.append(metadata)

        def write_dict_csv(path, records):
            if not records:
                z.writestr(path, "")
                return
            fields = []
            for record in records:
                for key in record:
                    if key not in fields:
                        fields.append(key)
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
            z.writestr(path, buf.getvalue())

        write_dict_csv("analysis/trade_summary.csv", summary_rows)
        write_dict_csv("analysis/trade_metrics.csv", metrics_rows)
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        z.writestr("unresolved_rows.json", json.dumps(unresolved, indent=2, ensure_ascii=False))
        z.writestr(
            "README.txt",
            f"""DEFINEDGE MOMENTUM TICK COLLECTOR V3

Detected input format: {detected_format}

V3 was specifically updated for the real AlgoStra strategy-log CSV format,
including the LIQ20 BEAR AP V1 export.

The AlgoStra log itself contains exact option trading symbols on ENTRY1 rows,
for example:
  ANGELONE29SEP26P300
  RVNL29SEP26P210
  TCS29SEP26P2340

V3 pairs ENTRY with the later EXIT for the same exact option symbol, resolves
that symbol against the current Definedge NFO master file, then collects:

- exact traded-option tick data: UTC, IST time, LTP, LTQ, OI
- exact traded-option 1-minute data as fallback
- underlying stock tick + 1-minute data
- optional entry ATM-1 / ATM / ATM+1 for BOTH CE and PE
- broker orders/trades/positions/limits
- trade_summary.csv
- trade_metrics.csv with MFE, MAE, peak-to-exit giveback and post-exit extremes

Definedge documents tick history as available for the last 2 trading sessions.
Run this collector daily / by the next session and keep the generated ZIP.

Read-only: no place/modify/cancel order APIs are present.
""",
        )

    mem.seek(0)
    stamp = datetime.now(IST).strftime("%Y-%m-%d_%H%M%S")
    return mem, f"Momentum_Tick_Data_V3_{stamp}.zip", len(manifest), len(unresolved), detected_format

@app.get("/")
def home():
    return render_template("index.html", otp_sent=False)

@app.post("/send-otp")
def send_otp_route():
    try:
        password = request.form.get("collector_password", "")
        state_id = send_otp(password)

        resp = make_response(render_template(
            "index.html",
            otp_sent=True,
            message="OTP sent. Password accepted for this session — you do NOT need to enter it again.",
        ))
        resp.set_cookie(
            "de_state",
            state_id,
            max_age=OTP_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
        )
        return resp
    except Exception as exc:
        return render_template("index.html", otp_sent=False, error=str(exc)), 500

@app.post("/collect")
def collect_route():
    try:
        otp = request.form.get("otp", "").strip()
        trade_file = request.files.get("trade_log")
        state_id = request.cookies.get("de_state", "")

        if not otp:
            raise RuntimeError("Enter the Definedge OTP.")
        if not trade_file or not trade_file.filename:
            raise RuntimeError("Attach the AlgoStra strategy-log CSV.")

        before_min = max(0, min(60, int(request.form.get("before_min", "3"))))
        after_min = max(0, min(180, int(request.form.get("after_min", "15"))))
        include_chain = request.form.get("include_chain") == "yes"

        session_key = authenticate(state_id, otp)
        mem, filename, resolved, unresolved, detected = build_package(
            session_key,
            trade_file,
            before_min,
            after_min,
            include_chain,
        )

        resp = Response(
            mem.getvalue(),
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
                "X-Resolved-Trades": str(resolved),
                "X-Unresolved-Trades": str(unresolved),
                "X-Input-Format": detected,
            },
        )
        resp.delete_cookie("de_state")
        return resp

    except Exception as exc:
        return render_template(
            "index.html",
            otp_sent=True,
            error=str(exc),
        ), 500

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
