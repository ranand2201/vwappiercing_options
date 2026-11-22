# -*- coding: utf-8 -*-
"""
config_loader.py

Loads Config/strategy_config.json and Config/order_config.json once and hands back a plain dict,
with hardcoded fallback defaults (DEFAULTS below) for every key either file omits. A missing or
partial config file can never crash the strategy or silently disable it -- it just runs with the
values that were previously hardcoded.

This is the single source of truth for every tunable trading parameter shared by LIVE and
BACKTEST (piercing/reclaim/entry timing and thresholds, exit formulas, the option premium band,
real order-placement settings). Pure implementation details that aren't meant to be tuned by
someone editing a config file (state name strings, internal helper constants) stay as plain code
constants and are NOT in here.
"""
import json
import os
from datetime import datetime

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG_PATH = os.path.join(_CONFIG_DIR, "strategy_config.json")
_DEFAULT_ORDER_CONFIG_PATH = os.path.join(_CONFIG_DIR, "order_config.json")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

DEFAULTS = {
    "day_start_time": "09:15:00",
    "piercing_start_delay_minutes": 20,
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
        "entry_order_type": "MARKET",
        "exit_order_type": "MARKET",
        "limit_buffer_percent": 1.05,
        "tick_size": 0.05,
        "product_type": "MIS",
        "lot_size": 65,  # NSE-fixed NIFTY lot size
        "lot_count": 1,
        "order_days": ["All"],
    },
}


def normalize_order_days(value):
    if isinstance(value, str):
        days = value.split(",")
    elif isinstance(value, list):
        days = value
    else:
        raise ValueError("order_days must be 'All', a weekday, or a list of weekdays")

    normalized_days = []
    for day in days:
        if not isinstance(day, str):
            raise ValueError("order_days entries must be weekday names or 'All'")
        normalized = day.strip().title()
        if normalized.lower() == "all":
            normalized = "All"
        elif normalized not in WEEKDAYS:
            raise ValueError("order_days entries must be Monday through Sunday or 'All'")
        if normalized not in normalized_days:
            normalized_days.append(normalized)

    if not normalized_days:
        raise ValueError("order_days must contain at least one weekday or 'All'")
    if "All" in normalized_days and len(normalized_days) > 1:
        raise ValueError("'All' cannot be combined with individual order_days")
    return normalized_days


def is_order_day(trade_date, order_days):
    normalized_days = normalize_order_days(order_days)
    if normalized_days == ["All"]:
        return True
    if isinstance(trade_date, str):
        trade_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
    return trade_date.strftime("%A") in normalized_days


def should_place_orders(live_trading_enabled, trade_date, order_days):
    return bool(live_trading_enabled) and is_order_day(trade_date, order_days)


def _merge_order_config(order_cfg, loaded_order_cfg):
    """Apply an order config, including support for the legacy shared order_type key."""
    if not isinstance(loaded_order_cfg, dict):
        raise ValueError("order settings must contain a JSON object")

    legacy_order_type = loaded_order_cfg.get("order_type")
    order_cfg.update({
        key: value for key, value in loaded_order_cfg.items()
        if key not in {"order_type", "place_order", "live_trading_enabled", "target_exit"}
        and not key.startswith("_comment_")
    })
    if legacy_order_type is not None:
        if "entry_order_type" not in loaded_order_cfg:
            order_cfg["entry_order_type"] = legacy_order_type
        if "exit_order_type" not in loaded_order_cfg:
            order_cfg["exit_order_type"] = legacy_order_type


def load_config(path=None, order_path=None):
    """
    Returns the strategy config dict with strategy_config.json and order_config.json overlaid on
    DEFAULTS. The standalone order config takes precedence over the legacy strategy_config.json
    "order" object.
    """
    cfg = dict(DEFAULTS)
    cfg["order"] = dict(DEFAULTS["order"])

    config_path = path or _DEFAULT_CONFIG_PATH
    if os.path.exists(config_path):
        with open(config_path) as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError(f"{config_path} must contain a JSON object")

        for key, value in loaded.items():
            if key == "order":
                _merge_order_config(cfg["order"], value)
            else:
                cfg[key] = value

    order_config_path = order_path or _DEFAULT_ORDER_CONFIG_PATH
    if os.path.exists(order_config_path):
        with open(order_config_path) as f:
            loaded_order_cfg = json.load(f)
        if not isinstance(loaded_order_cfg, dict):
            raise ValueError(f"{order_config_path} must contain a JSON object")

        if "place_order" in loaded_order_cfg:
            cfg["live_trading_enabled"] = loaded_order_cfg["place_order"]
        elif "live_trading_enabled" in loaded_order_cfg:
            cfg["live_trading_enabled"] = loaded_order_cfg["live_trading_enabled"]
        if "target_exit" in loaded_order_cfg:
            cfg["target_exit"] = loaded_order_cfg["target_exit"]
        _merge_order_config(cfg["order"], loaded_order_cfg)

    cfg["order"]["order_days"] = normalize_order_days(cfg["order"]["order_days"])
    return cfg
