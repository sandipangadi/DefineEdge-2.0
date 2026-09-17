from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from .red_bar import RedBarLevels, RenkoState, timing_state


@dataclass(frozen=True)
class GateState:
    oi_state: str
    volatility_state: str
    price_structure_state: str
    regime: str
    allowed_strategy_family: str
    red_bar_shadow: str
    decision_state: str
    oar_status: str
    version: str = "PRE-OAR-V1.1"

    def to_dict(self) -> dict:
        return asdict(self)


def allowed_family_for_regime(regime: str) -> str:
    mapping = {
        "BULLISH_EXPANSION": "LONG_CALL_OR_BULL_CALL_SPREAD",
        "BEARISH_EXPANSION": "LONG_PUT_OR_BEAR_PUT_SPREAD",
        "NONDIRECTIONAL_EXPANSION": "LONG_STRADDLE_OR_LONG_STRANGLE",
        "RANGE_COMPRESSION": "DEFINED_RISK_RANGE_OBSERVATION_ONLY",
        "CONFLICTING": "NONE",
    }
    return mapping.get(regime, "NONE")


def decide(
    *,
    oi_state: str,
    volatility_state: str,
    price_structure_state: str,
    regime: str,
    red_bar_levels: Optional[RedBarLevels],
    renko_state: Optional[RenkoState],
    oar_available: bool,
) -> GateState:
    family = allowed_family_for_regime(regime)

    if red_bar_levels is not None and renko_state is not None:
        red_bar_shadow = timing_state(red_bar_levels, renko_state)
    else:
        red_bar_shadow = "UNAVAILABLE_SHADOW"

    if regime == "CONFLICTING" or family == "NONE":
        decision = "NO_TRADE"
    elif price_structure_state in {"UNKNOWN", "CONFLICTING"}:
        decision = "WATCH"
    else:
        # Red-Bar/Renko remains shadow-only until evidence promotion.
        decision = "CHECK_OAR" if oar_available else "OAR_UNAVAILABLE"

    return GateState(
        oi_state=oi_state,
        volatility_state=volatility_state,
        price_structure_state=price_structure_state,
        regime=regime,
        allowed_strategy_family=family,
        red_bar_shadow=red_bar_shadow,
        decision_state=decision,
        oar_status="AVAILABLE" if oar_available else "UNAVAILABLE",
    )
