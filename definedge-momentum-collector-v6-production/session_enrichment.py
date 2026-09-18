import csv
import io
import json
import zipfile
from collections import defaultdict
from datetime import datetime, time as dt_time
from pathlib import Path


def _session_bounds(entry_dt):
    tz = entry_dt.tzinfo
    day = entry_dt.date()
    return (
        datetime.combine(day, dt_time(9, 15), tzinfo=tz),
        datetime.combine(day, dt_time(15, 30), tzinfo=tz),
    )


def _parse_minute_rows(raw, ist):
    rows = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        try:
            cols = next(csv.reader([line]))
        except Exception:
            continue
        cols = (cols + [""] * 7)[:7]
        stamp = str(cols[0]).strip()
        parsed = None
        for fmt in (
            "%d/%m/%Y %H:%M:%S",
            "%d-%m-%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d-%m-%Y %H:%M",
            "%Y-%m-%d %H:%M",
        ):
            try:
                parsed = datetime.strptime(stamp, fmt).replace(tzinfo=ist)
                break
            except ValueError:
                pass
        if parsed is None:
            continue

        def num(value):
            try:
                return float(value)
            except Exception:
                return None

        rows.append(
            {
                "dt": parsed,
                "open": num(cols[1]),
                "high": num(cols[2]),
                "low": num(cols[3]),
                "close": num(cols[4]),
                "volume": num(cols[5]),
                "oi": num(cols[6]),
            }
        )
    rows.sort(key=lambda row: row["dt"])
    return rows


def _nearest(rows, target):
    if not rows or target is None:
        return None
    return min(rows, key=lambda row: abs((row["dt"] - target).total_seconds()))


def _pct_change(start, end):
    if start in (None, 0) or end is None:
        return None
    return round((end / start - 1.0) * 100.0, 4)


def _abs_change(start, end):
    if start is None or end is None:
        return None
    return round(end - start, 4)


def _flow_label(price_change, oi_change):
    if price_change is None or oi_change is None:
        return "UNAVAILABLE"
    if price_change > 0 and oi_change > 0:
        return "LONG_BUILDUP"
    if price_change > 0 and oi_change < 0:
        return "SHORT_COVERING"
    if price_change < 0 and oi_change > 0:
        return "SHORT_BUILDUP"
    if price_change < 0 and oi_change < 0:
        return "LONG_UNWINDING"
    return "FLAT_OR_MIXED"


def _find_future(contract, nfo, trade_date):
    symbol = str(contract.get("symbol") or "").upper()
    expiry = str(contract.get("expiry") or "")
    candidates = [
        row
        for row in nfo
        if str(row.get("symbol") or "").upper() == symbol
        and str(row.get("instrument_type") or "").upper().startswith("FUT")
    ]
    same_expiry = [row for row in candidates if str(row.get("expiry") or "") == expiry]
    if same_expiry:
        return same_expiry[0]
    # Fallback: prefer the first future whose expiry text matches the option's month/year
    # through the master ordering. If no safe match exists, leave it unavailable.
    return candidates[0] if len(candidates) == 1 else None


def _resolve_nifty(nse):
    exact = [
        row
        for row in nse
        if str(row.get("tradingsymbol") or "").upper() in {"NIFTY 50", "NIFTY50"}
    ]
    if exact:
        return exact[0]
    return {
        "segment": "NSE",
        "token": "26000",
        "tradingsymbol": "Nifty 50",
    }


def _read_metrics(zf):
    try:
        raw = zf.read("analysis/trade_metrics.csv").decode("utf-8-sig", "replace")
    except KeyError:
        return {}
    rows = {}
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            key = int(float(row.get("trade_number") or 0))
        except Exception:
            continue
        rows[key] = row
    return rows


def _csv_text(rows):
    if not rows:
        return ""
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def enrich_package(output_path, session_key, input_bytes, legacy, job_id):
    """Append full-session 1-minute evidence and OI/price attribution.

    The frozen V5 engine remains untouched. This enrichment adds:
    - 09:15-15:30 1-minute data for traded options and cash underlyings
    - matching stock/index futures 1-minute data where resolvable
    - NIFTY 50 1-minute benchmark data
    - OI attribution at entry, option-MFE time and exit

    No order APIs are used.
    """
    parsed = legacy.parse_batch_zip(input_bytes)
    trades = parsed["positions"]
    nfo = legacy.parse_master(legacy.download_master(legacy.NFO_MASTER_URL))
    nse = legacy.parse_master(legacy.download_master(legacy.NSE_MASTER_URL))
    nfo_by_ts = {row["tradingsymbol"].upper(): row for row in nfo}

    resolved = []
    for index, trade in enumerate(trades, start=1):
        contract = legacy.resolve_option(trade["tradingsymbol"], nfo_by_ts)
        if not contract:
            continue
        underlying = legacy.resolve_underlying(contract["symbol"], nse)
        future = _find_future(contract, nfo, trade["entry_time"].date())
        item = dict(trade)
        item.update(
            {
                "trade_number": index,
                "contract": contract,
                "underlying_record": underlying,
                "future_record": future,
            }
        )
        resolved.append(item)

    plan = {}
    refs = {}
    benchmark = _resolve_nifty(nse)

    def add(record, date_key, start_dt, end_dt, role):
        if not record:
            return None
        key = (str(record["token"]), date_key, role)
        legacy.group_window_add(
            plan,
            key,
            start_dt,
            end_dt,
            {
                "segment": record["segment"],
                "token": record["token"],
                "tradingsymbol": record["tradingsymbol"],
                "timeframe": "minute",
                "role": role,
            },
        )
        return key

    for trade in resolved:
        entry_dt = trade["entry_time"]
        start_dt, end_dt = _session_bounds(entry_dt)
        date_key = entry_dt.date().isoformat()
        refs[trade["trade_number"]] = {
            "option": add(trade["contract"], date_key, start_dt, end_dt, "option"),
            "underlying": add(trade["underlying_record"], date_key, start_dt, end_dt, "underlying"),
            "future": add(trade["future_record"], date_key, start_dt, end_dt, "future"),
            "benchmark": add(benchmark, date_key, start_dt, end_dt, "benchmark"),
        }

    raw_map, errors = legacy.execute_history_plan(
        session_key,
        plan,
        job_id,
        97,
        99,
        "V6.4 full-session 1-minute enrichment",
    )

    path = Path(output_path)
    temp_path = path.with_name(path.stem + "_enriched.zip")
    attribution = []

    with zipfile.ZipFile(path, "r") as source:
        metrics = _read_metrics(source)
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))

            for key, details in plan.items():
                raw = raw_map.get(key)
                if raw is None:
                    continue
                token, date_key, role = key
                symbol = legacy.safe_name(details["metadata"]["tradingsymbol"])
                target.writestr(
                    f"market_data/session_1m/{role}/{symbol}/minute_{date_key}.csv",
                    legacy.minute_to_csv(raw),
                )

            for trade in resolved:
                number = trade["trade_number"]
                metric = metrics.get(number, {})
                entry_dt = trade["entry_time"]
                exit_dt = trade.get("exit_time")
                mfe_dt = legacy.parse_full_timestamp(metric.get("max_option_ltp_time_ist"))
                if mfe_dt is None:
                    try:
                        mfe_dt = datetime.fromisoformat(metric.get("max_option_ltp_time_ist") or "")
                    except Exception:
                        mfe_dt = None

                row = {
                    "trade_number": number,
                    "strategy_name": trade.get("strategy_name", ""),
                    "underlying": trade.get("underlying", ""),
                    "tradingsymbol": trade["contract"].get("tradingsymbol", ""),
                    "optiontype": trade["contract"].get("optiontype", ""),
                    "strike": trade["contract"].get("strike", ""),
                    "entry_time_ist": entry_dt.isoformat(),
                    "mfe_time_ist": mfe_dt.isoformat() if mfe_dt else "",
                    "exit_time_ist": exit_dt.isoformat() if exit_dt else "",
                    "future_tradingsymbol": (
                        trade["future_record"].get("tradingsymbol", "")
                        if trade.get("future_record")
                        else ""
                    ),
                }

                for role in ("option", "future"):
                    key = refs[number].get(role)
                    rows = _parse_minute_rows(raw_map.get(key, ""), legacy.IST) if key else []
                    at_entry = _nearest(rows, entry_dt)
                    at_mfe = _nearest(rows, mfe_dt)
                    at_exit = _nearest(rows, exit_dt)
                    prefix = role
                    for label, point in (("entry", at_entry), ("mfe", at_mfe), ("exit", at_exit)):
                        row[f"{prefix}_{label}_price"] = point.get("close") if point else ""
                        row[f"{prefix}_{label}_oi"] = point.get("oi") if point else ""

                    if at_entry and at_mfe:
                        price_chg = _pct_change(at_entry.get("close"), at_mfe.get("close"))
                        oi_chg = _abs_change(at_entry.get("oi"), at_mfe.get("oi"))
                        oi_pct = _pct_change(at_entry.get("oi"), at_mfe.get("oi"))
                        row[f"{prefix}_entry_to_mfe_price_pct"] = price_chg if price_chg is not None else ""
                        row[f"{prefix}_entry_to_mfe_oi_change"] = oi_chg if oi_chg is not None else ""
                        row[f"{prefix}_entry_to_mfe_oi_pct"] = oi_pct if oi_pct is not None else ""
                        if role == "future":
                            row["future_entry_to_mfe_flow"] = _flow_label(price_chg, oi_chg)

                    if at_entry and at_exit:
                        price_chg = _pct_change(at_entry.get("close"), at_exit.get("close"))
                        oi_chg = _abs_change(at_entry.get("oi"), at_exit.get("oi"))
                        oi_pct = _pct_change(at_entry.get("oi"), at_exit.get("oi"))
                        row[f"{prefix}_entry_to_exit_price_pct"] = price_chg if price_chg is not None else ""
                        row[f"{prefix}_entry_to_exit_oi_change"] = oi_chg if oi_chg is not None else ""
                        row[f"{prefix}_entry_to_exit_oi_pct"] = oi_pct if oi_pct is not None else ""
                        if role == "future":
                            row["future_entry_to_exit_flow"] = _flow_label(price_chg, oi_chg)

                attribution.append(row)

            target.writestr("analysis/oi_momentum_attribution.csv", _csv_text(attribution))
            target.writestr(
                "manifest/session_enrichment.json",
                json.dumps(
                    {
                        "version": "6.4",
                        "session_window_ist": "09:15-15:30",
                        "minute_streams_requested": len(plan),
                        "minute_streams_downloaded": len(raw_map),
                        "errors": {str(key): value for key, value in errors.items()},
                        "notes": [
                            "Futures price/OI labels use the standard price-vs-OI taxonomy.",
                            "Option OI is recorded but is not mechanically labelled short covering because option OI is two-sided.",
                            "Historical Definedge minute data supplies OI for derivatives; Greeks/IV are not raw historical fields in this endpoint.",
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

    temp_path.replace(path)
    return {
        "session_enrichment_version": "6.4",
        "session_minute_streams_requested": len(plan),
        "session_minute_streams_downloaded": len(raw_map),
        "session_enrichment_errors": len(errors),
        "oi_attribution_rows": len(attribution),
    }
