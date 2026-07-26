# Run: python -m BusinessLogic.vwappiercing_options.tests.test_search_scrip --key <path> [--symbol NIFTY28JUL26F]
# -*- coding: utf-8 -*-
"""
test_search_scrip.py

Diagnostic: fetchOHLC/get_quotes for anything other than the 3 hardcoded
index tokens (NIFTYBANK-INDEX/NIFTY50-INDEX/INDIAVIX-INDEX) silently returns
no data, because zebumynt_utitlity.get_updated_exchange_token() has no way
to resolve a tradingsymbol (e.g. a future/option symbol) to the numeric
scrip token this Noren-family API actually needs. This script calls
search_scrip() directly and prints the raw response so we can see the real
field names and fix token resolution properly.
"""
import argparse

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BrokerUtility.broker_platform.zebu.zebumynt_utility import zebumynt_utitlity
from Utility.nse_utility import nse_utitlity


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search for a scrip's token via the broker's search API")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Search text (default: this week's NIFTY future symbol)")
    parser.add_argument("--exchange", type=str, default="NFO", help="Exchange to search (default: NFO)")
    args = parser.parse_args()

    login = UserInterfaceLogin(args.key).get_data()
    broker = zebumynt_utitlity(user_name=login.user_id, client_id=login.api_key,
                               secret_id=login.api_secret_key, pin=login.password,
                               totp=login.totp_key, phone_no=login.phone_no)

    search_text = args.symbol
    if search_text is None:
        nse_utility = nse_utitlity()
        current_week_expiry, *_ = nse_utility.get_index_expiry_date("NIFTY")
        search_text = broker.get_future_name("NIFTY", current_week_expiry)

    print(f"\nSearching {args.exchange} for '{search_text}' (full symbol)...")
    result_full = broker.search_scrip(searchtext=search_text, exchange=args.exchange)
    print("Result type:", type(result_full))

    print(f"\nSearching {args.exchange} for 'NIFTY' (base name, broader match)...")
    result_base = broker.search_scrip(searchtext="NIFTY", exchange=args.exchange)
    print("Result type:", type(result_base))

    print("\nDone. Share this output -- the field names in the response (e.g. 'token'/'tsym'/'exch') "
         "are what get_updated_exchange_token needs to resolve symbols to tokens correctly.")
