"""V6 research-only momentum/pattern correlation pack.

This module does not alter the frozen V5 reconstruction engine or trading rules.
It adds machine-readable research artifacts to each V6 evidence ZIP so later
analysis can correlate actual/missed momentum with documented Definedge P&F
ecosystem conditions and existing MFE/MAE/giveback metrics.

Pattern entries are hypotheses/conditions to test, not claims that a condition
occurred. A pattern is only marked observed when market data and a documented or
platform-native definition support it.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path


PATTERN_REGISTRY = [
    {"id": "PF_DTB", "label": "Double Top Buy", "role": "confirmed_bullish_breakout_candidate", "family": "point_and_figure"},
    {"id": "PF_DBS", "label": "Double Bottom Sell", "role": "confirmed_bearish_breakout_candidate", "family": "point_and_figure"},
    {"id": "PF_PRE_DTB", "label": "Pre Double Top Buy", "role": "platform_pretrigger_candidate", "family": "point_and_figure", "detection_policy": "platform_event_until_formula_verified"},
    {"id": "PF_PRE_DBS", "label": "Pre Double Bottom Sell", "role": "platform_pretrigger_candidate", "family": "point_and_figure", "detection_policy": "platform_event_until_formula_verified"},
    {"id": "PF_AP_BULL", "label": "Affordable probable breakout - Bullish", "role": "probable_bullish_setup", "family": "point_and_figure"},
    {"id": "PF_AP_BEAR", "label": "Affordable probable breakout - Bearish", "role": "probable_bearish_setup", "family": "point_and_figure"},
    {"id": "DSMART_ABOVE_6", "label": "Above D-Smart (6-period)", "role": "bullish_state_candidate", "family": "d_smart"},
    {"id": "DSMART_BELOW_6", "label": "Below D-Smart (6-period)", "role": "bearish_state_candidate", "family": "d_smart"},
    {"id": "DSMART_CLOUD_ABOVE_6", "label": "Price Above D Smart Line Cloud (6)", "role": "bullish_cloud_state_candidate", "family": "d_smart"},
    {"id": "DSMART_CLOUD_BELOW_6", "label": "Price Below D Smart Line Cloud (6)", "role": "bearish_cloud_state_candidate", "family": "d_smart"},
    {"id": "DSMART_PULLBACK", "label": "D-Smart Pullback", "role": "trend_pullback_candidate", "family": "d_smart", "detection_policy": "formula_must_be_verified"},
    {"id": "DSMART_STAR", "label": "D-Smart Star / exhaustion", "role": "exhaustion_candidate", "family": "d_smart", "detection_policy": "formula_must_be_verified"},
    {"id": "MAST_BULL", "label": "Price above MAST", "role": "bullish_state_candidate", "family": "mast", "detection_policy": "use_actual_period_multiplier_if_known"},
    {"id": "MAST_BEAR", "label": "Price below MAST", "role": "bearish_state_candidate", "family": "mast", "detection_policy": "use_actual_period_multiplier_if_known"},
    {"id": "DTDB_BULL", "label": "DTDB bullish momentum state", "role": "bullish_momentum_context", "family": "dtdb", "detection_policy": "formula_must_be_verified"},
    {"id": "DTDB_BEAR", "label": "DTDB bearish momentum state", "role": "bearish_momentum_context", "family": "dtdb", "detection_policy": "formula_must_be_verified"},
    {"id": "CAM_H3", "label": "Price above Camarilla H3", "role": "strategy_threshold_context", "family": "camarilla"},
    {"id": "CAM_L3", "label": "Price below Camarilla L3", "role": "strategy_threshold_context", "family": "camarilla"},
]


def _read_csv(text: str):
    try:
        return list(csv.DictReader(io.StringIO(text)))
    except Exception:
        return []


def _find_metric_rows(zf: zipfile.ZipFile):
    """Return only true trade-level metric rows, never strategy aggregates.

    Retrospective test on the 09-Sep V6 package showed the old substring-based
    header check also admitted strategy_comparison.csv because
    avg_peak_to_exit_giveback_pct contains peak_to_exit_giveback_pct.  Require
    trade identity columns plus exact metric columns instead.
    """
    rows = []
    sources = []
    required_identity = {"trade_number", "strategy_name", "underlying", "tradingsymbol"}
    exact_metric_fields = {
        "mfe_pct_from_entry_basis",
        "mae_pct_from_entry_basis",
        "peak_to_exit_giveback_pct",
    }

    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        try:
            text = zf.read(name).decode("utf-8-sig", errors="replace")
        except Exception:
            continue
        first = text.splitlines()[0] if text.splitlines() else ""
        if not first:
            continue
        try:
            header = {str(x).strip() for x in next(csv.reader([first]))}
        except Exception:
            continue
        if not required_identity.issubset(header):
            continue
        if not (header & exact_metric_fields):
            continue
        found = _read_csv(text)
        found = [r for r in found if str(r.get("trade_number", "")).strip() and str(r.get("tradingsymbol", "")).strip()]
        if found:
            rows.extend(found)
            sources.append(name)

    return rows, sources


def _chart_sources(zf: zipfile.ZipFile):
    """Inventory downloaded market-data files for overlay/replay; no inference."""
    result = []
    for name in zf.namelist():
        low = name.lower()
        if low.endswith(".csv") and any(token in low for token in ("tick", "minute", "option", "underlying", "chain", "history")):
            result.append(name)
    return sorted(set(result))


def _metric_summary(row: dict):
    keys = (
        "trade_number", "strategy_name", "underlying", "tradingsymbol", "optiontype",
        "entry_time_ist", "exit_time_ist", "entry_price_reported", "exit_price_reported",
        "mfe_pct_from_entry_basis", "mae_pct_from_entry_basis",
        "peak_to_exit_giveback_pct", "available_profit_captured_pct",
        "minutes_to_mfe", "underlying_move_pct", "reported_option_return_pct",
        "max_option_ltp_during_trade", "max_option_ltp_time_ist",
        "min_option_ltp_during_trade", "min_option_ltp_time_ist",
        "post_exit_max_option_ltp", "post_exit_max_time_ist",
        "post_exit_min_option_ltp", "post_exit_min_time_ist",
    )
    return {key: row.get(key, "") for key in keys}


def build_analysis_pack(output_path: str, all_options_manifest: dict) -> dict:
    path = Path(output_path)
    if not path.exists():
        return {}

    with zipfile.ZipFile(path, "r") as zf:
        metric_rows, metric_sources = _find_metric_rows(zf)
        chart_sources = _chart_sources(zf)

    trade_queue = []
    for row in metric_rows:
        trade_queue.append({
            "trade": _metric_summary(row),
            "analysis_objective": {
                "entry": "Was genuine directional momentum present before/at entry, and how late was the executed entry versus the earliest documented qualifying setup/state?",
                "exit": "When did momentum first materially deteriorate or reverse, and how late/early was the actual exit versus documented opposing/reversal conditions?",
                "efficiency": "Use MFE, MAE, profit captured and peak-to-exit giveback as outcome measures, not as pattern definitions.",
            },
            "candidate_patterns": [p["id"] for p in PATTERN_REGISTRY],
            "required_evidence": [
                "downloaded underlying/option tick or 1-minute path",
                "sufficient pre-entry P&F context using tested box/reversal/price-type settings",
                "timestamped AlgoStra entry/exit/activity events",
                "documented or platform-native definition before declaring a pattern match",
            ],
            "status": "queued_for_evidence_correlation",
        })

    no_trade_queue = []
    for strategy in all_options_manifest.get("strategies", []):
        if int(strategy.get("position_trade_count") or 0) == 0:
            no_trade_queue.append({
                "strategy_name": strategy.get("strategy_name", ""),
                "scope": strategy.get("scope", ""),
                "first_event_ist": strategy.get("first_event_ist", ""),
                "last_event_ist": strategy.get("last_event_ist", ""),
                "analysis_objective": "Determine whether meaningful momentum occurred during the eligible strategy window despite no trade; if yes, identify which documented P&F/indicator conditions appeared and which configured condition blocked entry.",
                "candidate_patterns": [p["id"] for p in PATTERN_REGISTRY],
                "status": "queued_for_missed_momentum_review",
            })

    return {
        "schema_version": "momentum-pattern-correlation-v1.1",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "scope": "stock_options_and_index_options",
        "principle": "Judge whether entries coincide with genuine momentum and exits coincide with deterioration/reversal; do not optimise rules from isolated trades.",
        "pattern_registry": PATTERN_REGISTRY,
        "metric_source_files": metric_sources,
        "chart_source_inventory": chart_sources,
        "executed_trade_queue_count": len(trade_queue),
        "no_trade_strategy_queue_count": len(no_trade_queue),
        "executed_trade_queue": trade_queue,
        "no_trade_strategy_queue": no_trade_queue,
        "correlation_output_fields": [
            "earliest_probable_setup_time", "confirmed_pattern_time", "momentum_acceleration_time",
            "strategy_signal_time", "actual_entry_time", "entry_delay_minutes_or_boxes",
            "mfe_time", "mfe_pct", "mae_pct", "momentum_deterioration_time",
            "first_opposing_pattern_time", "actual_exit_time", "exit_delay_minutes_or_boxes",
            "peak_to_exit_giveback_pct", "available_profit_captured_pct",
            "post_exit_continuation_or_reversal", "entry_quality_class", "exit_quality_class",
            "evidence_grade", "doc_definition_reference",
        ],
        "quality_rule": "A pattern/indicator is marked observed only when reconstructed market data and a documented or platform-native definition both support it. Otherwise status remains unknown/not-computable.",
    }


def _queue_csv(pack: dict) -> str:
    fields = [
        "trade_number", "strategy_name", "underlying", "tradingsymbol", "optiontype",
        "entry_time_ist", "exit_time_ist", "mfe_pct_from_entry_basis",
        "mae_pct_from_entry_basis", "peak_to_exit_giveback_pct",
        "available_profit_captured_pct", "status",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for item in pack.get("executed_trade_queue", []):
        trade = item.get("trade", {})
        row = {key: trade.get(key, "") for key in fields}
        row["status"] = item.get("status", "")
        writer.writerow(row)
    return buf.getvalue()


def append_analysis_to_zip(output_path: str, all_options_manifest: dict) -> dict:
    pack = build_analysis_pack(output_path, all_options_manifest)
    if not pack:
        return {}
    with zipfile.ZipFile(output_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("momentum_correlation/MOMENTUM_PATTERN_ANALYSIS.json", json.dumps(pack, indent=2, ensure_ascii=False))
        zf.writestr("momentum_correlation/TRADE_CORRELATION_QUEUE.csv", _queue_csv(pack))
        zf.writestr("momentum_correlation/PATTERN_REGISTRY.json", json.dumps(PATTERN_REGISTRY, indent=2, ensure_ascii=False))
    return pack
