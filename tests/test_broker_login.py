# Run: python -m BusinessLogic.vwappiercing_options.tests.test_broker_login --key <path_to_service_account_json>
# -*- coding: utf-8 -*-
"""
test_broker_login.py

Reads BrokerData from the sheet and logs into Zebu Mynt directly (no market
data call). Broker auth usually works outside market hours too, so this
catches credential/TOTP issues before market open.
"""
import argparse

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BrokerUtility.broker_platform.zebu.zebumynt_utility import zebumynt_utitlity


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Zebu Mynt broker login using the sheet's BrokerData")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    args = parser.parse_args()

    login = UserInterfaceLogin(args.key).get_data()
    print("Logging in as:", login.user_id, "broker:", login.broker)

    broker = zebumynt_utitlity(user_name=login.user_id,
                               client_id=login.api_key,
                               secret_id=login.api_secret_key,
                               pin=login.password,
                               totp=login.totp_key,
                               phone_no=login.phone_no)

    print("get_running_status():", broker.get_running_status())
    print("Done. running_status True means the session token was issued successfully.")
