# Run: python -m BusinessLogic.vwappiercing_options.tests.test_fyers_option_chain --key <path>
# -*- coding: utf-8 -*-
"""
test_fyers_option_chain.py

Diagnostic-only script: calls Fyers' real getOptionChain() and dumps the raw
response shape (columns, dtypes, a few sample rows) so we can see exactly
what a real Fyers option trading symbol looks like, and what fields are
available (strike, option type, ltp, etc.) before wiring the VWAP Piercing
Options strategy's entry/exit option-price logic around it. Makes no
trades, no orders -- read-only market data calls.
"""
import argparse

from BusinessLogic.vwappiercing_options.UserInterface.gsheet.login.login import UserInterfaceLogin
from BrokerUtility.pal.utility_manager import utility_manager


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dump Fyers getOptionChain() raw response shape")
    parser.add_argument("--key", type=str, required=True, help="Path to the service account json file")
    parser.add_argument("--symbol", type=str, default="NIFTY50-INDEX",
                        help="Underlying symbol to query the chain for (default: NIFTY50-INDEX)")
    args = parser.parse_args()

    login = UserInterfaceLogin(args.key).get_data()
    print("Broker from sheet:", login.broker)

    obj_utility_manager = utility_manager()
    broker = obj_utility_manager.get_utility_object(login).get_broker_utility()
    print("Resolved broker utility class:", type(broker).__name__)

    if not hasattr(broker, "getOptionChain"):
        print(f"{type(broker).__name__} has no getOptionChain() -- this script only works against Fyers.")
        raise SystemExit(1)

    print(f"\nCalling getOptionChain({args.symbol!r}) ...")
    chain_df, call_oi, put_oi = broker.getOptionChain(args.symbol)

    print("\n--- Raw shape ---")
    print("rows:", len(chain_df), " call_oi:", call_oi, " put_oi:", put_oi)
    print("columns:", list(chain_df.columns))
    print("\ndtypes:\n", chain_df.dtypes)

    print("\n--- First 10 rows (all columns) ---")
    with __import__("pandas").option_context("display.max_columns", None, "display.width", 200):
        print(chain_df.head(10))

    # try to spot the option-type / symbol / premium columns under a few likely names, so we can
    # confirm real symbol format and which column holds the tradable premium.
    candidate_type_cols = [c for c in chain_df.columns if "type" in c.lower()]
    candidate_symbol_cols = [c for c in chain_df.columns if "symbol" in c.lower()]
    candidate_ltp_cols = [c for c in chain_df.columns if "ltp" in c.lower() or c.lower() == "lp"]
    candidate_strike_cols = [c for c in chain_df.columns if "strike" in c.lower()]

    print("\n--- Guessed column roles ---")
    print("option-type column candidates:", candidate_type_cols)
    print("symbol column candidates:", candidate_symbol_cols)
    print("ltp/premium column candidates:", candidate_ltp_cols)
    print("strike column candidates:", candidate_strike_cols)

    if candidate_symbol_cols:
        sym_col = candidate_symbol_cols[0]
        print(f"\nSample real symbols from '{sym_col}':")
        for s in chain_df[sym_col].head(10).tolist():
            print(" ", s)

    if candidate_type_cols:
        print(f"\nDistinct values in '{candidate_type_cols[0]}':", chain_df[candidate_type_cols[0]].unique().tolist())

    print("\nDone. Paste this output back so we can wire the real column names into option_selection.py.")
