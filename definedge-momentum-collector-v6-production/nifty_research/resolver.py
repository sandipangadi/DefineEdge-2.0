"""Research-only NIFTY contract resolver for DS-01/CVP-01.

No production V6 imports. Pure functions make this testable offline against NFO/NSE
master snapshots before any Definedge OTP/API collection is attempted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
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


def _norm_name(value: object) -> str:
    return str(value).strip().lower().replace(" ", "").replace("_", "")


def _col(df: pd.DataFrame, *names: str) -> str:
    norm = {_norm_name(c): c for c in df.columns}
    for name in names:
        key = _norm_name(name)
        if key in norm:
            return norm[key]
    raise KeyError(f"Missing master column; tried {names}; available={list(df.columns)}")


def load_nfo_master_csv(path_or_buffer) -> pd.DataFrame:
    """Load a Definedge NFO/NSE master snapshot safely.

    Definedge's production snapshot used by V6 is headerless. Reading it with pandas'
    default header=0 silently turns the first contract into column names, so this loader
    deliberately uses header=None and lets normalize_nfo_master apply the positional schema.
    """
    return pd.read_csv(path_or_buffer, header=None)


def _parse_expiry(series: pd.Series) -> pd.Series:
    raw = series.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    compact = pd.to_datetime(raw.where(raw.str.fullmatch(r"\d{8}")), format="%d%m%Y", errors="coerce")
    generic = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    missing = compact.isna() & ~raw.str.lower().isin(["", "nan", "nat", "none"])
    if missing.any():
        generic.loc[missing] = pd.to_datetime(raw.loc[missing], errors="coerce", dayfirst=True)
    return compact.fillna(generic).dt.date


def _scale_strike(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    positive = s[s > 0]
    if not positive.empty and positive.median() > 100000:
        s = s / 100.0
    return s


def normalize_nfo_master(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize named or raw headerless Definedge NFO master data.

    Production headerless positional schema observed in the V6 evidence snapshot:
    0 segment, 1 token, 2 underlying, 3 trading symbol, 4 instrument type,
    5 expiry DDMMYYYY, 8 option type, 9 strike-in-paise.
    """
    known = {_norm_name(c) for c in df.columns}
    has_named_schema = bool(known & {"token", "securityid", "instrumenttoken"})
    out = pd.DataFrame(index=df.index)
    if not has_named_schema:
        if df.shape[1] < 10:
            raise KeyError(f"Headerless NFO master needs at least 10 columns; found {df.shape[1]}")
        out["token"] = df.iloc[:, 1].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out["underlying"] = df.iloc[:, 2].astype(str).str.upper().str.strip()
        out["symbol"] = df.iloc[:, 3].astype(str).str.strip()
        out["instrument"] = df.iloc[:, 4].astype(str).str.upper().str.strip()
        out["expiry"] = _parse_expiry(df.iloc[:, 5])
        out["option_type"] = df.iloc[:, 8].astype(str).str.upper().str.strip()
        out["strike"] = _scale_strike(df.iloc[:, 9])
    else:
        out["token"] = df[_col(df, "token", "securityid", "instrumenttoken")].astype(str).str.strip()
        try:
            out["underlying"] = df[_col(df, "underlying", "name", "root", "underlyingsymbol")].astype(str).str.upper().str.strip()
        except KeyError:
            out["underlying"] = ""
        out["symbol"] = df[_col(df, "symbol", "tradingsymbol", "trading_symbol")].astype(str).str.strip()
        out["expiry"] = _parse_expiry(df[_col(df, "expiry", "expirydate", "expiry_date")])
        try:
            out["strike"] = _scale_strike(df[_col(df, "strike", "strikeprice", "strike_price")])
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
    if m["underlying"].ne("").any():
        mask = m["underlying"].eq("NIFTY")
    else:
        s = m["symbol"].str.upper()
        mask = s.str.startswith("NIFTY") & ~s.str.startswith(("NIFTYBANK", "NIFTYFIN", "NIFTYMID", "NIFTYNXT"))
    return m.loc[mask].copy()


def expiries_for(master: pd.DataFrame, trade_date: date) -> list[date]:
    n = nifty_rows(master)
    is_option = n["option_type"].isin(["CE", "PE", "CALL", "PUT"]) | n["instrument"].str.contains("OPT", na=False)
    return sorted(e for e in n.loc[is_option, "expiry"].dropna().unique() if e >= trade_date)


def current_next_expiry(master: pd.DataFrame, trade_date: date) -> tuple[date, Optional[date]]:
    exps = expiries_for(master, trade_date)
    if not exps:
        raise LookupError(f"No NIFTY option expiry on/after {trade_date}")
    return exps[0], exps[1] if len(exps) > 1 else None


def round_atm(spot: float, step: int = 50) -> int:
    return int((float(spot) + step / 2) // step * step)


def option_contract(master: pd.DataFrame, expiry: date, strike: float, option_type: str) -> Contract:
    n = nifty_rows(master)
    ot = option_type.upper()
    rows = n[(n["expiry"] == expiry) & (pd.to_numeric(n["strike"], errors="coerce") == float(strike))]
    explicit = rows[rows["option_type"].isin([ot, "CALL" if ot == "CE" else "PUT"])]
    if explicit.empty:
        explicit = rows[rows["symbol"].str.upper().str.endswith(ot)]
    if len(explicit) != 1:
        raise LookupError(f"Expected one NIFTY {expiry} {strike:g}{ot}; found {len(explicit)}")
    r = explicit.iloc[0]
    return Contract(str(r.token), r.symbol, r.expiry, float(r.strike), ot, r.instrument)


def option_ladder(master: pd.DataFrame, trade_date: date, spot: float, offsets: Iterable[int] = (-200, 0, 200), neighbor_steps: int = 1) -> dict:
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
    fut = n[(n["expiry"] >= trade_date) & n["instrument"].str.contains("FUT", na=False)]
    if fut.empty:
        strike = pd.to_numeric(n["strike"], errors="coerce")
        fut = n[(n["expiry"] >= trade_date) & (~n["option_type"].isin(["CE", "PE", "CALL", "PUT"])) & (strike.isna() | (strike <= 0))]
    fut = fut.sort_values("expiry")
    if fut.empty:
        raise LookupError(f"No front NIFTY future on/after {trade_date}")
    r = fut.iloc[0]
    return Contract(str(r.token), r.symbol, r.expiry, None, None, r.instrument)


def nifty_spot_token(nse_master: pd.DataFrame) -> tuple[str, str]:
    """Resolve NIFTY 50 cash-index token from named or raw headerless NSE master."""
    known = {_norm_name(c) for c in nse_master.columns}
    has_named_schema = bool(known & {"token", "securityid", "instrumenttoken"})
    if not has_named_schema:
        if nse_master.shape[1] < 5:
            raise KeyError(f"Headerless NSE master needs at least 5 columns; found {nse_master.shape[1]}")
        token = nse_master.iloc[:, 1].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        name = nse_master.iloc[:, 2].astype(str).str.strip()
        symbol = nse_master.iloc[:, 3].astype(str).str.strip()
        instr = nse_master.iloc[:, 4].astype(str).str.upper().str.strip()
    else:
        token = nse_master[_col(nse_master, "token", "securityid", "instrumenttoken")].astype(str).str.strip()
        try:
            name = nse_master[_col(nse_master, "name", "underlying", "symbolname")].astype(str).str.strip()
        except KeyError:
            name = pd.Series("", index=nse_master.index)
        symbol = nse_master[_col(nse_master, "symbol", "tradingsymbol", "trading_symbol")].astype(str).str.strip()
        try:
            instr = nse_master[_col(nse_master, "instrument", "instrumenttype", "instrument_type")].astype(str).str.upper().str.strip()
        except KeyError:
            instr = pd.Series("", index=nse_master.index)
    label = name.str.upper().str.replace(r"\s+", " ", regex=True).str.strip()
    sym = symbol.str.upper().str.replace(r"\s+", " ", regex=True).str.strip()
    mask = (label.eq("NIFTY 50") | sym.eq("NIFTY 50")) & (instr.eq("") | instr.str.contains("IDX", na=False))
    if mask.sum() != 1:
        raise LookupError(f"Expected one NSE NIFTY 50 index token; found {int(mask.sum())}")
    i = mask[mask].index[0]
    return str(token.loc[i]), str(symbol.loc[i])
