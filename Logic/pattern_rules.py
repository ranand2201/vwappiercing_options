# -*- coding: utf-8 -*-
"""
pattern_rules.py

Pure, stateless VWAP-piercing pattern predicates shared between the live
LogicVwapPiercingOptions state machine and offline testing/dry-run scripts,
so a dry run against historical candles reflects exactly what the live
engine would decide.
"""
from datetime import datetime, timedelta

from DataTypes.defines import *

# New Piercing candidates are only looked for once VWAP has had time to settle (15 min after
# execution start) and stop being looked for after 14:30 -- an in-progress setup or open trade
# at that point still runs to its natural conclusion (SL or EOD), only new detection is cut off.
PIERCING_START_DELAY_MINUTES = 15
PIERCING_CUTOFF_TIME = "14:30:00"

# Any trade still open (SL not yet hit) at this time is force-closed at the prevailing price,
# regardless of SL/Exit-1..4 state. Pending (not-yet-entered) setups are unaffected -- this only
# closes an already-open position.
FORCE_EXIT_TIME = "14:50:00"

# A piercing candle only counts if its Close has moved at least this far past VWAP -- filters out
# marginal pierces that are really just noise around the VWAP line.
PIERCING_MIN_VWAP_GAP = 6

# ...and the candle's Open must already be at least this far from VWAP on the piercing side, so
# the candle is genuinely piercing through rather than opening right on top of VWAP.
PIERCING_MIN_OPEN_VWAP_GAP = 5

# During SEEK_CONFIRM_ENTRY, entry only fires once price has cleared back through VWAP by at
# least this much -- a bare crossing right on the VWAP line is treated as noise, not a real entry.
ENTRY_MIN_VWAP_GAP = 5


def compute_piercing_start_time(execution_start_time):
    """execution_start_time: 'HH:MM:SS'. Returns 'HH:MM:SS', PIERCING_START_DELAY_MINUTES after it."""
    return (datetime.strptime(execution_start_time, "%H:%M:%S")
            + timedelta(minutes=PIERCING_START_DELAY_MINUTES)).strftime("%H:%M:%S")


def is_piercing_window_open(check_time, piercing_start_time):
    """check_time/piercing_start_time as zero-padded 'HH:MM:SS' strings (safe to compare lexically)."""
    return piercing_start_time <= check_time < PIERCING_CUTOFF_TIME


def is_force_exit_time_reached(check_time):
    """check_time as a zero-padded 'HH:MM:SS' string (safe to compare lexically)."""
    return check_time >= FORCE_EXIT_TIME


def time_of_day(date_time_str):
    """Extracts 'HH:MM:SS' from a full 'DD-MM-YYYY HH:MM:SS' (or similar) timestamp string."""
    return date_time_str.split(" ")[-1]


def piercing_direction(row):
    """
    Returns 'BUY', 'SELL', or None for a candle row (needs OPEN/CLOSE/VWAP). Only counts as a
    piercing if the Open starts at least PIERCING_MIN_OPEN_VWAP_GAP away from VWAP on the piercing
    side, and the Close has cleared VWAP on the other side by at least PIERCING_MIN_VWAP_GAP --
    a marginal open or close right on the VWAP line is treated as noise, not a real piercing.
    """
    if row[OPEN_PRICE] < row[VWAP] - PIERCING_MIN_OPEN_VWAP_GAP and row[CLOSE_PRICE] > row[VWAP] + PIERCING_MIN_VWAP_GAP:
        return "BUY"
    if row[OPEN_PRICE] > row[VWAP] + PIERCING_MIN_OPEN_VWAP_GAP and row[CLOSE_PRICE] < row[VWAP] - PIERCING_MIN_VWAP_GAP:
        return "SELL"
    return None


def is_reclaimed(row, vwap, direction):
    """True if this candle's close lands back on the opposite side of VWAP from the piercing close."""
    return (row[CLOSE_PRICE] < vwap) if direction == "BUY" else (row[CLOSE_PRICE] > vwap)


def is_vwap_reentry_triggered(check_price, vwap, direction):
    """
    The entry trigger: once Reclaim is confirmed, entry fires when price crosses back through
    VWAP in the original piercing direction by at least ENTRY_MIN_VWAP_GAP (BUY: back above VWAP;
    SELL: back below VWAP). check_price is a candle's Close for the backtest/dry-run
    (candle-driven), or the live LTP for the tick-driven live engine -- same predicate either way.
    """
    return (check_price > vwap + ENTRY_MIN_VWAP_GAP) if direction == "BUY" \
        else (check_price < vwap - ENTRY_MIN_VWAP_GAP)
