# Run: python -m BusinessLogic.vwappiercing_options.tests.test_option_chain_quotes --key <path> [--strike 24600]
# -*- coding: utf-8 -*-
"""
test_option_chain_quotes.py

Exercises the real get_option_chain() + get_quotes() calls and the
premium-nearest-to-100-110 selection logic in isolation, using the previous
trading day's close as a stand-in reference price (quotes outside market
hours typically still return the last traded price). This is the piece
flagged as least verified -- the exact response shape of the broker's
option-chain API couldn't be checked without a live session.
"""
import argparse

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BusinessLogic.vwappiercing_options.Logic.option_selection import select_by_premium
from BrokerUtility.broker_platform.zebu.zebumynt_utility import zebumynt_utitlity
from Utility.nse_utility import nse_utitlity
from DataTypes.trade_data import get_quote_request_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test get_option_chain + get_quotes + premium selection")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    parser.add_argument("--strike", type=int, default=None,
                        help="Center strike to query around (default: fetched via a future quote)")
    args = parser.parse_args()

    login = UserInterfaceLogin(args.key).get_data()
    broker = zebumynt_utitlity(user_name=login.user_id, client_id=login.api_key,
                               secret_id=login.api_secret_key, pin=login.password,
                               totp=login.totp_key, phone_no=login.phone_no)

    nse_utility = nse_utitlity()
    current_week_expiry, next_week_expiry, monthly_expiry, is_expiry_day, \
        is_current_week_monthly_expiry, is_next_week_monthly_expiry = \
        nse_utility.get_index_expiry_date("NIFTY")
    future_symbol = broker.get_future_name("NIFTY", current_week_expiry)
    print("Future symbol:", future_symbol)

    atm_strike = args.strike
    if atm_strike is None:
        # market_type="" would make get_quotes parse this as an OPTION symbol; a future needs
        # any other non-empty value so it's used as-is and resolved to NFO.
        dict_quotes = broker.get_quotes([get_quote_request_data(p_symbol=future_symbol, p_market_type="FUT")])
        if future_symbol not in dict_quotes or dict_quotes[future_symbol].ltp <= 0:
            print("Could not fetch a future quote to derive ATM strike -- pass --strike explicitly.")
            raise SystemExit(1)
        future_ltp = dict_quotes[future_symbol].ltp
        atm_strike = round(future_ltp / 50) * 50
        print("Future LTP:", future_ltp, "-> ATM strike:", atm_strike)
    else:
        print("Using provided strike:", atm_strike)

    for option_type in ("CE", "PE"):
        print(f"\n--- {option_type} chain around {atm_strike} (expiry {current_week_expiry}) ---")
        lst_contracts = broker.get_option_chain("NIFTY", atm_strike, option_type, current_week_expiry, p_count=40)
        print("Contracts returned:", len(lst_contracts))
        for tsym, strike, opt in lst_contracts[:20]:
            print(" ", tsym, strike, opt)

        if not lst_contracts:
            print("get_option_chain returned nothing -- check the instname/symname/optt/exd filters "
                 "in zebumynt_utitlity.get_option_chain against a fresh search_scrip response.")
            continue

        # market_type="" would send these through __get_option_name's mismatched re-parse; these
        # tradingsymbols are already correct, so use the pass-through sentinel instead.
        lst_req = [get_quote_request_data(p_symbol=tsym, p_market_type="OPT") for tsym, strike, opt in lst_contracts]
        dict_quotes = broker.get_quotes(lst_req)
        print("Quotes fetched for:", len(dict_quotes), "of", len(lst_contracts), "contracts")
        for tsym, quote in dict_quotes.items():
            print(" ", tsym, "ltp=", quote.ltp)

        best_symbol, best_price = select_by_premium(lst_contracts, dict_quotes)
        print(f"Selected {option_type}:", best_symbol, "@", best_price,
             "(target band 100-110)")
