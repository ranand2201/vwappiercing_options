# Run: python -m BusinessLogic.vwappiercing_options.tests.test_pattern_dry_run --key <path> [--date YYYY-MM-DD] [--interval 15]
# -*- coding: utf-8 -*-
"""
test_pattern_dry_run.py

Offline dry run of the Piercing -> Reclaim -> Confirm -> SL/Exit pattern
against a past trading day's real candles. Thin wrapper around
Logic/backtest_engine.py's run_backtest_for_day (same replay logic
run_backtest.py uses to write BackTestData) with verbose per-event logging
turned on, so this reflects real behavior -- including the piercing-window
gating (start+15min to 14:30) and SL-triggers-next-trade behavior -- rather
than a separate copy of the state machine that could drift.

Since this only has candle-close data (no intrabar LTP ticks), entry/exit
are approximated from candle High/Low crossing the relevant levels.
"""
import argparse

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BusinessLogic.vwappiercing_options.Logic.backtest_engine import run_backtest_for_day, describe_exit_outcomes, STATUS_NO_DATA
from BrokerUtility.broker_platform.zebu.zebumynt_utility import zebumynt_utitlity
from Utility.nse_utility import nse_utitlity


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry-run the VWAP piercing pattern against a past trading day")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    parser.add_argument("--date", type=str, default=None, help="Trade date to test, YYYY-MM-DD (default: previous trading day)")
    parser.add_argument("--interval", type=int, default=15, help="Candle interval in minutes (default: 15)")
    parser.add_argument("--index", type=str, default="NIFTY", help="Index name (default: NIFTY)")
    args = parser.parse_args()

    login = UserInterfaceLogin(args.key).get_data()
    broker = zebumynt_utitlity(user_name=login.user_id, client_id=login.api_key,
                               secret_id=login.api_secret_key, pin=login.password,
                               totp=login.totp_key, phone_no=login.phone_no)

    nse_utility = nse_utitlity()
    trade_date = args.date or nse_utility.get_prev_day_trade_date(preset=0)
    print("Using trade date:", trade_date)

    results, future_symbol, status = run_backtest_for_day(broker, args.index, trade_date, args.interval, log_fn=print)

    print("Future symbol:", future_symbol)

    if status == STATUS_NO_DATA:
        print("No candle data returned -- nothing to dry-run (contract may be expired/delisted, or date/interval invalid).")
        raise SystemExit(1)

    print(f"\nDone. {len(results)} trade(s) detected on {trade_date}:")
    for r in results:
        sl_note = f"SL hit @ {r.sl_hit.timestamp}" if r.sl_hit.is_hit else "still open at EOD"
        print(f"  {r.trade_type} @ {r.entry_future_price} (entry {r.entry_timestamp}) -- {sl_note}, "
             f"MAE={r.mae:.2f} MFE={r.mfe:.2f}, exits: {describe_exit_outcomes(r)}")
