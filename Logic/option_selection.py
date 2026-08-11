# -*- coding: utf-8 -*-
"""
option_selection.py

Pure premium-matching logic shared between the live LogicVwapPiercingOptions
engine and offline testing scripts: given a list of candidate option
contracts and their quotes, pick the one whose premium is closest to the
target band (default ~100-110, per the readme's "option chain 100-110" note).
"""


def select_by_premium(lst_contracts, dict_quotes, target_low=100.0, target_high=110.0):
    """
    lst_contracts: list of (tradingsymbol, strike_price, option_type) tuples.
    dict_quotes: {tradingsymbol: quote_data} as returned by broker.get_quotes().
    Returns (best_symbol, best_price) or (None, 0.0) if nothing usable is found.
    """
    best_symbol = None
    best_diff = None
    best_price = 0.0
    for tsym, strike, opt in lst_contracts:
        if tsym not in dict_quotes:
            continue
        ltp = dict_quotes[tsym].ltp
        if ltp <= 0:
            continue
        if target_low <= ltp <= target_high:
            diff = 0.0
        else:
            diff = min(abs(ltp - target_low), abs(ltp - target_high))
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_symbol = tsym
            best_price = ltp
    return best_symbol, best_price


def select_cheapest_in_band(chain_df, option_type, band_low=80.0, band_high=130.0,
                            symbol_col="symbol", type_col="option_type", ltp_col="ltp"):
    """
    chain_df: a DataFrame as returned by a broker's getOptionChain() (Fyers-style) -- one row per
    contract, plus a non-option underlying/index row (option_type=="" there) that gets excluded
    by the option_type filter below. Unlike select_by_premium's "closest to a target band" rule,
    this only ever considers contracts strictly inside [band_low, band_high] and picks the
    lowest-premium (cheapest) one among them. Returns (symbol, price), or (None, 0.0) if the
    chain is empty/missing or nothing in the band qualifies.
    """
    if chain_df is None or len(chain_df) == 0:
        return None, 0.0
    if type_col not in chain_df.columns or ltp_col not in chain_df.columns or symbol_col not in chain_df.columns:
        return None, 0.0

    candidates = chain_df[(chain_df[type_col] == option_type) &
                          (chain_df[ltp_col] >= band_low) & (chain_df[ltp_col] <= band_high)]
    if candidates.empty:
        return None, 0.0

    best_row = candidates.loc[candidates[ltp_col].idxmin()]
    return str(best_row[symbol_col]), float(best_row[ltp_col])
