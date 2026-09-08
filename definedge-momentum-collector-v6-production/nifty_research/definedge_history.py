"""Definedge history client for NIFTY research mode only.

Authentication is injected by caller; no credentials are stored in source.
"""
from __future__ import annotations
from datetime import date
from pathlib import Path
import io
import pandas as pd
import requests

BASE = "https://data.definedgesecurities.com/sds/history"


def history_url(segment: str, token: str, timeframe: str, from_date: date, to_date: date) -> str:
    return f"{BASE}/{segment}/{token}/{timeframe}/{from_date.isoformat()}/{to_date.isoformat()}"


def fetch_history(api_session_key: str, segment: str, token: str, from_date: date, to_date: date, timeframe: str = "minute", timeout: int = 45) -> pd.DataFrame:
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")
    r = requests.get(history_url(segment, token, timeframe, from_date, to_date), headers={"Authorization": api_session_key}, timeout=timeout)
    r.raise_for_status()
    text = r.text.strip()
    if not text:
        return pd.DataFrame()
    # Definedge history responses are CSV-like in the existing collector architecture.
    df = pd.read_csv(io.StringIO(text))
    if df.empty:
        return df
    dt_col = next((c for c in df.columns if str(c).lower().replace(" ", "") in {"dateandtime", "datetime", "timestamp"}), None)
    if dt_col:
        df[dt_col] = pd.to_datetime(df[dt_col], errors="coerce")
        df = df.sort_values(dt_col).drop_duplicates(subset=[dt_col], keep="last")
    return df.reset_index(drop=True)


def save_raw(df: pd.DataFrame, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
