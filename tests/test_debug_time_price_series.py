# Run: python -m BusinessLogic.vwappiercing_options.tests.test_debug_time_price_series --key <path> [--date YYYY-MM-DD]
# -*- coding: utf-8 -*-
"""
test_debug_time_price_series.py

fetchOHLC's raw-response print is commented out and its except swallows
errors, so a failure there is a black box. This calls the underlying
self.zebumynt.get_time_price_series() directly (same call fetchOHLC makes)
and prints the exact request args, raw response, and any exception -- to
see why candle data isn't coming back even after token resolution works.
"""
import argparse
import traceback

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BrokerUtility.broker_platform.zebu.zebumynt_utility import zebumynt_utitlity
from Utility.nse_utility import nse_utitlity


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Debug the raw get_time_price_series call")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    parser.add_argument("--date", type=str, default=None, help="Trade date, YYYY-MM-DD (default: previous trading day)")
    parser.add_argument("--interval", type=int, default=15, help="Candle interval in minutes")
    args = parser.parse_args()

    login = UserInterfaceLogin(args.key).get_data()
    broker = zebumynt_utitlity(user_name=login.user_id, client_id=login.api_key,
                               secret_id=login.api_secret_key, pin=login.password,
                               totp=login.totp_key, phone_no=login.phone_no)

    nse_utility = nse_utitlity()
    trade_date = args.date or nse_utility.get_prev_day_trade_date(preset=0)
    current_week_expiry, *_ = nse_utility.get_index_expiry_date("NIFTY")
    future_symbol = broker.get_future_name("NIFTY", current_week_expiry)

    exchange, token = broker.get_updated_exchange_token(future_symbol)
    print("future_symbol:", future_symbol)
    print("resolved exchange:", exchange, "token:", token)

    str_from_date = f"{trade_date} 09:15:00"
    str_to_date = f"{trade_date} 15:30:00"
    print("starttime:", str_from_date, "endtime:", str_to_date, "interval:", args.interval)

    print("\n--- calling self.zebumynt.get_time_price_series directly ---")
    try:
        response = broker.zebumynt.get_time_price_series(exchange=exchange, token=token,
                                                          starttime=str_from_date, endtime=str_to_date,
                                                          interval=int(args.interval))
        print("Response type:", type(response))
        print("Response:", response)
    except Exception:
        print("Exception raised:")
        traceback.print_exc()

    print("\n--- also trying epoch-second start/end times, in case the API expects that instead ---")
    import calendar
    from datetime import datetime
    epoch_from = calendar.timegm(datetime.strptime(str_from_date, "%Y-%m-%d %H:%M:%S").timetuple())
    epoch_to = calendar.timegm(datetime.strptime(str_to_date, "%Y-%m-%d %H:%M:%S").timetuple())
    print("epoch starttime:", epoch_from, "endtime:", epoch_to)
    try:
        response2 = broker.zebumynt.get_time_price_series(exchange=exchange, token=token,
                                                           starttime=epoch_from, endtime=epoch_to,
                                                           interval=int(args.interval))
        print("Response type:", type(response2))
        print("Response:", response2)
    except Exception:
        print("Exception raised:")
        traceback.print_exc()
