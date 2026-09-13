import hashlib
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API_ROOT = os.getenv(
    "SHOONYA_API_BASE",
    "https://api.shoonya.com/NorenWClientTP/",
).rstrip("/") + "/"
IST = ZoneInfo("Asia/Kolkata")

REQUIRED_VARS = (
    "SHOONYA_USER_ID",
    "SHOONYA_PASSWORD",
    "SHOONYA_VENDOR_CODE",
    "SHOONYA_API_SECRET",
    "SHOONYA_IMEI",
)


def config_status():
    presence = {
        name: bool(os.getenv(name, "").strip())
        for name in REQUIRED_VARS
    }
    missing = [name for name, present in presence.items() if not present]
    return {
        "status": "ready" if not missing else "configuration_incomplete",
        "configured": not missing,
        "missing": missing,
        "presence": presence,
        "read_only_probe": True,
        "orders_enabled": False,
    }


def _api_call(action, payload, session_key=None):
    body = {
        "jData": json.dumps(payload, separators=(",", ":")),
    }
    if session_key:
        body["jKey"] = session_key

    response = requests.post(
        API_ROOT + action,
        data=body,
        timeout=30,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"stat": "Not_Ok", "emsg": "Shoonya returned non-JSON data."}

    if not isinstance(data, dict) and not isinstance(data, list):
        data = {"stat": "Not_Ok", "emsg": "Shoonya returned an unexpected response."}

    return response.status_code, data


def _require_config():
    missing = config_status()["missing"]
    if missing:
        raise RuntimeError(
            "Shoonya configuration is incomplete: " + ", ".join(missing)
        )
    return {
        name: os.getenv(name, "").strip()
        for name in REQUIRED_VARS
    }


def _failure(data, http_status):
    if isinstance(data, dict):
        message = str(data.get("emsg") or data.get("message") or "Shoonya API request failed.")
    else:
        message = "Shoonya API request failed."
    raise RuntimeError(f"{message} (HTTP {http_status})")


def _nifty_instrument(values):
    if not isinstance(values, list):
        return None
    for item in values:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("tsym") or "").strip().upper()
        if symbol in {"NIFTY 50", "NIFTY50"}:
            return {
                "exchange": str(item.get("exch") or "NSE"),
                "token": str(item.get("token") or "26000"),
                "tradingsymbol": item.get("tsym") or "Nifty 50",
            }
    return None


def _safe_quote(data):
    if not isinstance(data, dict):
        return {}
    allowed = (
        "stat",
        "request_time",
        "exch",
        "token",
        "tsym",
        "lp",
        "pc",
        "o",
        "h",
        "l",
        "c",
        "ap",
        "v",
        "oi",
        "ltt",
        "bp1",
        "sp1",
        "bq1",
        "sq1",
    )
    return {key: data[key] for key in allowed if key in data}


def _safe_bar(row):
    if not isinstance(row, dict):
        return {}
    allowed = (
        "stat",
        "time",
        "ssboe",
        "into",
        "inth",
        "intl",
        "intc",
        "intv",
        "intvwap",
        "v",
        "oi",
    )
    return {key: row[key] for key in allowed if key in row}


def run_probe(factor2, lookback_days=7):
    cfg = _require_config()
    factor = str(factor2 or "").strip()
    if not factor:
        raise RuntimeError("OTP/TOTP is required for the read-only Shoonya probe.")

    login_payload = {
        "apkversion": "1.0.0",
        "uid": cfg["SHOONYA_USER_ID"],
        "pwd": hashlib.sha256(
            cfg["SHOONYA_PASSWORD"].encode("utf-8")
        ).hexdigest(),
        "factor2": factor,
        "vc": cfg["SHOONYA_VENDOR_CODE"],
        "appkey": cfg["SHOONYA_API_SECRET"],
        "imei": cfg["SHOONYA_IMEI"],
        "source": "API",
    }
    login_status, login_data = _api_call("QuickAuth", login_payload)
    if login_status >= 400 or not isinstance(login_data, dict) or login_data.get("stat") != "Ok":
        _failure(login_data, login_status)

    session_key = str(login_data.get("susertoken") or "").strip()
    if not session_key:
        raise RuntimeError("Shoonya login succeeded but returned no session token.")

    instrument = None
    search_status, search_data = _api_call(
        "SearchScrip",
        {
            "uid": cfg["SHOONYA_USER_ID"],
            "exch": "NSE",
            "stext": "Nifty 50",
        },
        session_key,
    )
    if search_status < 400 and isinstance(search_data, dict):
        instrument = _nifty_instrument(search_data.get("values"))

    if instrument is None:
        instrument = {
            "exchange": "NSE",
            "token": "26000",
            "tradingsymbol": "Nifty 50",
        }

    quote_status, quote_data = _api_call(
        "GetQuotes",
        {
            "uid": cfg["SHOONYA_USER_ID"],
            "exch": instrument["exchange"],
            "token": instrument["token"],
        },
        session_key,
    )
    if quote_status >= 400 or not isinstance(quote_data, dict) or quote_data.get("stat") != "Ok":
        _failure(quote_data, quote_status)

    now = datetime.now(IST)
    start = now - timedelta(days=max(1, min(int(lookback_days), 30)))
    series_status, series_data = _api_call(
        "TPSeries",
        {
            "uid": cfg["SHOONYA_USER_ID"],
            "exch": instrument["exchange"],
            "token": instrument["token"],
            "st": str(int(start.timestamp())),
            "et": str(int(now.timestamp())),
            "intrv": "5",
        },
        session_key,
    )
    if series_status >= 400 or not isinstance(series_data, list):
        _failure(series_data, series_status)

    bars = [_safe_bar(row) for row in series_data if isinstance(row, dict)]
    bars = [row for row in bars if row]
    return {
        "status": "ok",
        "read_only_probe": True,
        "orders_enabled": False,
        "login": {
            "stat": login_data.get("stat"),
            "lastaccesstime": login_data.get("lastaccesstime"),
        },
        "instrument": instrument,
        "quote": _safe_quote(quote_data),
        "historical_5m": {
            "lookback_days": max(1, min(int(lookback_days), 30)),
            "bar_count": len(bars),
            "first_bar": bars[-1] if bars else None,
            "last_bar": bars[0] if bars else None,
        },
    }
