# -*- coding: utf-8 -*-
"""
backtest_engine.py

Thin entry point for the historical backtest replay. The actual state
machine (Piercing -> Reclaim -> Confirm/Entry -> SL/Exit-1..4/EOD) lives in
piercing_engine.py's VwapPiercingEngine, shared verbatim with the live
engine (Logic/vwap_piercing_options.py) so a rule change can't require
touching two places and drifting between them -- only this file's job is to
adapt that shared engine's BACKTEST mode to the run_backtest_for_day(...)
function signature run_backtest.py and tests/test_pattern_dry_run.py expect.

Rows are built as paper_trade_row (same dataclass the live engine writes to
PaperTradeData) since BackTestData now uses an identical sheet layout --
option_name/option_price fields are just left blank/0.0 here since there's
no real historical option premium data to fill them with.
"""
from .piercing_engine import (
    VwapPiercingEngine, Mode, STATUS_NO_DATA, STATUS_OK,
    determine_best_case_exit, describe_exit_outcomes, _exit_pnl_points,
)
from .pattern_rules import resolve_front_month_future_symbol


def run_backtest_for_day(broker, index_name, trade_date_str, candle_interval_minutes, log_fn=None):
    """
    Returns (list_of_paper_trade_row, future_symbol, status) for one trading day.
    log_fn, if given, is called with a string for each pattern event (Piercing/Reclaim/
    invalidation/Confirm-Entry/SL-close) -- pass print for a verbose dry-run trace.
    """
    engine = VwapPiercingEngine(mode=Mode.BACKTEST, broker=broker, index_name=index_name,
                                trade_date_str=trade_date_str, candle_interval_minutes=candle_interval_minutes,
                                log_fn=log_fn)
    return engine.run_backtest_day()
