import csv
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, jsonify, make_response, render_template, request, send_file
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

JOBS = {}
JOB_LOCK = threading.Lock()
JOB_TTL_SECONDS = 2 * 60 * 60
TMP_DIR = Path("/tmp/definedge_v5")
TMP_DIR.mkdir(parents=True, exist_ok=True)

MAX_INPUT_MB = 30
MAX_CSV_FILES = 100
MAX_POSITION_TRADES = 150
HISTORY_WORKERS = 4


def new_http_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    session.mount(
        "https://",
        HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20),
    )
    session.headers.update({"User-Agent": "DefinedgeMomentumCollector/5.0"})
    return session


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
        raise RuntimeError(
            "Missing Render environment variable(s): " + ", ".join(missing)
        )


def cleanup_states():
    now = time.time()
    for key in [
        key
        for key, value in OTP_STATES.items()
        if now - value["created"] > OTP_TTL_SECONDS
    ]:
        OTP_STATES.pop(key, None)


def cleanup_jobs():
    now = time.time()
    stale = []
    with JOB_LOCK:
        for job_id, job in JOBS.items():
            if now - job["created"] > JOB_TTL_SECONDS:
                stale.append(job_id)

        for job_id in stale:
            job = JOBS.pop(job_id, None)
            if job and job.get("output_path"):
                try:
                    Path(job["output_path"]).unlink(missing_ok=True)
                except Exception:
                    pass


def set_job(job_id, **updates):
    with JOB_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)


def send_definedge_otp(password):
    require_config()

    if password != COLLECTOR_PASSWORD:
        raise RuntimeError("Collector password is incorrect.")

    response = HTTP.get(
        LOGIN_URL + API_TOKEN,
        headers={"api_secret": API_SECRET},
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            f"Definedge OTP request failed ({response.status_code}): "
            f"{response.text[:500]}"
        )

    data = response.json()
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

    if isinstance(data, dict) and data.get("access_token"):
        response = HTTP.post(OMS_TOKEN_URL, json=data, timeout=30)
        if response.ok:
            oms_data = response.json()
            if oms_data.get("api_session_key"):
                return oms_data["api_session_key"]

    return None


def authenticate(state_id, otp):
    cleanup_states()
    state = OTP_STATES.get(state_id)

    if not state or not state.get("password_verified"):
        raise RuntimeError(
            "OTP session expired. Click Send Definedge OTP again."
        )

    otp_token = state["otp_token"]

    # Minimal flow already proven in the user's GitHub test.
    response = HTTP.post(
        TOKEN_URL,
        json={
            "otp_token": otp_token,
            "otp": otp.strip(),
        },
        timeout=30,
    )

    if response.ok:
        try:
            key = extract_session_key(response.json())
            if key:
                OTP_STATES.pop(state_id, None)
                return key
        except Exception:
            pass

    # Fallback to fuller form used in Definedge documentation.
    response2 = HTTP.post(
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

    if not response2.ok:
        raise RuntimeError(
            "Definedge OTP authentication failed. "
            f"JSON={response.status_code}, "
            f"form={response2.status_code}: "
            f"{response2.text[:500]}"
        )

    key = extract_session_key(response2.json())

    if not key:
        raise RuntimeError(
            "Definedge authentication did not return a usable api_session_key."
        )

    OTP_STATES.pop(state_id, None)
    return key


def broker_get(session_key, endpoint):
    response = HTTP.get(
        API_BASE + endpoint,
        headers={"Authorization": session_key},
        timeout=30,
    )

    if not response.ok:
        return {
            "_http_status": response.status_code,
            "_error": response.text[:2000],
        }

    try:
        return response.json()
    except Exception:
        return {
            "_http_status": response.status_code,
            "_raw_text": response.text,
        }


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
    cleaned = []

    for row in rows:
        if not isinstance(row, dict):
            row = {"value": row}

        out = {}

        for key, value in row.items():
            if key not in fields:
                fields.append(key)

            if isinstance(value, (dict, list)):
                out[key] = json.dumps(value, ensure_ascii=False)
            else:
                out[key] = value

        cleaned.append(out)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(cleaned)

    return buffer.getvalue()


def download_master(url):
    response = HTTP.get(url, timeout=45)

    if not response.ok:
        raise RuntimeError(
            f"Definedge master-file download failed ({response.status_code})."
        )

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = [
            name
            for name in zf.namelist()
            if name.lower().endswith(".csv")
        ]

        if not names:
            raise RuntimeError("Definedge master ZIP contained no CSV.")

        return zf.read(names[0]).decode(
            "utf-8-sig",
            errors="replace",
        )


def parse_master(raw):
    rows = list(csv.reader(io.StringIO(raw)))

    if not rows:
        return []

    fields = [
        "segment",
        "token",
        "symbol",
        "tradingsymbol",
        "instrument_type",
        "expiry",
        "ticksize",
        "lotsize",
        "optiontype",
        "strike_raw",
        "priceprec",
        "multiplier",
        "isin",
        "pricemult",
        "company",
    ]

    first = [
        re.sub(r"[^a-z0-9]+", "", value.lower())
        for value in rows[0]
    ]

    start = 1 if ("segment" in first[:2] or "token" in first[:3]) else 0

    output = []

    for row in rows[start:]:
        if len(row) < 4:
            continue

        row = row + [""] * max(0, len(fields) - len(row))

        record = dict(
            zip(
                fields,
                [value.strip() for value in row[: len(fields)]],
            )
        )

        try:
            raw_strike = float(record["strike_raw"] or 0)
            priceprec = int(float(record["priceprec"] or 0))
            multiplier = float(record["multiplier"] or 1)
            denominator = multiplier * (10 ** priceprec)
            record["strike"] = (
                raw_strike / denominator
                if denominator
                else raw_strike
            )
        except Exception:
            record["strike"] = None

        output.append(record)

    return output


def parse_full_timestamp(value):
    text = str(value or "").strip()

    if not text or text == "-":
        return None

    for fmt in (
        "%d/%m/%Y, %I:%M:%S %p",
        "%d-%m-%Y, %I:%M:%S %p",
        "%d/%m/%Y %I:%M:%S %p",
        "%d-%m-%Y %I:%M:%S %p",
        "%d/%m/%Y, %H:%M:%S",
        "%d-%m-%Y, %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            pass

    return None


def positions_symbol_to_contract(symbol_text):
    """
    AlgoStra Positions:
        TCS 29-Sep-2026 PE 2340

    Definedge trading symbol:
        TCS29SEP26P2340
    """
    text = str(symbol_text or "").strip()

    match = re.match(
        r"^(.*?)\s+"
        r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+"
        r"(CE|PE)\s+"
        r"([0-9]+(?:\.[0-9]+)?)$",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    underlying, expiry_text, optiontype, strike_text = match.groups()

    try:
        expiry = datetime.strptime(
            expiry_text,
            "%d-%b-%Y",
        )
    except ValueError:
        return None

    strike_float = float(strike_text)

    if strike_float.is_integer():
        strike_for_symbol = str(int(strike_float))
    else:
        strike_for_symbol = (
            f"{strike_float:.8f}"
            .rstrip("0")
            .rstrip(".")
        )

    cp = "C" if optiontype.upper() == "CE" else "P"

    return {
        "underlying": underlying.strip().upper(),
        "expiry": expiry.strftime("%d-%b-%Y"),
        "optiontype": optiontype.upper(),
        "strike": strike_float,
        "tradingsymbol": (
            f"{underlying.strip().upper()}"
            f"{expiry.strftime('%d%b%y').upper()}"
            f"{cp}"
            f"{strike_for_symbol}"
        ),
    }


def parse_numeric(value):
    text = str(value or "").replace(",", "").strip()

    if not text or text == "-":
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except Exception:
        return None


def parse_positions_csv(text, filename):
    lines = text.splitlines()

    if not lines:
        return None

    strategy_name = ""
    header_index = None

    for index, line in enumerate(lines[:30]):
        stripped = line.strip()

        if stripped.lower().startswith("strategy:"):
            strategy_name = stripped.split(":", 1)[1].strip()

        if (
            "Entry Qty 1" in line
            and "Entry Price 1" in line
            and "Entry Time 1" in line
            and "Order Status" in line
        ):
            header_index = index
            break

    if header_index is None:
        return None

    csv_text = "\n".join(lines[header_index:])
    rows = list(csv.DictReader(io.StringIO(csv_text)))

    if not rows:
        return None

    trades = []

    for source_row, row in enumerate(rows, start=1):
        contract = positions_symbol_to_contract(row.get("Symbol", ""))

        if not contract:
            continue

        for leg in (1, 2):
            qty_text = str(
                row.get(f"Entry Qty {leg}", "") or ""
            ).strip()

            entry_time_text = str(
                row.get(f"Entry Time {leg}", "") or ""
            ).strip()

            if (
                not qty_text
                or qty_text in ("0", "0.0")
                or not entry_time_text
                or entry_time_text == "-"
            ):
                continue

            entry_dt = parse_full_timestamp(entry_time_text)

            if not entry_dt:
                continue

            exit_dt = parse_full_timestamp(
                row.get(f"Exit Time {leg}", "")
            )

            trades.append(
                {
                    "source_filename": filename,
                    "source_type": "positions",
                    "strategy_name": strategy_name,
                    "source_row": source_row,
                    "leg": leg,
                    "symbol_display": str(
                        row.get("Symbol", "") or ""
                    ).strip(),
                    "exchange": str(
                        row.get("Exchange", "") or ""
                    ).strip(),
                    "underlying": contract["underlying"],
                    "tradingsymbol": contract["tradingsymbol"],
                    "expiry": contract["expiry"],
                    "strike": contract["strike"],
                    "optiontype": contract["optiontype"],
                    "quantity": parse_numeric(qty_text),
                    "entry_price_reported": parse_numeric(
                        row.get(f"Entry Price {leg}", "")
                    ),
                    "exit_price_reported": parse_numeric(
                        row.get(f"Exit Price {leg}", "")
                    ),
                    "entry_time": entry_dt,
                    "exit_time": exit_dt,
                    "cmp_reported": str(
                        row.get("CMP (% change)", "") or ""
                    ).strip(),
                    "pnl_reported_text": str(
                        row.get("P&L (% change)", "") or ""
                    ).strip(),
                    "pnl_reported_value": parse_numeric(
                        row.get("P&L (% change)", "")
                    ),
                    "order_status": str(
                        row.get("Order Status", "") or ""
                    ).strip(),
                }
            )

    return {
        "strategy_name": strategy_name,
        "trades": trades,
    }


def parse_activity_log(text, filename):
    rows = list(csv.reader(io.StringIO(text)))

    if not rows:
        return None

    header = [
        value.strip().lower()
        for value in rows[0]
    ]

    if not (
        "timestamp" in header
        and "category" in header
        and "message" in header
    ):
        return None

    events = []

    for row in rows[1:]:
        if len(row) < 4:
            continue

        if len(row) >= 5:
            sr, date_text, time_text, category = row[:4]
            message = ",".join(row[4:]).strip()
        else:
            sr, date_text, category, message = row[:4]
            time_text = ""

        dt = None

        if time_text:
            dt = parse_full_timestamp(
                f"{date_text.strip()}, {time_text.strip()}"
            )

        events.append(
            {
                "source_filename": filename,
                "sr": str(sr).strip(),
                "datetime": dt,
                "date": str(date_text).strip(),
                "time": str(time_text).strip(),
                "category": str(category).strip().upper(),
                "message": message,
            }
        )

    return events


def normalized_strategy_name(name):
    text = str(name or "").upper()

    text = re.sub(
        r"\b(POSITIONS?|ACTIVITY\s*LOGS?|LOGS?)\b",
        " ",
        text,
    )

    text = re.sub(
        r"\.CSV$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"[^A-Z0-9]+",
        " ",
        text,
    )

    return " ".join(text.split())


def strategy_from_activity_filename(filename):
    return normalized_strategy_name(Path(filename).stem)


def safe_zip_input(file_bytes):
    if len(file_bytes) > MAX_INPUT_MB * 1024 * 1024:
        raise RuntimeError(
            f"Input ZIP is too large. Maximum is {MAX_INPUT_MB} MB."
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        raise RuntimeError(
            "The uploaded file is not a valid ZIP."
        )

    csv_names = []

    total_uncompressed = 0

    for info in zf.infolist():
        total_uncompressed += info.file_size

        if total_uncompressed > 100 * 1024 * 1024:
            raise RuntimeError(
                "The ZIP expands beyond the safe 100 MB limit."
            )

        path = Path(info.filename)

        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(
                "Unsafe path detected inside ZIP."
            )

        if info.filename.lower().endswith(".csv"):
            csv_names.append(info.filename)

    if len(csv_names) > MAX_CSV_FILES:
        raise RuntimeError(
            f"Too many CSVs in one ZIP. Maximum is {MAX_CSV_FILES}."
        )

    if not csv_names:
        raise RuntimeError(
            "No CSV files were found inside the ZIP."
        )

    return zf, csv_names


def parse_batch_zip(file_bytes):
    zf, csv_names = safe_zip_input(file_bytes)

    originals = {}
    positions = []
    activities_by_strategy = defaultdict(list)
    activity_events_all = []
    file_report = []

    for name in csv_names:
        raw = zf.read(name)
        text = raw.decode("utf-8-sig", errors="replace")

        originals[name] = raw

        positions_result = parse_positions_csv(text, name)

        if positions_result is not None:
            strategy_name = positions_result["strategy_name"]
            positions.extend(positions_result["trades"])

            file_report.append(
                {
                    "filename": name,
                    "detected_type": "positions",
                    "strategy_name": strategy_name,
                    "trade_rows": len(
                        positions_result["trades"]
                    ),
                }
            )
            continue

        activity_result = parse_activity_log(text, name)

        if activity_result is not None:
            strategy_key = strategy_from_activity_filename(name)

            activities_by_strategy[strategy_key].extend(
                activity_result
            )

            activity_events_all.extend(activity_result)

            file_report.append(
                {
                    "filename": name,
                    "detected_type": "activity_log",
                    "strategy_name": strategy_key,
                    "trade_rows": "",
                }
            )
            continue

        file_report.append(
            {
                "filename": name,
                "detected_type": "unrecognised_csv",
                "strategy_name": "",
                "trade_rows": "",
            }
        )

    zf.close()

    if not positions:
        raise RuntimeError(
            "No AlgoStra Positions trades were detected in the ZIP."
        )

    if len(positions) > MAX_POSITION_TRADES:
        raise RuntimeError(
            f"Detected {len(positions)} trades. "
            f"Maximum per run is {MAX_POSITION_TRADES}."
        )

    # Attach matched activity-log summary to each strategy.
    strategy_activity = {}

    strategy_names = sorted(
        {
            trade["strategy_name"]
            for trade in positions
            if trade["strategy_name"]
        }
    )

    for strategy_name in strategy_names:
        key = normalized_strategy_name(strategy_name)
        events = activities_by_strategy.get(key, [])

        categories = defaultdict(int)
        max_loss = False
        square_off = False
        stopped = False
        first_event = None
        last_event = None

        for event in events:
            categories[event["category"]] += 1

            message_lower = event["message"].lower()

            if "maximum loss" in message_lower or "max loss" in message_lower:
                max_loss = True

            if event["category"] in ("SQROFF", "TIMESQROFF"):
                square_off = True

            if event["category"] == "STOPPED":
                stopped = True

            if event["datetime"]:
                if first_event is None or event["datetime"] < first_event:
                    first_event = event["datetime"]

                if last_event is None or event["datetime"] > last_event:
                    last_event = event["datetime"]

        strategy_activity[strategy_name] = {
            "strategy_name": strategy_name,
            "activity_file_found": bool(events),
            "activity_event_count": len(events),
            "entry_events": categories.get("ENTRY1", 0)
            + categories.get("ENTRY2", 0)
            + categories.get("ENTRY", 0),
            "exit_events": categories.get("EXIT", 0),
            "sqroff_events": categories.get("SQROFF", 0),
            "time_sqroff_events": categories.get("TIMESQROFF", 0),
            "stopped_events": categories.get("STOPPED", 0),
            "max_loss_triggered": max_loss,
            "square_off_seen": square_off,
            "stopped_seen": stopped,
            "first_event_ist": (
                first_event.isoformat()
                if first_event
                else ""
            ),
            "last_event_ist": (
                last_event.isoformat()
                if last_event
                else ""
            ),
        }

    return {
        "originals": originals,
        "positions": positions,
        "activity_events": activity_events_all,
        "strategy_activity": strategy_activity,
        "file_report": file_report,
    }


def resolve_option(tradingsymbol, nfo_by_ts):
    return nfo_by_ts.get(
        str(tradingsymbol).upper().strip()
    )


def resolve_underlying(symbol, nse):
    target = str(symbol).upper().replace("-EQ", "").strip()

    candidates = [
        record
        for record in nse
        if target
        in (
            record["symbol"].upper().replace("-EQ", ""),
            record["tradingsymbol"].upper().replace("-EQ", ""),
        )
    ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda record: (
            0
            if record["tradingsymbol"].upper().endswith("-EQ")
            else 1
        )
    )

    return candidates[0]


def history_request(
    session_key,
    segment,
    token,
    timeframe,
    start_dt,
    end_dt,
):
    from_text = start_dt.astimezone(IST).strftime(
        "%d%m%Y%H%M"
    )

    to_text = end_dt.astimezone(IST).strftime(
        "%d%m%Y%H%M"
    )

    url = (
        f"{HISTORY_BASE}/"
        f"{segment}/"
        f"{token}/"
        f"{timeframe}/"
        f"{from_text}/"
        f"{to_text}"
    )

    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                url,
                headers={"Authorization": session_key},
                timeout=45,
            )

            if response.ok:
                return response.text

            last_error = (
                f"HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

            if response.status_code not in (
                429,
                500,
                502,
                503,
                504,
            ):
                break

        except Exception as exc:
            last_error = str(exc)

        time.sleep(0.7 * (attempt + 1))

    raise RuntimeError(
        last_error or "History request failed."
    )


def parse_ticks(raw):
    rows = []

    for line in raw.splitlines():
        if not line.strip():
            continue

        try:
            columns = next(csv.reader([line]))
            epoch = float(columns[0])
            ltp = float(columns[1])
        except Exception:
            continue

        dt = datetime.fromtimestamp(
            epoch,
            tz=timezone.utc,
        ).astimezone(IST)

        def numeric_or_blank(value):
            try:
                return float(value)
            except Exception:
                return ""

        rows.append(
            {
                "utc_seconds": columns[0],
                "timestamp_ist": dt.isoformat(),
                "_dt": dt,
                "ltp": ltp,
                "ltq": (
                    numeric_or_blank(columns[2])
                    if len(columns) > 2
                    else ""
                ),
                "open_interest": (
                    numeric_or_blank(columns[3])
                    if len(columns) > 3
                    else ""
                ),
            }
        )

    return rows


def ticks_to_csv(rows):
    buffer = io.StringIO()

    fields = [
        "utc_seconds",
        "timestamp_ist",
        "ltp",
        "ltq",
        "open_interest",
    ]

    writer = csv.DictWriter(
        buffer,
        fieldnames=fields,
    )

    writer.writeheader()

    for row in rows:
        writer.writerow(
            {
                key: row.get(key, "")
                for key in fields
            }
        )

    return buffer.getvalue()


def minute_to_csv(raw):
    buffer = io.StringIO()

    writer = csv.writer(buffer)

    writer.writerow(
        [
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        ]
    )

    for line in raw.splitlines():
        if not line.strip():
            continue

        columns = next(csv.reader([line]))
        writer.writerow(
            (columns + [""] * 7)[:7]
        )

    return buffer.getvalue()


def nearest_tick(ticks, target_dt):
    if not ticks or target_dt is None:
        return None

    return min(
        ticks,
        key=lambda row: abs(
            (
                row["_dt"]
                - target_dt
            ).total_seconds()
        ),
    )


def ticks_between(ticks, start_dt, end_dt):
    return [
        row
        for row in ticks
        if start_dt <= row["_dt"] <= end_dt
    ]


def group_window_add(plan, key, start_dt, end_dt, metadata):
    if key not in plan:
        plan[key] = {
            "start": start_dt,
            "end": end_dt,
            "metadata": metadata,
        }
        return

    if start_dt < plan[key]["start"]:
        plan[key]["start"] = start_dt

    if end_dt > plan[key]["end"]:
        plan[key]["end"] = end_dt


def option_chain_contracts(
    contract,
    nfo,
    underlying_entry_ltp,
):
    if underlying_entry_ltp is None:
        return []

    symbol = contract["symbol"].upper()
    expiry = contract["expiry"]

    options = [
        record
        for record in nfo
        if record["symbol"].upper() == symbol
        and record["expiry"] == expiry
        and record.get("strike") is not None
        and record["optiontype"].upper() in ("CE", "PE")
    ]

    strikes = sorted(
        {
            float(record["strike"])
            for record in options
        }
    )

    if not strikes:
        return []

    atm_index = min(
        range(len(strikes)),
        key=lambda index: abs(
            strikes[index]
            - underlying_entry_ltp
        ),
    )

    chosen = strikes[
        max(0, atm_index - 1):
        min(len(strikes), atm_index + 2)
    ]

    selected = []

    for strike in chosen:
        for side in ("CE", "PE"):
            hits = [
                record
                for record in options
                if abs(
                    float(record["strike"])
                    - strike
                ) < 1e-9
                and record["optiontype"].upper() == side
            ]

            if hits:
                selected.append(hits[0])

    return selected


def safe_name(value):
    return (
        re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            str(value),
        )
        .strip("._")[:120]
        or "item"
    )


def write_dict_csv(zf, path, rows):
    if not rows:
        zf.writestr(path, "")
        return

    fields = []

    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)

    buffer = io.StringIO()

    writer = csv.DictWriter(
        buffer,
        fieldnames=fields,
    )

    writer.writeheader()
    writer.writerows(rows)

    zf.writestr(
        path,
        buffer.getvalue(),
    )


def compute_trade_metrics(
    trade,
    option_ticks,
    underlying_ticks,
    after_end,
):
    entry_dt = trade["entry_time"]
    exit_dt = trade["exit_time"]

    if not option_ticks or not entry_dt:
        return {}

    entry_tick = nearest_tick(
        option_ticks,
        entry_dt,
    )

    if entry_tick is None:
        return {}

    reported_entry = trade.get(
        "entry_price_reported"
    )

    entry_basis = (
        reported_entry
        if reported_entry not in (None, 0)
        else entry_tick["ltp"]
    )

    result = {
        "entry_tick_time_ist": entry_tick["timestamp_ist"],
        "entry_tick_ltp": entry_tick["ltp"],
        "entry_tick_oi": entry_tick["open_interest"],
        "entry_price_basis": entry_basis,
        "entry_tick_minus_reported": (
            round(
                entry_tick["ltp"] - reported_entry,
                4,
            )
            if reported_entry is not None
            else ""
        ),
    }

    if not exit_dt:
        return result

    exit_tick = nearest_tick(
        option_ticks,
        exit_dt,
    )

    result.update(
        {
            "exit_tick_time_ist": (
                exit_tick["timestamp_ist"]
                if exit_tick
                else ""
            ),
            "exit_tick_ltp": (
                exit_tick["ltp"]
                if exit_tick
                else ""
            ),
            "exit_tick_oi": (
                exit_tick["open_interest"]
                if exit_tick
                else ""
            ),
        }
    )

    reported_exit = trade.get(
        "exit_price_reported"
    )

    if (
        exit_tick
        and reported_exit is not None
    ):
        result["exit_tick_minus_reported"] = round(
            exit_tick["ltp"] - reported_exit,
            4,
        )

    during = ticks_between(
        option_ticks,
        entry_dt,
        exit_dt,
    )

    after = ticks_between(
        option_ticks,
        exit_dt,
        after_end,
    )

    if during:
        peak = max(
            during,
            key=lambda row: row["ltp"],
        )

        trough = min(
            during,
            key=lambda row: row["ltp"],
        )

        peak_oi_rows = [
            row
            for row in during
            if isinstance(
                row.get("open_interest"),
                (int, float),
            )
        ]

        result.update(
            {
                "max_option_ltp_during_trade": peak["ltp"],
                "max_option_ltp_time_ist": peak["timestamp_ist"],
                "min_option_ltp_during_trade": trough["ltp"],
                "min_option_ltp_time_ist": trough["timestamp_ist"],
                "mfe_pct_from_entry_basis": (
                    round(
                        (
                            peak["ltp"]
                            / entry_basis
                            - 1
                        )
                        * 100,
                        4,
                    )
                    if entry_basis
                    else ""
                ),
                "mae_pct_from_entry_basis": (
                    round(
                        (
                            trough["ltp"]
                            / entry_basis
                            - 1
                        )
                        * 100,
                        4,
                    )
                    if entry_basis
                    else ""
                ),
                "minutes_to_mfe": round(
                    (
                        peak["_dt"]
                        - entry_dt
                    ).total_seconds()
                    / 60,
                    2,
                ),
            }
        )

        if peak_oi_rows:
            result["max_open_interest_during_trade"] = max(
                float(
                    row["open_interest"]
                )
                for row in peak_oi_rows
            )

        exit_basis = (
            reported_exit
            if reported_exit is not None
            else (
                exit_tick["ltp"]
                if exit_tick
                else None
            )
        )

        if exit_basis is not None and peak["ltp"]:
            result["peak_to_exit_giveback_pct"] = round(
                (
                    1
                    - exit_basis
                    / peak["ltp"]
                )
                * 100,
                4,
            )

        if (
            entry_basis is not None
            and exit_basis is not None
            and peak["ltp"] > entry_basis
        ):
            result["available_profit_captured_pct"] = round(
                (
                    exit_basis
                    - entry_basis
                )
                / (
                    peak["ltp"]
                    - entry_basis
                )
                * 100,
                4,
            )

    if after:
        post_peak = max(
            after,
            key=lambda row: row["ltp"],
        )

        post_low = min(
            after,
            key=lambda row: row["ltp"],
        )

        result.update(
            {
                "post_exit_max_option_ltp": post_peak["ltp"],
                "post_exit_max_time_ist": post_peak["timestamp_ist"],
                "post_exit_min_option_ltp": post_low["ltp"],
                "post_exit_min_time_ist": post_low["timestamp_ist"],
            }
        )

    if (
        reported_entry is not None
        and reported_exit is not None
        and reported_entry != 0
    ):
        result["reported_option_return_pct"] = round(
            (
                reported_exit
                / reported_entry
                - 1
            )
            * 100,
            4,
        )

    if underlying_ticks:
        u_entry = nearest_tick(
            underlying_ticks,
            entry_dt,
        )

        u_exit = nearest_tick(
            underlying_ticks,
            exit_dt,
        )

        if u_entry:
            result["underlying_entry_ltp"] = u_entry["ltp"]

        if u_exit:
            result["underlying_exit_ltp"] = u_exit["ltp"]

        if u_entry and u_exit and u_entry["ltp"]:
            result["underlying_move_pct"] = round(
                (
                    u_exit["ltp"]
                    / u_entry["ltp"]
                    - 1
                )
                * 100,
                4,
            )

    return result


def execute_history_plan(
    session_key,
    plan,
    job_id,
    progress_start,
    progress_end,
    label,
):
    results = {}
    errors = {}

    items = list(plan.items())

    if not items:
        return results, errors

    total = len(items)

    def task(item):
        key, details = item

        raw = history_request(
            session_key,
            details["metadata"]["segment"],
            details["metadata"]["token"],
            details["metadata"]["timeframe"],
            details["start"],
            details["end"],
        )

        return key, raw

    completed = 0

    with ThreadPoolExecutor(
        max_workers=HISTORY_WORKERS
    ) as executor:
        future_map = {
            executor.submit(task, item): item[0]
            for item in items
        }

        for future in as_completed(future_map):
            key = future_map[future]

            try:
                result_key, raw = future.result()
                results[result_key] = raw
            except Exception as exc:
                errors[key] = str(exc)

            completed += 1

            pct = (
                progress_start
                + (
                    progress_end
                    - progress_start
                )
                * completed
                / total
            )

            set_job(
                job_id,
                progress=round(pct),
                message=(
                    f"{label}: "
                    f"{completed}/{total}"
                ),
            )

    return results, errors


def build_v5_package(
    job_id,
    session_key,
    input_bytes,
    input_filename,
    before_minutes,
    after_minutes,
    include_chain,
):
    cleanup_jobs()

    set_job(
        job_id,
        status="running",
        progress=3,
        message="Reading AlgoStra ZIP...",
    )

    parsed = parse_batch_zip(input_bytes)
    trades = parsed["positions"]

    set_job(
        job_id,
        progress=8,
        message=(
            f"Detected {len(trades)} strategy trades "
            f"across the uploaded CSV bundle."
        ),
    )

    nfo_raw = download_master(NFO_MASTER_URL)
    nse_raw = download_master(NSE_MASTER_URL)

    nfo = parse_master(nfo_raw)
    nse = parse_master(nse_raw)

    nfo_by_ts = {
        record["tradingsymbol"].upper(): record
        for record in nfo
    }

    resolved_trades = []
    unresolved = []

    for index, trade in enumerate(
        trades,
        start=1,
    ):
        contract = resolve_option(
            trade["tradingsymbol"],
            nfo_by_ts,
        )

        if not contract:
            unresolved.append(
                {
                    "trade_number": index,
                    "strategy_name": trade["strategy_name"],
                    "tradingsymbol": trade["tradingsymbol"],
                    "reason": (
                        "Exact option was not found "
                        "in the current Definedge NFO master."
                    ),
                }
            )
            continue

        underlying_rec = resolve_underlying(
            contract["symbol"],
            nse,
        )

        trade_copy = dict(trade)
        trade_copy["trade_number"] = index
        trade_copy["contract"] = contract
        trade_copy["underlying_record"] = underlying_rec

        resolved_trades.append(trade_copy)

    if not resolved_trades:
        raise RuntimeError(
            "None of the AlgoStra position trades could be "
            "matched to Definedge NFO contracts."
        )

    set_job(
        job_id,
        progress=12,
        message=(
            f"Resolved {len(resolved_trades)} trades. "
            "Planning deduplicated market-data downloads..."
        ),
    )

    option_plan = {}
    option_minute_plan = {}
    underlying_plan = {}
    underlying_minute_plan = {}

    for trade in resolved_trades:
        entry_dt = trade["entry_time"]
        exit_dt = trade["exit_time"] or entry_dt

        start_dt = entry_dt - timedelta(
            minutes=before_minutes
        )

        end_dt = exit_dt + timedelta(
            minutes=after_minutes
        )

        contract = trade["contract"]

        date_key = entry_dt.date().isoformat()

        option_key = (
            contract["token"],
            date_key,
        )

        group_window_add(
            option_plan,
            option_key,
            start_dt,
            end_dt,
            {
                "segment": contract["segment"],
                "token": contract["token"],
                "tradingsymbol": contract["tradingsymbol"],
                "timeframe": "tick",
            },
        )

        group_window_add(
            option_minute_plan,
            option_key,
            start_dt,
            end_dt,
            {
                "segment": contract["segment"],
                "token": contract["token"],
                "tradingsymbol": contract["tradingsymbol"],
                "timeframe": "minute",
            },
        )

        underlying = trade["underlying_record"]

        if underlying:
            underlying_key = (
                underlying["token"],
                date_key,
            )

            group_window_add(
                underlying_plan,
                underlying_key,
                start_dt,
                end_dt,
                {
                    "segment": underlying["segment"],
                    "token": underlying["token"],
                    "tradingsymbol": underlying["tradingsymbol"],
                    "timeframe": "tick",
                },
            )

            group_window_add(
                underlying_minute_plan,
                underlying_key,
                start_dt,
                end_dt,
                {
                    "segment": underlying["segment"],
                    "token": underlying["token"],
                    "tradingsymbol": underlying["tradingsymbol"],
                    "timeframe": "minute",
                },
            )

    option_raw, option_errors = execute_history_plan(
        session_key,
        option_plan,
        job_id,
        12,
        30,
        "Exact option ticks",
    )

    option_minute_raw, option_minute_errors = execute_history_plan(
        session_key,
        option_minute_plan,
        job_id,
        30,
        38,
        "Option 1-minute backup",
    )

    underlying_raw, underlying_errors = execute_history_plan(
        session_key,
        underlying_plan,
        job_id,
        38,
        50,
        "Underlying ticks",
    )

    underlying_minute_raw, underlying_minute_errors = execute_history_plan(
        session_key,
        underlying_minute_plan,
        job_id,
        50,
        56,
        "Underlying 1-minute backup",
    )

    option_ticks = {
        key: parse_ticks(raw)
        for key, raw in option_raw.items()
    }

    underlying_ticks = {
        key: parse_ticks(raw)
        for key, raw in underlying_raw.items()
    }

    # Build chain plan only after underlying entry LTP is known.
    chain_plan = {}
    chain_refs_by_trade = defaultdict(list)

    if include_chain:
        for trade in resolved_trades:
            entry_dt = trade["entry_time"]
            exit_dt = trade["exit_time"] or entry_dt
            start_dt = entry_dt - timedelta(
                minutes=before_minutes
            )
            end_dt = exit_dt + timedelta(
                minutes=after_minutes
            )
            date_key = entry_dt.date().isoformat()

            underlying = trade["underlying_record"]

            if not underlying:
                continue

            ukey = (
                underlying["token"],
                date_key,
            )

            u_ticks = underlying_ticks.get(
                ukey,
                [],
            )

            u_entry = nearest_tick(
                u_ticks,
                entry_dt,
            )

            if not u_entry:
                continue

            chain_contracts = option_chain_contracts(
                trade["contract"],
                nfo,
                u_entry["ltp"],
            )

            for chain_contract in chain_contracts:
                chain_key = (
                    chain_contract["token"],
                    date_key,
                )

                group_window_add(
                    chain_plan,
                    chain_key,
                    start_dt,
                    end_dt,
                    {
                        "segment": chain_contract["segment"],
                        "token": chain_contract["token"],
                        "tradingsymbol": chain_contract["tradingsymbol"],
                        "timeframe": "tick",
                    },
                )

                chain_refs_by_trade[
                    trade["trade_number"]
                ].append(
                    {
                        "tradingsymbol": chain_contract["tradingsymbol"],
                        "token": chain_contract["token"],
                        "strike": chain_contract["strike"],
                        "optiontype": chain_contract["optiontype"],
                        "date_key": date_key,
                    }
                )

    chain_raw, chain_errors = execute_history_plan(
        session_key,
        chain_plan,
        job_id,
        56,
        78,
        "Entry ATM chain ticks",
    )

    chain_ticks = {
        key: parse_ticks(raw)
        for key, raw in chain_raw.items()
    }

    set_job(
        job_id,
        progress=80,
        message="Calculating MFE, MAE and strategy comparisons...",
    )

    broker_payloads = {
        "orders": broker_get(
            session_key,
            "/orders",
        ),
        "trades": broker_get(
            session_key,
            "/trades",
        ),
        "positions": broker_get(
            session_key,
            "/positions",
        ),
        "limits": broker_get(
            session_key,
            "/limits",
        ),
    }

    all_trade_rows = []
    metric_rows = []
    duplicate_groups = defaultdict(list)
    per_trade_metadata = []

    for trade in resolved_trades:
        contract = trade["contract"]
        entry_dt = trade["entry_time"]
        exit_dt = trade["exit_time"]
        date_key = entry_dt.date().isoformat()

        option_key = (
            contract["token"],
            date_key,
        )

        exact_ticks = option_ticks.get(
            option_key,
            [],
        )

        underlying_ticks_for_trade = []

        if trade["underlying_record"]:
            underlying_key = (
                trade["underlying_record"]["token"],
                date_key,
            )

            underlying_ticks_for_trade = underlying_ticks.get(
                underlying_key,
                [],
            )

        after_end = (
            (exit_dt or entry_dt)
            + timedelta(
                minutes=after_minutes
            )
        )

        metrics = compute_trade_metrics(
            trade,
            exact_ticks,
            underlying_ticks_for_trade,
            after_end,
        )

        base_row = {
            "trade_number": trade["trade_number"],
            "strategy_name": trade["strategy_name"],
            "source_filename": trade["source_filename"],
            "source_row": trade["source_row"],
            "leg": trade["leg"],
            "underlying": trade["underlying"],
            "tradingsymbol": contract["tradingsymbol"],
            "definedge_token": contract["token"],
            "expiry": contract["expiry"],
            "strike": contract["strike"],
            "optiontype": contract["optiontype"],
            "quantity": trade["quantity"],
            "entry_time_ist": entry_dt.isoformat(),
            "exit_time_ist": (
                exit_dt.isoformat()
                if exit_dt
                else ""
            ),
            "entry_price_reported": trade["entry_price_reported"],
            "exit_price_reported": trade["exit_price_reported"],
            "pnl_reported_value": trade["pnl_reported_value"],
            "pnl_reported_text": trade["pnl_reported_text"],
            "order_status": trade["order_status"],
        }

        all_trade_rows.append(base_row)

        metric_rows.append(
            {
                **base_row,
                **metrics,
            }
        )

        duplicate_key = (
            f"{contract['tradingsymbol']}|"
            f"{entry_dt.isoformat()}"
        )

        duplicate_groups[duplicate_key].append(
            trade
        )

        chain_refs = chain_refs_by_trade.get(
            trade["trade_number"],
            [],
        )

        per_trade_metadata.append(
            {
                **base_row,
                "chain_contracts": chain_refs,
                "market_data_option_path": (
                    f"market_data/options/"
                    f"{safe_name(contract['tradingsymbol'])}/"
                ),
                "market_data_underlying_path": (
                    (
                        f"market_data/underlyings/"
                        f"{safe_name(trade['underlying_record']['tradingsymbol'])}/"
                    )
                    if trade["underlying_record"]
                    else ""
                ),
                "metrics": metrics,
            }
        )

    duplicate_rows = []

    group_number = 0

    for duplicate_key, group in sorted(
        duplicate_groups.items()
    ):
        if len(group) < 2:
            continue

        group_number += 1
        group_id = f"DUP-{group_number:03d}"

        for trade in group:
            duplicate_rows.append(
                {
                    "duplicate_group_id": group_id,
                    "tradingsymbol": trade["contract"]["tradingsymbol"],
                    "entry_time_ist": trade["entry_time"].isoformat(),
                    "strategy_name": trade["strategy_name"],
                    "exit_time_ist": (
                        trade["exit_time"].isoformat()
                        if trade["exit_time"]
                        else ""
                    ),
                    "entry_price_reported": trade["entry_price_reported"],
                    "exit_price_reported": trade["exit_price_reported"],
                    "pnl_reported_value": trade["pnl_reported_value"],
                }
            )

    strategy_metrics = defaultdict(list)

    for row in metric_rows:
        strategy_metrics[
            row["strategy_name"]
        ].append(row)

    strategy_comparison = []

    for strategy_name, rows in sorted(
        strategy_metrics.items()
    ):
        pnl_values = [
            row["pnl_reported_value"]
            for row in rows
            if isinstance(
                row["pnl_reported_value"],
                (int, float),
            )
        ]

        returns = [
            row["reported_option_return_pct"]
            for row in rows
            if isinstance(
                row.get("reported_option_return_pct"),
                (int, float),
            )
        ]

        mfes = [
            row["mfe_pct_from_entry_basis"]
            for row in rows
            if isinstance(
                row.get("mfe_pct_from_entry_basis"),
                (int, float),
            )
        ]

        maes = [
            row["mae_pct_from_entry_basis"]
            for row in rows
            if isinstance(
                row.get("mae_pct_from_entry_basis"),
                (int, float),
            )
        ]

        givebacks = [
            row["peak_to_exit_giveback_pct"]
            for row in rows
            if isinstance(
                row.get("peak_to_exit_giveback_pct"),
                (int, float),
            )
        ]

        activity = parsed["strategy_activity"].get(
            strategy_name,
            {},
        )

        strategy_comparison.append(
            {
                "strategy_name": strategy_name,
                "trade_count": len(rows),
                "unique_contracts": len(
                    {
                        row["tradingsymbol"]
                        for row in rows
                    }
                ),
                "reported_total_pnl": (
                    round(sum(pnl_values), 2)
                    if pnl_values
                    else ""
                ),
                "winning_trades": sum(
                    1
                    for value in pnl_values
                    if value > 0
                ),
                "losing_trades": sum(
                    1
                    for value in pnl_values
                    if value < 0
                ),
                "avg_reported_option_return_pct": (
                    round(
                        sum(returns)
                        / len(returns),
                        4,
                    )
                    if returns
                    else ""
                ),
                "avg_mfe_pct": (
                    round(
                        sum(mfes)
                        / len(mfes),
                        4,
                    )
                    if mfes
                    else ""
                ),
                "avg_mae_pct": (
                    round(
                        sum(maes)
                        / len(maes),
                        4,
                    )
                    if maes
                    else ""
                ),
                "avg_peak_to_exit_giveback_pct": (
                    round(
                        sum(givebacks)
                        / len(givebacks),
                        4,
                    )
                    if givebacks
                    else ""
                ),
                "activity_log_found": activity.get(
                    "activity_file_found",
                    False,
                ),
                "max_loss_triggered": activity.get(
                    "max_loss_triggered",
                    False,
                ),
                "square_off_seen": activity.get(
                    "square_off_seen",
                    False,
                ),
                "activity_last_event_ist": activity.get(
                    "last_event_ist",
                    "",
                ),
            }
        )

    activity_summary_rows = list(
        parsed["strategy_activity"].values()
    )

    activity_event_rows = []

    for event in parsed["activity_events"]:
        activity_event_rows.append(
            {
                "source_filename": event["source_filename"],
                "datetime_ist": (
                    event["datetime"].isoformat()
                    if event["datetime"]
                    else ""
                ),
                "category": event["category"],
                "message": event["message"],
            }
        )

    data_quality = [
        {
            "check": "CSV files detected",
            "value": len(parsed["file_report"]),
        },
        {
            "check": "Position trades detected",
            "value": len(trades),
        },
        {
            "check": "Position trades resolved to Definedge",
            "value": len(resolved_trades),
        },
        {
            "check": "Unresolved trades",
            "value": len(unresolved),
        },
        {
            "check": "Unique exact option tick downloads",
            "value": len(option_plan),
        },
        {
            "check": "Unique underlying tick downloads",
            "value": len(underlying_plan),
        },
        {
            "check": "Unique chain tick downloads",
            "value": len(chain_plan),
        },
        {
            "check": "Option tick request errors",
            "value": len(option_errors),
        },
        {
            "check": "Underlying tick request errors",
            "value": len(underlying_errors),
        },
        {
            "check": "Chain tick request errors",
            "value": len(chain_errors),
        },
    ]

    set_job(
        job_id,
        progress=88,
        message="Writing the complete analysis ZIP...",
    )

    stamp = datetime.now(IST).strftime(
        "%Y-%m-%d_%H%M%S"
    )

    output_path = (
        TMP_DIR
        / f"Momentum_Full_Analysis_V5_{stamp}_{job_id[:6]}.zip"
    )

    with zipfile.ZipFile(
        output_path,
        "w",
        zipfile.ZIP_DEFLATED,
    ) as zf:
        # Preserve user's original source bundle.
        for name, raw in parsed["originals"].items():
            zf.writestr(
                f"algostra_original/{name}",
                raw,
            )

        # Master snapshots.
        zf.writestr(
            "reference/nfo_master_snapshot.csv",
            nfo_raw,
        )

        zf.writestr(
            "reference/nse_master_snapshot.csv",
            nse_raw,
        )

        # Broker context.
        for name, payload in broker_payloads.items():
            zf.writestr(
                f"broker/{name}.json",
                json.dumps(
                    payload,
                    indent=2,
                    ensure_ascii=False,
                ),
            )

            zf.writestr(
                f"broker/{name}.csv",
                payload_to_csv(
                    payload,
                    name,
                ),
            )

        # Exact option data, deduplicated.
        for key, details in option_plan.items():
            token, date_key = key
            tradingsymbol = details["metadata"]["tradingsymbol"]
            base = (
                f"market_data/options/"
                f"{safe_name(tradingsymbol)}"
            )

            ticks = option_ticks.get(
                key,
                [],
            )

            if ticks:
                zf.writestr(
                    f"{base}/tick_{date_key}.csv",
                    ticks_to_csv(ticks),
                )

            minute_raw = option_minute_raw.get(
                key,
            )

            if minute_raw is not None:
                zf.writestr(
                    f"{base}/minute_{date_key}.csv",
                    minute_to_csv(minute_raw),
                )

        # Underlying data, deduplicated.
        for key, details in underlying_plan.items():
            token, date_key = key
            symbol = details["metadata"]["tradingsymbol"]
            base = (
                f"market_data/underlyings/"
                f"{safe_name(symbol)}"
            )

            ticks = underlying_ticks.get(
                key,
                [],
            )

            if ticks:
                zf.writestr(
                    f"{base}/tick_{date_key}.csv",
                    ticks_to_csv(ticks),
                )

            minute_raw = underlying_minute_raw.get(
                key,
            )

            if minute_raw is not None:
                zf.writestr(
                    f"{base}/minute_{date_key}.csv",
                    minute_to_csv(minute_raw),
                )

        # Chain data. If chain token is also an exact traded option,
        # it may exist in both logical uses but is still downloaded once.
        for key, details in chain_plan.items():
            token, date_key = key
            tradingsymbol = details["metadata"]["tradingsymbol"]
            base = (
                f"market_data/option_chain/"
                f"{safe_name(tradingsymbol)}"
            )

            ticks = chain_ticks.get(
                key,
                [],
            )

            if ticks:
                zf.writestr(
                    f"{base}/tick_{date_key}.csv",
                    ticks_to_csv(ticks),
                )

        # Analysis tables.
        write_dict_csv(
            zf,
            "analysis/all_strategy_trades.csv",
            all_trade_rows,
        )

        write_dict_csv(
            zf,
            "analysis/trade_metrics.csv",
            metric_rows,
        )

        write_dict_csv(
            zf,
            "analysis/strategy_comparison.csv",
            strategy_comparison,
        )

        write_dict_csv(
            zf,
            "analysis/duplicate_trade_map.csv",
            duplicate_rows,
        )

        write_dict_csv(
            zf,
            "analysis/activity_log_summary.csv",
            activity_summary_rows,
        )

        write_dict_csv(
            zf,
            "analysis/activity_log_events.csv",
            activity_event_rows,
        )

        write_dict_csv(
            zf,
            "analysis/input_file_report.csv",
            parsed["file_report"],
        )

        write_dict_csv(
            zf,
            "analysis/unresolved_trades.csv",
            unresolved,
        )

        write_dict_csv(
            zf,
            "analysis/data_quality.csv",
            data_quality,
        )

        zf.writestr(
            "manifest/trades.json",
            json.dumps(
                per_trade_metadata,
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
        )

        all_errors = {
            "option_tick_errors": {
                str(key): value
                for key, value in option_errors.items()
            },
            "option_minute_errors": {
                str(key): value
                for key, value in option_minute_errors.items()
            },
            "underlying_tick_errors": {
                str(key): value
                for key, value in underlying_errors.items()
            },
            "underlying_minute_errors": {
                str(key): value
                for key, value in underlying_minute_errors.items()
            },
            "chain_tick_errors": {
                str(key): value
                for key, value in chain_errors.items()
            },
        }

        zf.writestr(
            "manifest/api_errors.json",
            json.dumps(
                all_errors,
                indent=2,
                ensure_ascii=False,
            ),
        )

        zf.writestr(
            "README_OUTPUT.txt",
            f"""MOMENTUM FULL ANALYSIS V5

INPUT
-----
{input_filename}

WHAT IS INSIDE
--------------
1. algostra_original/
   Your original Positions and Activity Log CSV files.

2. market_data/options/
   Exact traded option tick data:
   timestamp, LTP, LTQ, Open Interest.
   Also 1-minute backup data.

3. market_data/underlyings/
   Matching stock tick and 1-minute data.

4. market_data/option_chain/
   Entry-time ATM-1, ATM and ATM+1 contracts for both CE and PE
   when the chain option is enabled.

5. analysis/trade_metrics.csv
   Main file for ChatGPT analysis:
   MFE, MAE, peak-to-exit giveback, post-exit movement,
   underlying movement and reported-vs-tick comparison.

6. analysis/strategy_comparison.csv
   Comparison of T7 / T8 / SL12 / V1 / LIQ20 / LIQ50 / LIQINST variants.

7. analysis/duplicate_trade_map.csv
   Shows the same market trade repeated across strategy variants.
   Definedge market data is downloaded once and reused.

8. analysis/activity_log_summary.csv
   Strategy-log information including max-loss / square-off / stop events.

IMPORTANT
---------
Definedge tick history is time-limited. Keep this ZIP permanently.
Upload this complete ZIP into the ChatGPT Momentum Trading project
for analysis against the strategy history stored there.

Resolved trades: {len(resolved_trades)}
Unresolved trades: {len(unresolved)}
Exact option streams downloaded: {len(option_plan)}
Underlying streams downloaded: {len(underlying_plan)}
Chain streams requested: {len(chain_plan)}
""",
        )

    set_job(
        job_id,
        status="done",
        progress=100,
        message=(
            "Completed. "
            f"{len(resolved_trades)} trades resolved; "
            f"{len(unresolved)} unresolved."
        ),
        output_path=str(output_path),
        output_filename=output_path.name,
        summary={
            "position_trades_detected": len(trades),
            "resolved_trades": len(resolved_trades),
            "unresolved_trades": len(unresolved),
            "unique_exact_option_streams": len(option_plan),
            "unique_underlying_streams": len(underlying_plan),
            "unique_chain_streams": len(chain_plan),
        },
    )


def job_worker(
    job_id,
    session_key,
    input_bytes,
    input_filename,
    before_minutes,
    after_minutes,
    include_chain,
):
    try:
        build_v5_package(
            job_id,
            session_key,
            input_bytes,
            input_filename,
            before_minutes,
            after_minutes,
            include_chain,
        )
    except Exception as exc:
        set_job(
            job_id,
            status="error",
            progress=100,
            message=str(exc),
        )


@app.get("/")
def home():
    cleanup_jobs()
    return render_template(
        "index.html",
        otp_sent=False,
    )


@app.post("/send-otp")
def send_otp_route():
    try:
        password = request.form.get(
            "collector_password",
            "",
        )

        state_id = send_definedge_otp(
            password
        )

        response = make_response(
            render_template(
                "index.html",
                otp_sent=True,
                message=(
                    "OTP sent. Password accepted for this session."
                ),
            )
        )

        response.set_cookie(
            "de_state",
            state_id,
            max_age=OTP_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
        )

        return response

    except Exception as exc:
        return render_template(
            "index.html",
            otp_sent=False,
            error=str(exc),
        ), 500


@app.post("/collect")
def collect_route():
    try:
        otp = request.form.get(
            "otp",
            "",
        ).strip()

        upload = request.files.get(
            "batch_zip"
        )

        state_id = request.cookies.get(
            "de_state",
            "",
        )

        if not otp:
            raise RuntimeError(
                "Enter the Definedge OTP."
            )

        if not upload or not upload.filename:
            raise RuntimeError(
                "Upload the full AlgoStra ZIP."
            )

        if not upload.filename.lower().endswith(
            ".zip"
        ):
            raise RuntimeError(
                "V5 expects one ZIP containing the AlgoStra CSV files."
            )

        input_bytes = upload.read()

        if not input_bytes:
            raise RuntimeError(
                "Uploaded ZIP is empty."
            )

        # Validate ZIP now so bad input fails immediately.
        test_zf, _ = safe_zip_input(
            input_bytes
        )
        test_zf.close()

        before_minutes = max(
            0,
            min(
                60,
                int(
                    request.form.get(
                        "before_min",
                        "3",
                    )
                ),
            ),
        )

        after_minutes = max(
            0,
            min(
                180,
                int(
                    request.form.get(
                        "after_min",
                        "15",
                    )
                ),
            ),
        )

        include_chain = (
            request.form.get(
                "include_chain"
            )
            == "yes"
        )

        session_key = authenticate(
            state_id,
            otp,
        )

        job_id = secrets.token_urlsafe(18)

        with JOB_LOCK:
            JOBS[job_id] = {
                "created": time.time(),
                "status": "queued",
                "progress": 1,
                "message": "Starting...",
                "output_path": "",
                "output_filename": "",
                "summary": {},
            }

        thread = threading.Thread(
            target=job_worker,
            kwargs={
                "job_id": job_id,
                "session_key": session_key,
                "input_bytes": input_bytes,
                "input_filename": upload.filename,
                "before_minutes": before_minutes,
                "after_minutes": after_minutes,
                "include_chain": include_chain,
            },
            daemon=True,
        )

        thread.start()

        response = make_response(
            render_template(
                "job.html",
                job_id=job_id,
            )
        )

        response.delete_cookie(
            "de_state"
        )

        return response

    except Exception as exc:
        return render_template(
            "index.html",
            otp_sent=True,
            error=str(exc),
        ), 500


@app.get("/status/<job_id>")
def status_route(job_id):
    cleanup_jobs()

    with JOB_LOCK:
        job = JOBS.get(job_id)

        if not job:
            return jsonify(
                {
                    "status": "missing",
                    "progress": 100,
                    "message": (
                        "This job expired or the service restarted."
                    ),
                }
            ), 404

        return jsonify(
            {
                "status": job["status"],
                "progress": job["progress"],
                "message": job["message"],
                "summary": job.get(
                    "summary",
                    {},
                ),
                "download_ready": (
                    job["status"] == "done"
                    and bool(
                        job.get(
                            "output_path"
                        )
                    )
                ),
            }
        )


@app.get("/download/<job_id>")
def download_route(job_id):
    cleanup_jobs()

    with JOB_LOCK:
        job = JOBS.get(job_id)

        if not job:
            return "Job expired.", 404

        if job["status"] != "done":
            return "Job is not complete.", 409

        output_path = job.get(
            "output_path"
        )

        output_filename = job.get(
            "output_filename"
        )

    if not output_path:
        return "Output file unavailable.", 404

    path = Path(output_path)

    if not path.exists():
        return "Output file expired.", 404

    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=output_filename,
        max_age=0,
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "5.0",
    }


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000",
            )
        ),
    )
