# Run: python -m BusinessLogic.vwappiercing_options.tests.test_fetch_historical_candles --key <path> [--date YYYY-MM-DD] [--interval 15]
# -*- coding: utf-8 -*-
"""
test_fetch_historical_candles.py

Fetches a past trading day's NIFTY future candles (default: the previous
trading day) and prints them, so you can confirm the future symbol resolves
correctly. Note: the broker's VWAP column (intvwap) is a per-candle
(interval) VWAP, not the cumulative session VWAP the pattern engine needs --
confirmed by its volatility mirroring price itself rather than smoothing out
through the day -- so the engine always computes VWAP itself via
compute_vwap() and ignores intvwap entirely. Uses historical data, so this
works even when the market is closed today.
"""
import argparse

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BrokerUtility.broker_platform.zebu.zebumynt_utility import zebumynt_utitlity
from Utility.nse_utility import nse_utitlity
from DataTypes.defines import VWAP


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch a past trading day's NIFTY future candles")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    parser.add_argument("--date", type=str, default=None, help="Trade date to fetch, YYYY-MM-DD (default: previous trading day)")
    parser.add_argument("--interval", type=int, default=15, help="Candle interval in minutes (default: 15)")
    args = parser.parse_args()

    login = UserInterfaceLogin(args.key).get_data()
    broker = zebumynt_utitlity(user_name=login.user_id, client_id=login.api_key,
                               secret_id=login.api_secret_key, pin=login.password,
                               totp=login.totp_key, phone_no=login.phone_no)

    nse_utility = nse_utitlity()
    trade_date = args.date or nse_utility.get_prev_day_trade_date(preset=0)
    print("Using trade date:", trade_date)

    current_week_expiry, next_week_expiry, monthly_expiry, is_expiry_day, \
        is_current_week_monthly_expiry, is_next_week_monthly_expiry = \
        nse_utility.get_index_expiry_date("NIFTY")
    future_symbol = broker.get_future_name("NIFTY", current_week_expiry)
    print("Future symbol:", future_symbol)

    str_from_date = f"{trade_date} 09:15:00"
    str_to_date = f"{trade_date} 15:30:00"
    # market_type="" tells Zebu's fetchOHLC to parse the symbol as an OPTION; a future needs
    # any other non-empty value (see LogicVwapPiercingOptions.FUTURE_MARKET_TYPE) so it's used
    # as-is and resolved to NFO.
    candle_data = broker.fetchOHLC(future_symbol, str_from_date, str_to_date,
                                   interval=f"{args.interval}minute", all_data=True, market_type="FUT")

    if candle_data is None or len(candle_data) == 0:
        print("No candle data returned -- check the symbol/date/interval, or that the broker "
              "session is valid.")
    else:
        print("Candle count:", len(candle_data))
        print("Columns:", list(candle_data.columns))
        has_vwap = VWAP in candle_data.columns and (candle_data[VWAP] > 0).any()
        print("Broker-supplied intvwap column populated:", has_vwap,
             "(informational only -- the engine ignores this and always computes VWAP itself,"
             " since intvwap is per-candle not cumulative session VWAP)")
        print("\nFirst 5 candles:")
        print(candle_data.head())
        print("\nLast 5 candles:")
        print(candle_data.tail())
