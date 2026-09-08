"""Automated six-month NIFTY50/F&O momentum-regime research.

Research-only. No order placement.

Design:
1) Download latest Definedge NSE cash + NSE F&O master files.
2) Restrict to current NIFTY50 symbols that are F&O eligible.
3) Fetch six months of NSE 1-minute underlying history.
4) Rank by six-month traded-value proxy and freeze top 20.
5) Build daily regime labels from top-20 breadth/return/dispersion.
6) Extract intraday momentum episodes and measure 30-minute MFE/MAE.
7) Split chronologically 4 months discovery / 2 months validation.
8) Emit CSV/JSON evidence plus a ZIP ready for Trading Brain Drive.

Important limitation: current Definedge master files contain live contracts; expired
option-contract history across the full six-month window requires archived contract
masters/tokens. This module therefore derives the underlying/regime mathematical
admission layer and records that historical option-premium validation is pending.
"""
from __future__ import annotations

import csv
import io
import json
import math
import statistics
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests

NSE_CASH_MASTER = "https://app.definedgesecurities.com/public/nsecash.zip"
NSE_FNO_MASTER = "https://app.definedgesecurities.com/public/nsefno.zip"
HISTORY_BASE = "https://data.definedgesecurities.com/sds/history"

# Current research universe seed. F&O eligibility is verified dynamically from the
# Definedge master before any symbol is used. Keeping the 50-name seed explicit also
# makes the research reproducible if index membership changes later.
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE",
    "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM",
    "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "WIPRO",
]


@dataclass
class Bar:
    dt: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


def _f(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _download_zip_csv(url: str) -> list[list[str]]:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"No CSV found in master archive: {url}")
        text = zf.read(names[0]).decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def _master_maps():
    cash_rows = _download_zip_csv(NSE_CASH_MASTER)
    fno_rows = _download_zip_csv(NSE_FNO_MASTER)

    cash = {}
    for row in cash_rows:
        if len(row) < 4:
            continue
        seg, token, symbol, tradingsym = row[:4]
        sym = symbol.strip().upper()
        if seg.strip().upper() == "NSE" and sym:
            # Prefer EQ rows when duplicate symbol records exist.
            if sym not in cash or tradingsym.strip().upper().endswith("-EQ"):
                cash[sym] = {"token": token.strip(), "tradingsym": tradingsym.strip()}

    eligible = set()
    for row in fno_rows:
        if len(row) < 5:
            continue
        seg, _token, symbol, _tradingsym, instrument = row[:5]
        if seg.strip().upper() == "NFO" and instrument.strip().upper() in {"FUTSTK", "OPTSTK"}:
            eligible.add(symbol.strip().upper())
    return cash, eligible


def _parse_dt(s: str) -> datetime | None:
    s = str(s).strip()
    fmts = (
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def fetch_minute_history(token: str, start: datetime, end: datetime, session_key: str) -> list[Bar]:
    f = start.strftime("%d%m%Y%H%M")
    t = end.strftime("%d%m%Y%H%M")
    url = f"{HISTORY_BASE}/NSE/{token}/minute/{f}/{t}"
    r = requests.get(url, headers={"Authorization": session_key}, timeout=90)
    r.raise_for_status()
    out = []
    for row in csv.reader(io.StringIO(r.text)):
        if len(row) < 6:
            continue
        dt = _parse_dt(row[0])
        if not dt:
            continue
        out.append(Bar(dt, _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]), _f(row[5])))
    out.sort(key=lambda b: b.dt)
    return out


def _daily_stats(bars: list[Bar]):
    byday = defaultdict(list)
    for b in bars:
        byday[b.dt.date()].append(b)
    result = {}
    for d, xs in byday.items():
        xs.sort(key=lambda b: b.dt)
        if not xs or xs[0].o <= 0:
            continue
        o, c = xs[0].o, xs[-1].c
        h = max(x.h for x in xs)
        l = min(x.l for x in xs)
        traded_value = sum(max(x.c, 0) * max(x.v, 0) for x in xs)
        result[d] = {
            "open": o, "close": c, "ret": (c / o - 1.0) if o else 0.0,
            "range": (h - l) / o if o else 0.0, "traded_value": traded_value,
        }
    return result


def _median(xs):
    return statistics.median(xs) if xs else 0.0


def _rank_top20(histories: dict[str, list[Bar]]):
    rows = []
    for sym, bars in histories.items():
        ds = _daily_stats(bars)
        vals = [x["traded_value"] for x in ds.values() if x["traded_value"] > 0]
        rows.append({"symbol": sym, "median_daily_traded_value": _median(vals), "days": len(ds)})
    rows.sort(key=lambda x: x["median_daily_traded_value"], reverse=True)
    return rows[:20], rows


def _build_regimes(top20: list[str], histories: dict[str, list[Bar]]):
    daily = {s: _daily_stats(histories[s]) for s in top20}
    dates = sorted(set().union(*(set(x.keys()) for x in daily.values())))
    raw = []
    dispersions = []
    for d in dates:
        rets = [daily[s][d]["ret"] for s in top20 if d in daily[s]]
        if len(rets) < 10:
            continue
        ew = statistics.mean(rets)
        breadth = sum(r > 0 for r in rets) / len(rets)
        disp = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        dispersions.append(disp)
        raw.append((d, ew, breadth, disp))
    disp_cut = _median(dispersions) * 1.35 if dispersions else 0.0
    regimes = {}
    for d, ew, breadth, disp in raw:
        if ew >= 0.004 and breadth >= 0.65:
            label = "BULL_TREND"
        elif ew <= -0.004 and breadth <= 0.35:
            label = "BEAR_TREND"
        elif disp >= disp_cut and abs(ew) < 0.004:
            label = "HIGH_DISPERSION_ROTATION"
        else:
            label = "NEUTRAL_ROTATION"
        regimes[d] = {"ew_return": ew, "breadth": breadth, "dispersion": disp, "regime": label}
    return regimes


def _episodes(symbol: str, bars: list[Bar], regimes: dict, horizon=30):
    events = []
    # 20-minute price impulse + relative recent volume. Downsample checks to every 5th
    # minute to reduce overlapping observations while preserving intraday structure.
    for i in range(60, len(bars) - horizon, 5):
        b = bars[i]
        if b.dt.date() != bars[i-20].dt.date():
            continue
        p0 = bars[i-20].c
        if p0 <= 0 or b.c <= 0:
            continue
        ret20 = b.c / p0 - 1.0
        if abs(ret20) < 0.005:
            continue
        recent_v = sum(max(x.v, 0) for x in bars[i-19:i+1])
        base_v = sum(max(x.v, 0) for x in bars[i-59:i-19]) / 2.0
        vr = recent_v / base_v if base_v > 0 else 1.0
        direction = 1 if ret20 > 0 else -1
        entry = b.c
        future = bars[i+1:i+1+horizon]
        if not future or any(x.dt.date() != b.dt.date() for x in future):
            continue
        if direction > 0:
            mfe = max(x.h / entry - 1.0 for x in future)
            mae = max(0.0, max(1.0 - x.l / entry for x in future))
        else:
            mfe = max(entry / x.l - 1.0 for x in future if x.l > 0)
            mae = max(0.0, max(x.h / entry - 1.0 for x in future))
        rg = regimes.get(b.dt.date(), {"regime": "UNKNOWN", "breadth": 0.5, "ew_return": 0.0, "dispersion": 0.0})
        aligned = int((direction > 0 and rg["regime"] == "BULL_TREND") or (direction < 0 and rg["regime"] == "BEAR_TREND"))
        rotational = int("ROTATION" in rg["regime"])
        score = abs(ret20) / 0.005 + 0.35 * math.log1p(max(vr, 0)) + 0.75 * aligned - 0.35 * rotational
        success = int(mfe >= 0.0075 and (mae <= 0 or mfe / max(mae, 1e-6) >= 1.5))
        events.append({
            "symbol": symbol, "datetime": b.dt.isoformat(sep=" "), "date": b.dt.date().isoformat(),
            "direction": "BULL" if direction > 0 else "BEAR", "ret20": ret20,
            "volume_ratio": vr, "regime": rg["regime"], "breadth": rg["breadth"],
            "market_return": rg["ew_return"], "dispersion": rg["dispersion"],
            "regime_aligned": aligned, "rotational": rotational,
            "entry": entry, "mfe30": mfe, "mae30": mae, "raw_score": score, "success": success,
        })
    return events


def _mean(rows, key):
    xs = [float(r[key]) for r in rows if r.get(key) is not None]
    return statistics.mean(xs) if xs else 0.0


def _derive_formula(train: list[dict]):
    wins = [r for r in train if r["success"] == 1]
    losses = [r for r in train if r["success"] == 0]
    features = ["ret20_abs", "volume_ratio", "regime_aligned", "rotational", "breadth_directional"]
    expanded = []
    for r in train:
        x = dict(r)
        x["ret20_abs"] = abs(r["ret20"])
        x["breadth_directional"] = r["breadth"] if r["direction"] == "BULL" else 1.0 - r["breadth"]
        expanded.append(x)
    wins_e = [x for x in expanded if x["success"] == 1]
    losses_e = [x for x in expanded if x["success"] == 0]
    weights = {}
    for f in features:
        allv = [float(x[f]) for x in expanded]
        sd = statistics.pstdev(allv) if len(allv) > 1 else 0.0
        diff = _mean(wins_e, f) - _mean(losses_e, f)
        weights[f] = diff / sd if sd > 1e-12 else 0.0
    # Keep formula interpretable and bounded rather than fitting a high-dimensional model.
    return weights


def _formula_score(r: dict, w: dict):
    breadth_dir = r["breadth"] if r["direction"] == "BULL" else 1.0 - r["breadth"]
    vals = {
        "ret20_abs": abs(r["ret20"]), "volume_ratio": r["volume_ratio"],
        "regime_aligned": r["regime_aligned"], "rotational": r["rotational"],
        "breadth_directional": breadth_dir,
    }
    return sum(w.get(k, 0.0) * vals[k] for k in vals)


def _metrics(rows: list[dict]):
    if not rows:
        return {"events": 0}
    return {
        "events": len(rows), "success_rate": sum(r["success"] for r in rows) / len(rows),
        "avg_mfe30": _mean(rows, "mfe30"), "avg_mae30": _mean(rows, "mae30"),
        "median_mfe30": _median([r["mfe30"] for r in rows]),
        "median_mae30": _median([r["mae30"] for r in rows]),
    }


def run_six_month_top20(session_key: str, output_root: Path | None = None, days: int = 183):
    if not session_key:
        raise RuntimeError("Definedge session key is required for six-month research.")
    end = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    start = end - timedelta(days=days)
    cash, eligible = _master_maps()
    universe = [s for s in NIFTY50 if s in eligible and s in cash]
    if len(universe) < 20:
        raise RuntimeError(f"Only {len(universe)} NIFTY50 symbols resolved as current F&O eligible; need at least 20.")

    histories = {}
    failures = {}
    for sym in universe:
        try:
            bars = fetch_minute_history(cash[sym]["token"], start, end, session_key)
            if len(bars) >= 500:
                histories[sym] = bars
            else:
                failures[sym] = f"insufficient bars: {len(bars)}"
        except Exception as exc:
            failures[sym] = str(exc)
    if len(histories) < 20:
        raise RuntimeError(f"Only {len(histories)} symbols returned usable six-month minute data; failures={failures}")

    top20_rows, all_ranked = _rank_top20(histories)
    top20 = [r["symbol"] for r in top20_rows]
    regimes = _build_regimes(top20, histories)
    events = []
    for sym in top20:
        events.extend(_episodes(sym, histories[sym], regimes))
    events.sort(key=lambda r: r["datetime"])
    if not events:
        raise RuntimeError("No momentum episodes were generated from the six-month sample.")

    split_dt = start + timedelta(days=days * 2 / 3)
    train = [r for r in events if datetime.fromisoformat(r["datetime"]) < split_dt]
    valid = [r for r in events if datetime.fromisoformat(r["datetime"]) >= split_dt]
    weights = _derive_formula(train)
    for r in events:
        r["formula_score"] = _formula_score(r, weights)

    # Evaluate top score quartile separately; threshold learned only from discovery sample.
    train_scores = sorted(r["formula_score"] for r in train)
    q75 = train_scores[int(0.75 * (len(train_scores)-1))] if train_scores else 0.0
    train_hi = [r for r in train if r["formula_score"] >= q75]
    valid_hi = [r for r in valid if r["formula_score"] >= q75]

    by_regime = {}
    for label in sorted(set(r["regime"] for r in events)):
        by_regime[label] = {
            "discovery": _metrics([r for r in train if r["regime"] == label]),
            "validation": _metrics([r for r in valid if r["regime"] == label]),
        }

    summary = {
        "research_version": "TOP20_6M_V1",
        "generated_at": datetime.now().isoformat(),
        "period": {"from": start.isoformat(), "to": end.isoformat(), "days": days},
        "selection": "NIFTY50 -> current F&O eligible -> top20 by six-month median daily underlying traded-value proxy",
        "top20": top20,
        "formula_features": weights,
        "formula": "Score = sum(weight_i * feature_i); weights are standardized discovery-sample winner-vs-loser mean differences",
        "discovery": _metrics(train), "validation": _metrics(valid),
        "discovery_top_quartile": _metrics(train_hi), "validation_top_quartile": _metrics(valid_hi),
        "score_threshold_q75_discovery": q75, "by_regime": by_regime,
        "failures": failures,
        "limitations": [
            "This V1 is the underlying/regime admission layer, not a completed historical option-premium backtest.",
            "Expired six-month option contracts require archived contract token masters or another historical-option source before ATM-1/ATM/ATM+1 premium MFE/MAE can be validated.",
            "No live trading rule is promoted from this research automatically; forward validation remains mandatory.",
        ],
    }

    root = Path(output_root or tempfile.mkdtemp(prefix="top20_6m_"))
    root.mkdir(parents=True, exist_ok=True)
    rank_path = root / "Top20_Universe_Ranking.csv"
    with rank_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "median_daily_traded_value", "days"])
        w.writeheader(); w.writerows(all_ranked)
    regime_path = root / "Daily_Regime_Map.csv"
    with regime_path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["date", "ew_return", "breadth", "dispersion", "regime"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for d in sorted(regimes):
            w.writerow({"date": d.isoformat(), **regimes[d]})
    ev_path = root / "Momentum_Episodes.csv"
    with ev_path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = list(events[0].keys())
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(events)
    (root / "Research_Summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (root / "METHOD.txt").write_text(
        "NIFTY50 F&O TOP20 SIX-MONTH RESEARCH\n"
        "Research-only; no order placement.\n"
        "Top20 is frozen after ranking by six-month median daily traded-value proxy.\n"
        "Momentum episode: absolute 20-minute move >=0.5%; outcome horizon 30 minutes.\n"
        "Discovery/validation split: first 2/3 vs final 1/3 chronologically.\n"
        "Regime labels use equal-weight return, breadth and cross-sectional dispersion.\n"
        "Historical option-premium layer remains pending archived expired-contract identifiers.\n",
        encoding="utf-8",
    )
    zip_path = root.parent / f"Trading_Brain_NIFTY50_FNO_Top20_6M_{datetime.now().date().isoformat()}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in root.iterdir():
            if p.is_file():
                zf.write(p, arcname=p.name)
    return zip_path, summary
