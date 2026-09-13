#!/usr/bin/env python3
"""Versioned, local-only Shoonya market-data relay.

Shoonya authentication and history requests originate from the user's own
computer. Only sanitized NIFTY 50 five-minute data is uploaded to Render.
This module deliberately contains no order placement, modification, or
cancellation functions.
"""

import csv
import hashlib
import html
import http.server
import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

RELAY_VERSION = "2.0.0"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / ".env.shoonya"
if not CONFIG_PATH.exists() and (BASE_DIR / "env.shoonya").exists():
    CONFIG_PATH = BASE_DIR / "env.shoonya"

DEFAULT_RENDER_URL = "https://definedge-momentum-batch-collector-v6.onrender.com"
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = int(os.getenv("SHOONYA_RELAY_PORT", "8766"))
LOCAL_URL = f"http://{LOCAL_HOST}:{LOCAL_PORT}/"
API_ROOT = (
    os.getenv("SHOONYA_API_BASE", "https://api.shoonya.com/NorenWClientTP/")
    .rstrip("/")
    + "/"
)
IST = ZoneInfo("Asia/Kolkata")


def load_dotenv(path):
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


CONFIG = load_dotenv(CONFIG_PATH)
CONFIG.update(
    {
        key: value
        for key, value in os.environ.items()
        if key.startswith("SHOONYA_")
        or key
        in {
            "COLLECTOR_PASSWORD",
            "RENDER_COLLECTOR_URL",
            "LOOKBACK_DAYS",
        }
    }
)


def cfg(name, default=""):
    return str(CONFIG.get(name, default) or "").strip()


def required_missing():
    names = (
        "SHOONYA_USER_ID",
        "SHOONYA_PASSWORD",
        "SHOONYA_VENDOR_CODE",
        "SHOONYA_API_SECRET",
    )
    return [name for name in names if not cfg(name)]


def local_device_id():
    """Return configured Shoonya IMEI/device ID or derive the desktop MAC."""
    configured = cfg("SHOONYA_IMEI")
    if configured and configured.lower() not in {
        "auto",
        "shoonya-local-01",
        "your_imei_or_device_id",
    }:
        return configured
    return f"{uuid.getnode():012x}"


def build_login_payload(factor2):
    """Build Shoonya QuickAuth payload using its documented hash contract."""
    user_id = cfg("SHOONYA_USER_ID")
    api_secret = cfg("SHOONYA_API_SECRET")
    return {
        "apkversion": "1.0.0",
        "uid": user_id,
        "pwd": hashlib.sha256(
            cfg("SHOONYA_PASSWORD").encode("utf-8")
        ).hexdigest(),
        "factor2": str(factor2 or "").strip(),
        "vc": cfg("SHOONYA_VENDOR_CODE"),
        "appkey": hashlib.sha256(
            f"{user_id}|{api_secret}".encode("utf-8")
        ).hexdigest(),
        "imei": local_device_id(),
        "source": "API",
    }


def api_call(action, payload, session_key=None):
    body = {"jData": json.dumps(payload, separators=(",", ":"))}
    if session_key:
        body["jKey"] = session_key
    encoded = urllib.parse.urlencode(body).encode("utf-8")
    last_status = 0
    last_text = ""
    for attempt in range(3):
        request = urllib.request.Request(
            API_ROOT + action,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": f"ShoonyaLocalRelay/{RELAY_VERSION}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                last_status = response.status
                last_text = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_text = exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            last_status = 0
            last_text = str(exc)
        if last_status not in {502, 503, 504}:
            break
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    try:
        data = json.loads(last_text)
    except (TypeError, ValueError):
        data = {
            "stat": "Not_Ok",
            "emsg": "Shoonya returned non-JSON data.",
        }
    return last_status, data


def safe_quote(data):
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
    )
    return {key: data[key] for key in allowed if key in data}


def safe_bar(data):
    if not isinstance(data, dict):
        return {}
    allowed = (
        "time",
        "ssboe",
        "into",
        "inth",
        "intl",
        "intc",
        "intv",
        "intvwap",
        "v",
        "intoi",
        "oi",
    )
    return {key: data[key] for key in allowed if key in data}


def bar_epoch(row):
    """Return a stable epoch for a TPSeries bar, interpreting text as IST."""
    raw_epoch = str(row.get("ssboe") or "").strip()
    if raw_epoch:
        try:
            value = int(float(raw_epoch))
            if value > 10_000_000_000:
                value //= 1000
            return value
        except ValueError:
            pass

    raw_time = str(row.get("time") or "").strip()
    formats = (
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw_time, fmt).replace(tzinfo=IST)
            return int(parsed.timestamp())
        except ValueError:
            continue
    raise RuntimeError(
        "Shoonya returned a 5-minute bar with an unrecognized timestamp."
    )


def normalize_bars(rows):
    """Sanitize, deduplicate, and sort TPSeries bars oldest to newest."""
    deduped = {}
    for row in rows:
        clean = safe_bar(row)
        if clean:
            deduped[bar_epoch(clean)] = clean
    return [deduped[key] for key in sorted(deduped)]


def parse_window(from_date, to_date, lookback_days):
    now = datetime.now(IST)
    try:
        end_date = (
            datetime.strptime(str(to_date or "").strip(), "%Y-%m-%d").date()
            if str(to_date or "").strip()
            else now.date()
        )
    except ValueError as exc:
        raise RuntimeError("To date must use YYYY-MM-DD.") from exc
    if str(from_date or "").strip():
        try:
            start_date = datetime.strptime(
                str(from_date).strip(), "%Y-%m-%d"
            ).date()
        except ValueError as exc:
            raise RuntimeError("From date must use YYYY-MM-DD.") from exc
    else:
        try:
            days = max(1, min(int(lookback_days), 240))
        except (TypeError, ValueError):
            days = 7
        start_date = end_date - timedelta(days=days - 1)
    if start_date > end_date:
        raise RuntimeError("From date cannot be after To date.")
    return start_date, end_date


def fetch_nifty(factor2, from_date="", to_date="", lookback_days="7"):
    missing = required_missing()
    if missing:
        raise RuntimeError(
            "Missing local configuration: " + ", ".join(missing)
        )
    factor = str(factor2 or "").strip()
    if not factor:
        raise RuntimeError(
            "Enter the current Shoonya TOTP generated by your authenticator."
        )

    start_date, end_date = parse_window(
        from_date, to_date, lookback_days
    )
    range_start = datetime.combine(start_date, dt_time.min, tzinfo=IST)
    range_end = datetime.combine(end_date, dt_time.max, tzinfo=IST)

    status, login = api_call("QuickAuth", build_login_payload(factor))
    if (
        status >= 400
        or not isinstance(login, dict)
        or str(login.get("stat", "")).lower() != "ok"
    ):
        message = login.get("emsg") if isinstance(login, dict) else None
        raise RuntimeError(
            "Shoonya login failed: "
            + str(message or "HTTP " + str(status))
        )

    session_key = str(login.get("susertoken") or "").strip()
    if not session_key:
        raise RuntimeError("Shoonya login returned no session token.")

    instrument = {
        "exchange": "NSE",
        "token": "26000",
        "tradingsymbol": "Nifty 50",
    }
    search_status, search = api_call(
        "SearchScrip",
        {
            "uid": cfg("SHOONYA_USER_ID"),
            "exch": "NSE",
            "stext": "Nifty 50",
        },
        session_key,
    )
    if search_status < 400 and isinstance(search, dict):
        for item in search.get("values", []):
            if (
                isinstance(item, dict)
                and str(item.get("tsym", "")).upper()
                in {"NIFTY 50", "NIFTY50"}
            ):
                instrument = {
                    "exchange": str(item.get("exch") or "NSE"),
                    "token": str(item.get("token") or "26000"),
                    "tradingsymbol": item.get("tsym") or "Nifty 50",
                }
                break

    quote_status, quote = api_call(
        "GetQuotes",
        {
            "uid": cfg("SHOONYA_USER_ID"),
            "exch": instrument["exchange"],
            "token": instrument["token"],
        },
        session_key,
    )
    if (
        quote_status >= 400
        or not isinstance(quote, dict)
        or str(quote.get("stat", "")).lower() != "ok"
    ):
        message = (
            quote.get("emsg")
            if isinstance(quote, dict)
            else "HTTP " + str(quote_status)
        )
        raise RuntimeError("Shoonya quote failed: " + str(message))

    all_rows = []
    chunk_count = 0
    chunk_start = range_start
    while chunk_start <= range_end:
        chunk_end = min(
            chunk_start + timedelta(days=7) - timedelta(seconds=1),
            range_end,
        )
        series_status, series = api_call(
            "TPSeries",
            {
                "uid": cfg("SHOONYA_USER_ID"),
                "exch": instrument["exchange"],
                "token": instrument["token"],
                "st": str(int(chunk_start.timestamp())),
                "et": str(int(chunk_end.timestamp())),
                "intrv": "5",
            },
            session_key,
        )
        if series_status >= 400 or not isinstance(series, list):
            message = (
                series.get("emsg")
                if isinstance(series, dict)
                else "unexpected response"
            )
            raise RuntimeError(
                "Shoonya 5-minute history failed for "
                + chunk_start.date().isoformat()
                + ": "
                + str(message)
            )
        all_rows.extend(series)
        chunk_count += 1
        chunk_start = chunk_end + timedelta(seconds=1)

    bars = normalize_bars(all_rows)
    return {
        "status": "ok",
        "relay_version": RELAY_VERSION,
        "read_only_probe": True,
        "orders_enabled": False,
        "instrument": instrument,
        "quote": safe_quote(quote),
        "historical_5m": {
            "from_date": start_date.isoformat(),
            "to_date": end_date.isoformat(),
            "lookback_days": (end_date - start_date).days + 1,
            "chunk_count": chunk_count,
            "bar_count": len(bars),
            "bars": bars,
        },
        "retrieved_at_ist": datetime.now(IST).isoformat(),
    }


def csv_bytes(bars):
    fields = [
        "time",
        "ssboe",
        "into",
        "inth",
        "intl",
        "intc",
        "intv",
        "intvwap",
        "v",
        "intoi",
        "oi",
    ]
    out = io.StringIO(newline="")
    writer = csv.DictWriter(
        out, fieldnames=fields, extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(bars)
    return out.getvalue().encode("utf-8")


def package_bytes(result):
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    csv_name = "NIFTY_5M_" + stamp + ".csv"
    json_name = "NIFTY_5M_" + stamp + "_metadata.json"
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr(
            csv_name, csv_bytes(result["historical_5m"]["bars"])
        )
        archive.writestr(
            json_name,
            json.dumps(
                {
                    "source": "Shoonya local relay",
                    "relay_version": RELAY_VERSION,
                    "instrument": result["instrument"],
                    "quote": result["quote"],
                    "historical_5m_summary": {
                        "from_date": result["historical_5m"][
                            "from_date"
                        ],
                        "to_date": result["historical_5m"]["to_date"],
                        "lookback_days": result["historical_5m"][
                            "lookback_days"
                        ],
                        "chunk_count": result["historical_5m"][
                            "chunk_count"
                        ],
                        "bar_count": result["historical_5m"][
                            "bar_count"
                        ],
                    },
                    "retrieved_at_ist": result["retrieved_at_ist"],
                    "read_only_probe": True,
                    "orders_enabled": False,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
    return "Shoonya_" + stamp + "_NIFTY_5M.zip", buf.getvalue()


def upload_to_render(package_name, package_data):
    render_url = cfg(
        "RENDER_COLLECTOR_URL", DEFAULT_RENDER_URL
    ).rstrip("/")
    password = cfg("COLLECTOR_PASSWORD")
    if not password:
        raise RuntimeError(
            "Add COLLECTOR_PASSWORD to .env.shoonya before running."
        )
    boundary = "----ShoonyaLocalRelayBoundary"
    parts = []

    def add_field(name, value):
        parts.append(
            (
                "--"
                + boundary
                + "\r\n"
                + 'Content-Disposition: form-data; name="'
                + name
                + '"\r\n\r\n'
                + str(value)
                + "\r\n"
            ).encode("utf-8")
        )

    add_field("collector_password", password)
    parts.append(
        (
            "--"
            + boundary
            + "\r\n"
            + 'Content-Disposition: form-data; name="package"; filename="'
            + package_name
            + '"\r\n'
            + "Content-Type: application/zip\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(package_data)
    parts.append(
        ("\r\n--" + boundary + "--\r\n").encode("utf-8")
    )
    request = urllib.request.Request(
        render_url + "/shoonya/relay-ingest",
        data=b"".join(parts),
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Cache-Control": "no-store",
            "User-Agent": f"ShoonyaLocalRelay/{RELAY_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=60
        ) as response:
            raw = response.read().decode("utf-8", "replace")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            "Render upload failed (HTTP "
            + str(exc.code)
            + "): "
            + body[:300]
        )


def page(message="", error="", result=None):
    missing = required_missing()
    result_text = (
        html.escape(json.dumps(result, indent=2, ensure_ascii=False))
        if result
        else ""
    )
    status = (
        '<div class="ok"><b>Local configuration:</b> ready. '
        + "Loaded "
        + html.escape(CONFIG_PATH.name)
        + ".</div>"
        if not missing
        else '<div class="warn"><b>Local configuration missing:</b> '
        + html.escape(", ".join(missing))
        + "<br>Fill .env.shoonya or env.shoonya using the example file.</div>"
    )
    result_block = (
        '<div class="section"><h2>Result</h2><pre>'
        + result_text
        + "</pre></div>"
        if result
        else ""
    )
    message_block = (
        '<div class="ok">' + html.escape(message) + "</div>"
        if message
        else ""
    )
    error_block = (
        '<div class="err"><b>Action failed:</b> '
        + html.escape(error)
        + "</div>"
        if error
        else ""
    )
    return (
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local Shoonya Relay V2</title>
<style>
body{font-family:Arial,sans-serif;max-width:900px;margin:40px auto;padding:0 18px;background:#f4f6f8;color:#172033}
.card{background:#fff;border-radius:16px;padding:28px;box-shadow:0 8px 28px rgba(0,0,0,.08)}
.badge{display:inline-block;background:#172033;color:#fff;padding:5px 9px;border-radius:999px;font-size:12px}
.muted{color:#5c6675;line-height:1.55}.ok,.err,.warn,.info{padding:13px 15px;border-radius:9px;margin:16px 0}
.ok{background:#eaf8ee}.err{background:#fdecec}.warn{background:#fff4d8}.info{background:#eef5ff}
.section{padding-top:18px;margin-top:18px;border-top:1px solid #e5e8ed}
label{font-weight:600}input{padding:10px;margin:6px 0 14px;width:280px;box-sizing:border-box}
button{padding:12px 18px;border:0;border-radius:9px;font-size:16px;cursor:pointer;background:#172033;color:#fff}
.small{font-size:13px;color:#687284;line-height:1.55}pre{white-space:pre-wrap;overflow:auto;background:#f4f6f8;padding:14px;border-radius:9px}
</style></head><body><div class="card">
<div class="badge">LOCAL READ-ONLY RELAY v"""
        + RELAY_VERSION
        + """</div><h1>Shoonya NIFTY data collector</h1>
<p class="muted">Shoonya login runs from this computer's internet connection. Only sanitized NIFTY 50 five-minute data is uploaded to Render. No order API is available.</p>
"""
        + status
        + message_block
        + error_block
        + """
<div class="info"><b>TOTP:</b> Open your Shoonya authenticator or Chrome extension and enter the current 6-digit code. This page does not send an SMS.</div>
<div class="section"><form method="post" action="/run" autocomplete="off">
<label for="factor2">Shoonya TOTP</label><br><input id="factor2" name="factor2" type="password" inputmode="numeric" autocomplete="one-time-code" maxlength="12" required><br>
<label for="from_date">From date (YYYY-MM-DD)</label><br><input id="from_date" name="from_date" type="text" value="2026-02-25" placeholder="YYYY-MM-DD"><br>
<label for="to_date">To date (YYYY-MM-DD)</label><br><input id="to_date" name="to_date" type="text" value="2026-05-18" placeholder="YYYY-MM-DD"><br>
<label for="lookback_days">Fallback lookback days (1-240)</label><br><input id="lookback_days" name="lookback_days" type="number" value="7" min="1" max="240"><br>
<button type="submit">Fetch and upload read-only data</button>
</form></div>
<div class="section small">Static credentials stay in the local .env.shoonya or env.shoonya file. The TOTP is used only for this request and is not saved.</div>
"""
        + result_block
        + "</div></body></html>"
    )


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format_string, *args):
        print(
            "%s - %s"
            % (self.address_string(), format_string % args)
        )

    def send_html(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self.send_html(page())
        elif path == "/health":
            self.send_json(
                {
                    "status": "ready",
                    "relay_version": RELAY_VERSION,
                    "configured": not required_missing(),
                    "config_file": CONFIG_PATH.name,
                    "read_only_probe": True,
                    "orders_enabled": False,
                }
            )
        else:
            self.send_error(404)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/run":
            self.send_error(404)
            return
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(403)
            return
        try:
            content_length = int(
                self.headers.get("Content-Length", "0")
            )
            raw_body = self.rfile.read(content_length).decode(
                "utf-8", "replace"
            )
            form = urllib.parse.parse_qs(
                raw_body, keep_blank_values=True
            )
            factor2 = form.get("factor2", [""])[0]
            from_date = form.get(
                "from_date", [cfg("DATA_FROM_DATE")]
            )[0]
            to_date = form.get(
                "to_date", [cfg("DATA_TO_DATE")]
            )[0]
            lookback_days = form.get(
                "lookback_days", ["7"]
            )[0]
            result = fetch_nifty(
                factor2, from_date, to_date, lookback_days
            )
            package_name, package_data = package_bytes(result)
            uploaded = upload_to_render(
                package_name, package_data
            )
            message = (
                "Fetched "
                + str(result["historical_5m"]["bar_count"])
                + " NIFTY 50 five-minute bars and uploaded the sanitized package."
            )
            if (
                isinstance(uploaded, dict)
                and uploaded.get("drive_file_url")
            ):
                message += (
                    " Drive package: " + uploaded["drive_file_url"]
                )
            self.send_html(
                page(
                    message=message,
                    result={
                        "status": result["status"],
                        "relay_version": RELAY_VERSION,
                        "instrument": result["instrument"],
                        "historical_5m": {
                            "from_date": result["historical_5m"][
                                "from_date"
                            ],
                            "to_date": result["historical_5m"][
                                "to_date"
                            ],
                            "lookback_days": result["historical_5m"][
                                "lookback_days"
                            ],
                            "chunk_count": result["historical_5m"][
                                "chunk_count"
                            ],
                            "bar_count": result["historical_5m"][
                                "bar_count"
                            ],
                        },
                        "upload": uploaded,
                        "read_only_probe": True,
                        "orders_enabled": False,
                    },
                )
            )
        except Exception as exc:
            self.send_html(page(error=str(exc)), status=400)


if __name__ == "__main__":
    print(
        "Starting Shoonya local read-only relay v"
        + RELAY_VERSION
        + " at "
        + LOCAL_URL
    )
    print("Static configuration file: " + str(CONFIG_PATH))
    webbrowser.open(LOCAL_URL)
    try:
        server = http.server.ThreadingHTTPServer(
            (LOCAL_HOST, LOCAL_PORT), Handler
        )
    except OSError as exc:
        raise SystemExit(
            "Port "
            + str(LOCAL_PORT)
            + " is already in use. Close the old V2 relay window and run the launcher again."
        ) from exc
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
