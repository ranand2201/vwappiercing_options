from unittest.mock import Mock

from BusinessLogic.vwappiercing_options.Logic.piercing_engine import (
    Mode,
    STATE_SEEK_PIERCING,
    STATE_SEEK_RECLAIM,
    VwapPiercingEngine,
    _DirectionState,
)
from DataTypes.defines import CLOSE_PRICE, HIGH_PRICE, LOW_PRICE, OPEN_PRICE, VWAP


def _piercing_candle():
    return {
        OPEN_PRICE: 90.0,
        HIGH_PRICE: 111.0,
        LOW_PRICE: 89.0,
        CLOSE_PRICE: 110.0,
        VWAP: 100.0,
    }


def _engine():
    engine = object.__new__(VwapPiercingEngine)
    engine.mode = Mode.BACKTEST
    engine.session_date_str = "2026-11-05"
    engine.piercing_start_time = "09:35:00"
    engine.candle_interval_minutes = 5
    engine.log_fn = Mock()
    return engine


def test_piercing_seeking_starts_on_0935_candle_not_0930_candle():
    engine = _engine()
    before_window = _DirectionState("BUY")
    at_window = _DirectionState("BUY")

    engine._VwapPiercingEngine__check_seek_piercing(
        before_window, _piercing_candle(), "2026-11-05 09:30:00")
    engine._VwapPiercingEngine__check_seek_piercing(
        at_window, _piercing_candle(), "2026-11-05 09:35:00")

    assert before_window.state == STATE_SEEK_PIERCING
    assert at_window.state == STATE_SEEK_RECLAIM
    assert at_window.piercing_candle[OPEN_PRICE] == 90.0
