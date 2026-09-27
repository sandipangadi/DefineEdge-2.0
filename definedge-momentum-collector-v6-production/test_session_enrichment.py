from datetime import datetime
from zoneinfo import ZoneInfo

from session_enrichment import (
    _abs_change,
    _flow_label,
    _pct_change,
    _session_bounds,
)


IST = ZoneInfo("Asia/Kolkata")


def test_session_bounds_are_full_nse_cash_session():
    entry = datetime(2026, 9, 17, 11, 42, tzinfo=IST)
    start, end = _session_bounds(entry)
    assert start == datetime(2026, 9, 17, 9, 15, tzinfo=IST)
    assert end == datetime(2026, 9, 17, 15, 30, tzinfo=IST)


def test_price_and_oi_change_helpers():
    assert _pct_change(100.0, 110.0) == 10.0
    assert _pct_change(0.0, 110.0) is None
    assert _abs_change(1000.0, 850.0) == -150.0
    assert _abs_change(None, 850.0) is None


def test_futures_price_oi_taxonomy():
    assert _flow_label(1.0, 100.0) == "LONG_BUILDUP"
    assert _flow_label(1.0, -100.0) == "SHORT_COVERING"
    assert _flow_label(-1.0, 100.0) == "SHORT_BUILDUP"
    assert _flow_label(-1.0, -100.0) == "LONG_UNWINDING"
    assert _flow_label(0.0, 100.0) == "FLAT_OR_MIXED"
    assert _flow_label(None, 100.0) == "UNAVAILABLE"


def test_short_covering_requires_price_up_and_oi_down():
    # Guard against the research error of calling any OI decline
    # "short covering" without the corresponding futures price move.
    assert _flow_label(-0.5, -100.0) != "SHORT_COVERING"
    assert _flow_label(0.5, 100.0) != "SHORT_COVERING"
    assert _flow_label(0.5, -100.0) == "SHORT_COVERING"
