from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float

    @property
    def is_red(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class RedBarLevels:
    source_ts: str
    high: float
    low: float
    level_044: float
    level_050: float
    level_056: float
    orientation: str
    version: str = "RB-044-050-056-v1"

    def to_dict(self) -> dict:
        return asdict(self)


def fib_price(low: float, high: float, ratio: float) -> float:
    """Absolute price at a Fibonacci ratio within [low, high]."""
    if high < low:
        low, high = high, low
    return low + (high - low) * ratio


def levels_from_red_bar(candle: Candle) -> RedBarLevels:
    """Freeze 0.44/0.50/0.56 as absolute prices from a completed red 5-min candle.

    These levels are later overlaid on Renko; they must not be re-anchored to Renko bricks.
    """
    if not candle.is_red:
        raise ValueError("Red-Bar source candle must be red (close < open).")
    return RedBarLevels(
        source_ts=candle.ts,
        high=float(candle.high),
        low=float(candle.low),
        level_044=fib_price(candle.low, candle.high, 0.44),
        level_050=fib_price(candle.low, candle.high, 0.50),
        level_056=fib_price(candle.low, candle.high, 0.56),
        orientation="LOW_TO_HIGH",
    )


def first_red_after_ex_candle(candles: Sequence[Candle], ex_index: int = 0) -> Candle:
    """Return first completed red candle after the designated Ex-candle index.

    Selection rule is intentionally explicit and versionable. Caller is responsible for
    passing completed 5-minute candles in chronological order.
    """
    if not candles:
        raise ValueError("No candles supplied.")
    if ex_index < 0 or ex_index >= len(candles):
        raise IndexError("ex_index out of range")
    for candle in candles[ex_index + 1 :]:
        if candle.is_red:
            return candle
    raise LookupError("No completed red candle found after Ex-candle.")


def classify_price(price: float, levels: RedBarLevels) -> str:
    """Classify current price relative to the frozen 0.44/0.56 trigger band."""
    if price > levels.level_056:
        return "ABOVE_056"
    if price < levels.level_044:
        return "BELOW_044"
    if price >= levels.level_050:
        return "MID_TO_056"
    return "044_TO_MID"


@dataclass(frozen=True)
class RenkoState:
    last_price: float
    last_direction: str  # UP / DOWN
    consecutive_bricks: int
    box_size: float = 12.5
    version: str = "TRADITIONAL-RENKO-12.5-v1"


def timing_state(levels: RedBarLevels, renko: RenkoState) -> str:
    """Shadow timing state; no trade is triggered solely by this provisional gate."""
    zone = classify_price(renko.last_price, levels)
    if zone == "ABOVE_056" and renko.last_direction.upper() == "UP":
        return "BULLISH_CONFIRMATION_SHADOW"
    if zone == "BELOW_044" and renko.last_direction.upper() == "DOWN":
        return "BEARISH_CONFIRMATION_SHADOW"
    return "WAIT_SHADOW"
