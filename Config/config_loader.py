# -*- coding: utf-8 -*-
"""
config_loader.py

Loads Config/strategy_config.json once and hands back a plain dict, with hardcoded fallback
defaults (DEFAULTS below, one entry per key -- the same values that used to be hardcoded directly
in pattern_rules.py/piercing_engine.py) for every key the file omits, and for the file itself if
it's missing entirely. A missing or partial config file can never crash the strategy or silently
disable it -- it just runs with the values that were previously hardcoded.

This is the single source of truth for every tunable trading parameter shared by LIVE and
BACKTEST (piercing/reclaim/entry timing and thresholds, exit formulas, the option premium band,
real order-placement settings). Pure implementation details that aren't meant to be tuned by
someone editing a config file (state name strings, internal helper constants) stay as plain code
constants and are NOT in here.
"""
import json
import os

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG_PATH = os.path.join(_CONFIG_DIR, "strategy_config.json")

DEFAULTS = {
    "day_start_time": "09:15:00",
    "piercing_start_delay_minutes": 15,
    "piercing_cutoff_time": "14:00:00",
    "force_exit_time": "14:50:00",
    "piercing_min_vwap_gap": 6,
    "piercing_min_open_vwap_gap": 5,
    "entry_min_vwap_gap": 5,
    "reclaim_candle_interval_minutes": 5,
    "entry_candle_interval_minutes": 1,
    "bollinger_period": 20,
    "bollinger_std_dev": 2,
    "option_premium_band_low": 80.0,
    "option_premium_band_high": 130.0,
    "exit2_percent": 0.2,
    "exit3_percent": 0.75,
    "index_name": "NIFTY",
    "strike_step": 50,
    "test_mode_start_time": "09:16:15",
    "test_mode_step_seconds": 60,
    "historical_option_lookup_delay_seconds": 0.5,
    # safety: real orders are placed ONLY when this is explicitly true. Absent/false keeps LIVE
    # fully paper-trade, exactly as it behaved before real order placement existed.
    "live_trading_enabled": False,
    # which of Exit1-4 ALSO closes the position for real, alongside SL (SL is always real
    # regardless of this). None/null = SL is the only real exit (paper-trade-era behavior).
    "target_exit": None,
    "order": {
        "order_type": "MARKET",
        "product_type": "MIS",
        "lot_size": 65,  # NSE-fixed NIFTY lot size
        "lot_count": 1,
    },
}


def load_config(path=None):
    """
    Returns the strategy config dict: DEFAULTS with strategy_config.json's values overlaid on top.
    Missing keys, a missing "order" sub-key, or a missing file entirely all fall back to DEFAULTS.
    """
    cfg = dict(DEFAULTS)
    cfg["order"] = dict(DEFAULTS["order"])

    config_path = path or _DEFAULT_CONFIG_PATH
    if not os.path.exists(config_path):
        return cfg

    with open(config_path) as f:
        loaded = json.load(f)

    for key, value in loaded.items():
        if key == "order" and isinstance(value, dict):
            cfg["order"].update(value)
        else:
            cfg[key] = value
    return cfg
