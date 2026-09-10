"""V6-only all-options observation layer.

This module does NOT modify the frozen V5 reconstruction engine. It augments the
V6 publication package so a daily AlgoStra bundle records every detected option
strategy (stock + index), including activity-only/no-trade strategies.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_CAPTURE_LOCK = threading.Lock()
INDEX_HINTS = ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "IDX ")


def _strategy_bucket(name: str) -> str:
    text = str(name or "").upper()
    return "index_options" if any(hint in text for hint in INDEX_HINTS) else "stock_options"


def _safe_iso(dt):
    return dt.isoformat() if dt else ""


def build_manifest(legacy, input_bytes: bytes, input_filename: str) -> dict:
    zf, csv_names = legacy.safe_zip_input(input_bytes)
    strategies = {}
    files = []

    try:
        for name in csv_names:
            raw = zf.read(name)
            text = raw.decode("utf-8-sig", errors="replace")
            pos = legacy.parse_positions_csv(text, name)
            if pos is not None:
                strategy_name = pos.get("strategy_name") or Path(name).stem
                key = legacy.normalized_strategy_name(strategy_name) or strategy_name
                item = strategies.setdefault(key, {
                    "strategy_name": strategy_name,
                    "scope": _strategy_bucket(strategy_name),
                    "position_trade_count": 0,
                    "activity_event_count": 0,
                    "entry_event_count": 0,
                    "exit_event_count": 0,
                    "first_event_ist": "",
                    "last_event_ist": "",
                    "underlyings": set(),
                    "option_sides": set(),
                    "source_files": set(),
                })
                trades = pos.get("trades", [])
                item["position_trade_count"] += len(trades)
                item["source_files"].add(name)
                for trade in trades:
                    if trade.get("underlying"):
                        item["underlyings"].add(str(trade["underlying"]).upper())
                    if trade.get("optiontype"):
                        item["option_sides"].add(str(trade["optiontype"]).upper())
                    if str(trade.get("underlying", "")).upper() in INDEX_HINTS:
                        item["scope"] = "index_options"
                files.append({"filename": name, "detected_type": "positions", "strategy_name": strategy_name, "trade_rows": len(trades)})
                continue

            activity = legacy.parse_activity_log(text, name)
            if activity is not None:
                strategy_name = legacy.strategy_from_activity_filename(name)
                key = legacy.normalized_strategy_name(strategy_name) or strategy_name
                item = strategies.setdefault(key, {
                    "strategy_name": strategy_name,
                    "scope": _strategy_bucket(strategy_name),
                    "position_trade_count": 0,
                    "activity_event_count": 0,
                    "entry_event_count": 0,
                    "exit_event_count": 0,
                    "first_event_ist": "",
                    "last_event_ist": "",
                    "underlyings": set(),
                    "option_sides": set(),
                    "source_files": set(),
                })
                item["source_files"].add(name)
                item["activity_event_count"] += len(activity)
                dated = [event.get("datetime") for event in activity if event.get("datetime")]
                if dated:
                    first_dt, last_dt = min(dated), max(dated)
                    if not item["first_event_ist"] or first_dt.isoformat() < item["first_event_ist"]:
                        item["first_event_ist"] = first_dt.isoformat()
                    if not item["last_event_ist"] or last_dt.isoformat() > item["last_event_ist"]:
                        item["last_event_ist"] = last_dt.isoformat()
                for event in activity:
                    cat = str(event.get("category", "")).upper()
                    if cat in {"ENTRY", "ENTRY1", "ENTRY2"}:
                        item["entry_event_count"] += 1
                    if cat in {"EXIT", "SQROFF", "TIMESQROFF"}:
                        item["exit_event_count"] += 1
                files.append({"filename": name, "detected_type": "activity_log", "strategy_name": strategy_name, "trade_rows": ""})
                continue

            files.append({"filename": name, "detected_type": "unrecognised_csv", "strategy_name": "", "trade_rows": ""})
    finally:
        zf.close()

    rows = []
    for item in strategies.values():
        row = dict(item)
        row["underlyings"] = sorted(item["underlyings"])
        row["option_sides"] = sorted(item["option_sides"])
        row["source_files"] = sorted(item["source_files"])
        row["observation_state"] = "executed_trades" if item["position_trade_count"] else "no_position_trade_detected"
        rows.append(row)
    rows.sort(key=lambda r: (r["scope"], r["strategy_name"]))

    now = datetime.now(legacy.IST)
    return {
        "schema_version": "all-options-observation-v1",
        "collection_scope": "ALL_OPTIONS_STOCK_AND_NIFTY",
        "collection_date_ist": now.date().isoformat(),
        "generated_at_ist": now.isoformat(),
        "source_input": input_filename,
        "strategy_count": len(rows),
        "stock_option_strategy_count": sum(1 for r in rows if r["scope"] == "stock_options"),
        "index_option_strategy_count": sum(1 for r in rows if r["scope"] == "index_options"),
        "executed_trade_count": sum(int(r["position_trade_count"]) for r in rows),
        "no_trade_strategy_count": sum(1 for r in rows if not r["position_trade_count"]),
        "strategies": rows,
        "files": files,
        "note": "V6 observation layer; frozen V5 reconstruction remains unchanged. No-trade strategies are retained as observations, not treated as missing data.",
    }


def _summary_csv(manifest: dict) -> str:
    fields = [
        "strategy_name", "scope", "observation_state", "position_trade_count",
        "activity_event_count", "entry_event_count", "exit_event_count",
        "first_event_ist", "last_event_ist", "underlyings", "option_sides", "source_files",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for source in manifest.get("strategies", []):
        row = {key: source.get(key, "") for key in fields}
        for key in ("underlyings", "option_sides", "source_files"):
            if isinstance(row.get(key), list):
                row[key] = "|".join(str(v) for v in row[key])
        writer.writerow(row)
    return buf.getvalue()


def append_manifest_to_zip(output_path: str, manifest: dict) -> None:
    path = Path(output_path)
    if not path.exists():
        return
    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("all_options/ALL_OPTIONS_OBSERVATION.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr("all_options/STRATEGY_OBSERVATION.csv", _summary_csv(manifest))


def build_observation_only_zip(legacy, input_bytes: bytes, input_filename: str, manifest: dict, job_id: str) -> str:
    out = legacy.TMP_DIR / f"All_Options_Observation_V6_{manifest['collection_date_ist']}_{job_id[:8]}.zip"
    source_zf, csv_names = legacy.safe_zip_input(input_bytes)
    try:
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in csv_names:
                zf.writestr(f"source_algostra/{Path(name).name}", source_zf.read(name))
            zf.writestr("all_options/ALL_OPTIONS_OBSERVATION.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            zf.writestr("all_options/STRATEGY_OBSERVATION.csv", _summary_csv(manifest))
    finally:
        source_zf.close()
    return str(out)


def wrap_v6_job_worker(production, original_worker):
    def wrapped(job_id, session_key, input_bytes, input_filename, before_minutes, after_minutes, include_chain, source_meta=None):
        with _CAPTURE_LOCK:
            manifest = build_manifest(production.legacy, input_bytes, input_filename)
            original_publish = production.publish_package

            def publish_with_all_options(output_path, evidence_folder_id, status_folder_id=None, source_meta=None, job_summary=None):
                append_manifest_to_zip(output_path, manifest)
                meta = dict(source_meta or {})
                meta.update({
                    "collection_scope": manifest["collection_scope"],
                    "collection_date_ist": manifest["collection_date_ist"],
                    "all_options_observation": True,
                })
                summary = dict(job_summary or {})
                summary.update({
                    "all_options_observation": True,
                    "all_options_strategy_count": manifest["strategy_count"],
                    "all_options_stock_strategy_count": manifest["stock_option_strategy_count"],
                    "all_options_index_strategy_count": manifest["index_option_strategy_count"],
                    "all_options_executed_trade_count": manifest["executed_trade_count"],
                    "all_options_no_trade_strategy_count": manifest["no_trade_strategy_count"],
                })
                return original_publish(output_path, evidence_folder_id, status_folder_id, source_meta=meta, job_summary=summary)

            production.publish_package = publish_with_all_options
            try:
                original_worker(
                    job_id=job_id,
                    session_key=session_key,
                    input_bytes=input_bytes,
                    input_filename=input_filename,
                    before_minutes=before_minutes,
                    after_minutes=after_minutes,
                    include_chain=include_chain,
                    source_meta=source_meta,
                )
            finally:
                production.publish_package = original_publish

            with production.legacy.JOB_LOCK:
                snapshot = dict(production.legacy.JOBS.get(job_id, {}))

            # If the frozen V5 engine has no executed positions to reconstruct,
            # preserve the day as an observation-only package rather than losing it.
            message = str(snapshot.get("message", ""))
            if snapshot.get("status") == "error" and "No AlgoStra Positions trades were detected" in message:
                output_path = build_observation_only_zip(production.legacy, input_bytes, input_filename, manifest, job_id)
                uploaded, publication_manifest = original_publish(
                    output_path,
                    production.EVIDENCE_FOLDER_ID,
                    production.STATUS_FOLDER_ID or None,
                    source_meta={
                        **dict(source_meta or {}),
                        "collection_scope": manifest["collection_scope"],
                        "collection_date_ist": manifest["collection_date_ist"],
                        "observation_only": True,
                    },
                    job_summary={
                        "pipeline_version": "6.3+all-options-observation-v1",
                        "reconstruction_engine": "V5 frozen (not invoked: zero executed positions)",
                        "all_options_strategy_count": manifest["strategy_count"],
                        "all_options_no_trade_strategy_count": manifest["no_trade_strategy_count"],
                        "observation_only": True,
                    },
                )
                production.legacy.set_job(
                    job_id,
                    status="done",
                    progress=100,
                    message="Completed: all-options observation package published; no executed AlgoStra positions were present to reconstruct.",
                    output_path=output_path,
                    output_filename=Path(output_path).name,
                    summary={
                        "pipeline_version": "6.3+all-options-observation-v1",
                        "ready_for_trading_brain": True,
                        "observation_only": True,
                        "all_options_strategy_count": manifest["strategy_count"],
                        "all_options_stock_strategy_count": manifest["stock_option_strategy_count"],
                        "all_options_index_strategy_count": manifest["index_option_strategy_count"],
                        "all_options_no_trade_strategy_count": manifest["no_trade_strategy_count"],
                        "drive_file_id": uploaded.get("id", ""),
                        "drive_file_name": uploaded.get("name", ""),
                        "drive_file_url": uploaded.get("webViewLink", ""),
                        "manifest_status": publication_manifest.get("status", ""),
                    },
                )

    return wrapped
