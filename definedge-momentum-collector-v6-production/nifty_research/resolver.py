"""Research-only NIFTY contract resolver for DS-01/CVP-01.

No production V6 imports. Pure functions make this testable offline against NFO/NSE
master snapshots before any Definedge OTP/API collection is attempted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional
import pandas as pd


@dataclass(frozen=True)
class Contract:
    token: str
    symbol: str
    expiry: date
    strike: Optional[float]
    option_type: Optional[str]
    instrument: str


def _col(df: pd.DataFrame, *names: str) -> str:
    norm = {str(c).strip().lower().replace(" ", "").replace("_", ""): c for c in df.columns}
    for name in names:
        key = name.lower().replace(" ", "").replace("_", "")
        if key in norm:
            return norm[key]
    raise KeyError(f"Missing master column; tried {names}; available={list(df.columns)}")


def normalize_nfo_master(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize common Definedge/NSE master naming without assuming fixed column order."""
    out = pd.DataFrame()
    out["token"] = df[_col(df, "token", "securityid", "instrumenttoken")].astype(str).str.strip()
    out["symbol"] = df[_col(df, "symbol", "tradingsymbol", "trading_symbol")].astype(str).str.strip()
    out["expiry"] = pd.to_datetime(df[_col(df, "expiry", "expirydate", "expiry_date")], errors="coerce", dayfirst=True).dt.date
    try:
        out["strike"] = pd.to_numeric(df[_col(df, "strike", "strikeprice", "strike_price")], errors="coerce")
    except KeyError:
        out["strike"] = pd.NA
    try:
        out["option_type"] = df[_col(df, "optiontype", "option_type", "opttype")].astype(str).str.upper().str.strip()
    except KeyError:
        out["option_type"] = ""
    try:
        out["instrument"] = df[_col(df, "instrument", "instrumenttype", "instrument_type")].astype(str).str.upper().str.strip()
    except KeyError:
        out["instrument"] = ""
    return out.dropna(subset=["expiry"]).reset_index(drop=True)


def nifty_rows(master: pd.DataFrame) -> pd.DataFrame:
    m = normalize_nfo_master(master)
    # Exclude FINNIFTY/MIDCPNIFTY etc.; require NIFTY prefix but not embedded alternatives.
    s = m["symbol"].str.upper()
    mask = s.str.startswith("NIFTY") & ~s.str.startswith(("NIFTYBANK", "NIFTYFIN", "NIFTYMID", "NIFTYNXT"))
    return m.loc[mask].copy()


def expiries_for(master: pd.DataFrame, trade_date: date) -> list[date]:
    n = nifty_rows(master)
    return sorted(e for e in n["expiry"].dropna().unique() if e >= trade_date)


def current_next_expiry(master: pd.DataFrame, trade_date: date) -> tuple[date, Optional[date]]:
    exps = expiries_for(master, trade_date)
    if not exps:
        raise LookupError(f"No NIFTY expiry on/after {trade_date}")
    return exps[0], exps[1] if len(exps) > 1 else None


def round_atm(spot: float, step: int = 50) -> int:
    """Deterministic half-up rounding; avoids Python banker's-rounding at x.5."""
    return int((float(spot) + step / 2) // step * step)


def option_contract(master: pd.DataFrame, expiry: date, strike: float, option_type: str) -> Contract:
    n = nifty_rows(master)
    ot = option_type.upper()
    rows = n[(n["expiry"] == expiry) & (pd.to_numeric(n["strike"], errors="coerce") == float(strike))]
    # Prefer explicit option-type column; fall back to symbol suffix for masters where type is blank.
    explicit = rows[rows["option_type"].isin([ot, "CALL" if ot == "CE" else "PUT"])]
    if explicit.empty:
        explicit = rows[rows["symbol"].str.upper().str.endswith(ot)]
    if len(explicit) != 1:
        raise LookupError(f"Expected one NIFTY {expiry} {strike:g}{ot}; found {len(explicit)}")
    r = explicit.iloc[0]
    return Contract(str(r.token), r.symbol, r.expiry, float(r.strike), ot, r.instrument)


def option_ladder(master: pd.DataFrame, trade_date: date, spot: float, offsets: Iterable[int] = (-200, 0, 200), neighbor_steps: int = 1) -> dict:
    """Resolve current/next expiry CE+PE legs plus neighboring 50-point strikes.

    Neighbors are retained to permit timestamp-aware ATM changes without look-ahead.
    Missing next expiry is represented as an empty mapping, never silently substituted.
    """
    current, nxt = current_next_expiry(master, trade_date)
    atm = round_atm(spot)
    strikes = sorted({atm + int(o) + j * 50 for o in offsets for j in range(-neighbor_steps, neighbor_steps + 1)})
    result = {"trade_date": trade_date.isoformat(), "atm": atm, "current_expiry": current.isoformat(), "next_expiry": nxt.isoformat() if nxt else None, "current": {}, "next": {}}
    for bucket, expiry in (("current", current), ("next", nxt)):
        if expiry is None:
            continue
        for strike in strikes:
            for ot in ("CE", "PE"):
                key = f"{strike}_{ot}"
                try:
                    result[bucket][key] = option_contract(master, expiry, strike, ot)
                except LookupError:
                    result[bucket][key] = None
    return result


def front_future(master: pd.DataFrame, trade_date: date) -> Contract:
    n = nifty_rows(master)
    # Futures generally have no CE/PE marker and no meaningful strike.
    strike = pd.to_numeric(n["strike"], errors="coerce")
    fut = n[(n["expiry"] >= trade_date) & (~n["option_type"].isin(["CE", "PE", "CALL", "PUT"])) & (strike.isna() | (strike == 0))]
    if not fut.empty and fut["instrument"].ne("").any():
        typed = fut[fut["instrument"].str.contains("FUT", na=False)]
        if not typed.empty:
            fut = typed
    fut = fut.sort_values("expiry")
    if fut.empty:
        raise LookupError(f"No front NIFTY future on/after {trade_date}")
    r = fut.iloc[0]
    return Contract(str(r.token), r.symbol, r.expiry, None, None, r.instrument)
