import csv
import io
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path


def _read_csv(zf, name):
    return list(csv.DictReader(io.StringIO(zf.read(name).decode("utf-8-sig", errors="replace"))))


def _csv_bytes(rows, fieldnames):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _strategy_from_events(rows):
    candidates = []
    for row in rows:
        message = str(row.get("message") or "").strip()
        category = str(row.get("category") or "").strip()

        m = re.search(r"\bYour\s+(.+?)\s+strategy\s+(?:has|is)\b", message, re.I)
        if m:
            candidates.append(m.group(1).strip())

        if category.upper().startswith("RECEIVED") and ":" in message:
            candidate = message.split(":", 1)[0].strip()
            if candidate:
                candidates.append(candidate)

    if not candidates:
        return ""
    return Counter(candidates).most_common(1)[0][0]


def repair_activity_log_mapping(zip_path):
    """Repair V5's filename-derived Activity Log mapping inside a V6 evidence ZIP.

    AlgoStra exports generic names such as ``rebalance_logs (8).csv``. The actual
    strategy name is embedded inside each log (RECEIVED / termination messages),
    so V6.3 maps from parsed event content rather than from the filename.
    Returns a small audit dict. Safe to call repeatedly.
    """
    path = Path(zip_path)
    if not path.exists():
        raise FileNotFoundError(path)

    events_name = "analysis/activity_log_events.csv"
    summary_name = "analysis/activity_log_summary.csv"
    comparison_name = "analysis/strategy_comparison.csv"
    input_report_name = "analysis/input_file_report.csv"

    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        required = {events_name, comparison_name}
        if not required.issubset(names):
            return {"status": "skipped", "reason": "activity analysis files absent"}

        events = _read_csv(zf, events_name)
        comparison = _read_csv(zf, comparison_name)
        input_report = _read_csv(zf, input_report_name) if input_report_name in names else []

        by_file = defaultdict(list)
        for row in events:
            by_file[str(row.get("source_filename") or "")].append(row)

        file_to_strategy = {}
        for filename, rows in by_file.items():
            strategy = _strategy_from_events(rows)
            if strategy:
                file_to_strategy[filename] = strategy

        known_strategies = [str(r.get("strategy_name") or "").strip() for r in comparison]
        known_set = {s for s in known_strategies if s}
        file_to_strategy = {f: s for f, s in file_to_strategy.items() if s in known_set}

        strategy_events = defaultdict(list)
        for row in events:
            strategy = file_to_strategy.get(str(row.get("source_filename") or ""), "")
            if strategy:
                strategy_events[strategy].append(row)

        summary_rows = []
        for strategy in known_strategies:
            rows = sorted(strategy_events.get(strategy, []), key=lambda r: str(r.get("datetime_ist") or ""))
            cats = [str(r.get("category") or "").upper() for r in rows]
            msgs = [str(r.get("message") or "") for r in rows]
            entry_events = sum(1 for c in cats if re.fullmatch(r"ENTRY\d+", c))
            exit_events = sum(1 for c in cats if c == "EXIT")
            sqroff_events = sum(1 for c in cats if c == "SQROFF")
            time_sqroff_events = sum(1 for c, m in zip(cats, msgs) if "TIME" in c and "SQ" in c or "TIME SQUARE" in m.upper())
            stopped_events = sum(1 for c in cats if c == "STOPPED")
            max_loss = any("MAXIMUM LOSS LIMIT" in m.upper() or "STOPLOSS HIT AT PNL" in m.upper() for m in msgs)
            summary_rows.append({
                "strategy_name": strategy,
                "activity_file_found": bool(rows),
                "activity_event_count": len(rows),
                "entry_events": entry_events,
                "exit_events": exit_events,
                "sqroff_events": sqroff_events,
                "time_sqroff_events": time_sqroff_events,
                "stopped_events": stopped_events,
                "max_loss_triggered": max_loss,
                "square_off_seen": sqroff_events > 0,
                "stopped_seen": stopped_events > 0,
                "first_event_ist": rows[0].get("datetime_ist", "") if rows else "",
                "last_event_ist": rows[-1].get("datetime_ist", "") if rows else "",
            })

        summary_by_strategy = {r["strategy_name"]: r for r in summary_rows}
        for row in comparison:
            s = summary_by_strategy.get(str(row.get("strategy_name") or ""), {})
            row["activity_log_found"] = s.get("activity_file_found", False)
            row["max_loss_triggered"] = s.get("max_loss_triggered", False)
            row["square_off_seen"] = s.get("square_off_seen", False)
            row["activity_last_event_ist"] = s.get("last_event_ist", "")

        for row in input_report:
            if str(row.get("detected_type") or "") == "activity_log":
                mapped = file_to_strategy.get(str(row.get("filename") or ""), "")
                if mapped:
                    row["strategy_name"] = mapped

        replacements = {
            summary_name: _csv_bytes(summary_rows, [
                "strategy_name", "activity_file_found", "activity_event_count", "entry_events",
                "exit_events", "sqroff_events", "time_sqroff_events", "stopped_events",
                "max_loss_triggered", "square_off_seen", "stopped_seen", "first_event_ist", "last_event_ist",
            ]),
            comparison_name: _csv_bytes(comparison, list(comparison[0].keys()) if comparison else []),
        }
        if input_report:
            replacements[input_report_name] = _csv_bytes(input_report, list(input_report[0].keys()))

        fd, tmp_name = tempfile.mkstemp(prefix=path.stem + "_repair_", suffix=".zip", dir=str(path.parent))
        Path(tmp_name).unlink(missing_ok=True)
        try:
            with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_DEFLATED) as out:
                for info in zf.infolist():
                    if info.filename in replacements:
                        out.writestr(info, replacements[info.filename])
                    else:
                        out.writestr(info, zf.read(info.filename))
            Path(tmp_name).replace(path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    return {
        "status": "repaired",
        "mapped_activity_files": len(file_to_strategy),
        "strategies_with_activity": sum(1 for r in summary_rows if r["activity_file_found"]),
        "total_activity_events": len(events),
    }
