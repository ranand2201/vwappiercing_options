# Run: python -m BusinessLogic.vwappiercing_options.tests.test_write_sample_trade --key <path_to_service_account_json>
# -*- coding: utf-8 -*-
"""
test_write_sample_trade.py

Standalone smoke test for the PaperTradeData gsheet write path. Bypasses the
broker login and candle/state-machine engine entirely -- useful for testing
on a day the market is closed. Writes one fake row to the PaperTradeData
worksheet of the "VWAPPiercingOptions" Google Sheet.
"""
import argparse
from datetime import date, datetime

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.paper_trade.paper_trade import UserInterfacePaperTrade
from BusinessLogic.vwappiercing_options.DataTypes.paper_trade_data import paper_trade_row, candle_snapshot, exit_hit, eod_exit


def build_sample_row():
    row = paper_trade_row()
    row.date = date.today().strftime("%Y-%m-%d")
    row.future = "NIFTY31JUL25F"
    row.option_name = "NIFTY31JUL2524600CE"
    row.trade_type = "BUY"

    row.piercing_candle = candle_snapshot(timestamp="09:30:00", open=24550.0, high=24610.0,
                                          low=24540.0, close=24600.0, vwap=24580.0)
    row.reclaim_candle = candle_snapshot(timestamp="09:45:00", open=24600.0, high=24605.0,
                                         low=24560.0, close=24565.0, vwap=24590.0)
    row.confirm_candle = candle_snapshot(timestamp="10:00:00", open=24610.0, high=24610.0,
                                         low=24610.0, close=24610.0, vwap=0.0)

    row.entry_future_price = 24610.0
    row.entry_option_price = 105.0
    row.entry_timestamp = "10:00:15"

    row.sl_hit = exit_hit(future_price=24540.0, option_price=70.0, timestamp="10:20:00", is_hit=True)
    row.exit1_hit = exit_hit(future_price=24680.0, option_price=145.0, timestamp="11:05:00", is_hit=True)
    row.exit2_hit = exit_hit(future_price=24733.0, option_price=170.0, timestamp="12:10:00", is_hit=False)
    row.exit3_hit = exit_hit(future_price=24795.0, option_price=190.0, timestamp="", is_hit=False)
    row.exit4_hit = exit_hit(future_price=24500.0, option_price=60.0, timestamp="", is_hit=False)
    row.exit5_eod = eod_exit(future_price=24650.0, option_price=120.0)

    row.mae = -70.0
    row.mae_time = "10:20:00"
    row.mfe = 145.0
    row.mfe_time = "11:05:00"
    return row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write one sample paper-trade row to PaperTradeData")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    args = parser.parse_args()

    print("Writing sample row via UserInterfacePaperTrade...")
    writer = UserInterfacePaperTrade(args.key)
    writer.write_trade(build_sample_row())
    print("Done. Check the PaperTradeData tab in the VWAPPiercingOptions Google Sheet for the new row.")
