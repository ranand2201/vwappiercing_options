# Run: python -m BusinessLogic.vwappiercing_options.tests.test_read_login_config --key <path_to_service_account_json>
# -*- coding: utf-8 -*-
"""
test_read_login_config.py

Reads the BrokerData and Config tabs of the VWAPPiercingOptions Google Sheet
and prints what was parsed. No broker or market call at all -- safe to run
anytime, including when the market is closed.
"""
import argparse

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BusinessLogic.vwappiercing_options.UserInterface.gsheet.config.config import UserInterfaceConfig


def mask(value):
    if not value:
        return value
    return value[:2] + "*" * max(len(value) - 2, 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read and print BrokerData + Config from the VWAPPiercingOptions sheet")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    args = parser.parse_args()

    print("--- Reading BrokerData ---")
    login = UserInterfaceLogin(args.key).get_data()
    print("broker:", login.broker)
    print("user_id:", login.user_id)
    print("password:", mask(login.password))
    print("api_key:", mask(login.api_key))
    print("api_secret_key:", mask(login.api_secret_key))
    print("phone_no:", login.phone_no)
    print("totp_key:", mask(login.totp_key))

    print("\n--- Reading Config ---")
    config = UserInterfaceConfig(args.key).get_data()
    print("candle_interval:", config.candle_interval)
    print("start_time:", config.start_time)
    print("end_time:", config.end_time)
    print("test_mode_status:", config.test_mode_status)
    print("test_mode_delta_days:", config.test_mode_delta_days)

    print("\nDone. Sanity-check: candle_interval should read as the numeric minutes "
          "value (e.g. 15), not the 'Test Mode' label -- that was the off-by-one bug "
          "that got fixed for this sheet's reader.")
