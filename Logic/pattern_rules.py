# -*- coding: utf-8 -*-
"""
pattern_rules.py

Pure, stateless VWAP-piercing pattern predicates shared between the live
LogicVwapPiercingOptions state machine and offline testing/dry-run scripts,
so a dry run against historical candles reflects exactly what the live
engine would decide.
"""
import calendar
from datetime import datetime, timedelta

from DataTypes.defines import *
from Utility.utility import generate_monthly_expiry_dates

# New Piercing candidates are only looked for once VWAP has had time to settle (15 min after
# execution start) and stop being looked for at PIERCING_CUTOFF_TIME. A setup already in progress
# (pierced but not yet entered) at that point is abandoned too -- see is_setup_abandon_time_reached
# -- only an already-open trade (IN_TRADE) runs past this point, until FORCE_EXIT_TIME.
PIERCING_START_DELAY_MINUTES = 15
PIERCING_CUTOFF_TIME = "14:00:00"

# Any trade still open (SL not yet hit) at this time is force-closed at the prevailing price,
# regardless of SL/Exit-1..4 state. This is independent of PIERCING_CUTOFF_TIME/setup-abandonment
# above -- it only ever applies to a trade that has actually entered (IN_TRADE).
FORCE_EXIT_TIME = "14:50:00"

# A piercing candle only counts if its Close has moved at least this far past VWAP -- filters out
# marginal pierces that are really just noise around the VWAP line.
PIERCING_MIN_VWAP_GAP = 6

# ...and the candle's Open must already be at least this far from VWAP on the piercing side, so
# the candle is genuinely piercing through rather than opening right on top of VWAP.
PIERCING_MIN_OPEN_VWAP_GAP = 5

# During SEEK_CONFIRM_ENTRY, entry only fires once the candle has closed back through VWAP by at
# least this much -- a bare crossing right on the VWAP line is treated as noise, not a real entry.
ENTRY_MIN_VWAP_GAP = 5

# The two legs after the Piercing candle each run on their own fixed timeframe, independent of the
# configurable main/piercing candle interval:
#   Reclaim  -- 5-min candles. A reclaim is a genuine rejection back across VWAP, so it needs a
#               slow enough candle to mean something; a 1-min close back across the line is mostly
#               noise and fires far too readily.
#   Entry    -- 1-min candles. Once the reclaim has printed, the trigger wants the fastest
#               confirmation available so entry isn't delayed waiting for a slower candle to close.
RECLAIM_CANDLE_INTERVAL_MINUTES = 5
ENTRY_CANDLE_INTERVAL_MINUTES = 1


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


def is_setup_abandon_time_reached(check_time):
    """
    check_time as a zero-padded 'HH:MM:SS' string. True once PIERCING_CUTOFF_TIME is reached --
    a setup that has pierced but not yet entered (SEEK_RECLAIM / SEEK_CONFIRM_ENTRY) at this point
    is given up on and reset, same cutoff as new-piercing detection. Does NOT apply to an
    already-open trade (IN_TRADE) -- that's FORCE_EXIT_TIME's job, independently.
    """
    return check_time >= PIERCING_CUTOFF_TIME


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
    body = abs(float(row[CLOSE_PRICE]) - float(row[OPEN_PRICE]))
    top_wick = float(row[HIGH_PRICE]) - max(float(row[OPEN_PRICE]), float(row[CLOSE_PRICE]))
    if body <= top_wick:
        return None
    if row[OPEN_PRICE] < row[VWAP] - PIERCING_MIN_OPEN_VWAP_GAP and row[CLOSE_PRICE] > row[VWAP] + PIERCING_MIN_VWAP_GAP:
        return "BUY"
    if row[OPEN_PRICE] > row[VWAP] + PIERCING_MIN_OPEN_VWAP_GAP and row[CLOSE_PRICE] < row[VWAP] - PIERCING_MIN_VWAP_GAP:
        return "SELL"
    return None


def is_reclaimed(row, vwap, direction):
    """
    True if this RECLAIM_CANDLE_INTERVAL_MINUTES candle's close lands back on the opposite side of
    VWAP from the piercing close -- BUY: closes below VWAP; SELL: closes above VWAP. No minimum
    gap here (unlike the piercing and entry legs): the reclaim only has to get back across the
    line, not clear it by a margin.
    """
    return (row[CLOSE_PRICE] < vwap) if direction == "BUY" else (row[CLOSE_PRICE] > vwap)


def is_vwap_reentry_triggered(check_price, vwap, direction):
    """
    The entry trigger: once Reclaim is confirmed, entry fires when an
    ENTRY_CANDLE_INTERVAL_MINUTES candle closes back through VWAP in the original piercing
    direction by at least ENTRY_MIN_VWAP_GAP (BUY: closes above VWAP; SELL: closes below VWAP).
    check_price is that 1-min candle's Close in both modes.
    """
    return (check_price > vwap + ENTRY_MIN_VWAP_GAP) if direction == "BUY" \
        else (check_price < vwap - ENTRY_MIN_VWAP_GAP)


def candle_close_time(candle_ts, interval_minutes):
    """
    A candle's close time, as a datetime, from its start timestamp ('YYYY-MM-DD HH:MM:SS') and its
    interval. Broker candle timestamps label the candle's START (confirmed against Fyers' history
    API, whose epoch stamps are the bar open), so a 5-min candle stamped 10:35 has not actually
    closed -- and so isn't knowable -- until 10:40.
    """
    return datetime.strptime(candle_ts, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=interval_minutes)


def has_closed_at_or_after(candle_ts, interval_minutes, reference_ts, reference_interval_minutes):
    """
    True if the candle starting at candle_ts closes no earlier than the reference candle does,
    i.e. it isn't knowable before the reference candle is. Keeps the pattern's legs in real
    chronological order now that they run on three different timeframes: a 5-min Reclaim candle
    must not be one that was still forming inside the Piercing candle, and a 1-min entry candle
    must not be one that closed before the Reclaim candle confirmed. A tie (e.g. the 10:39 1-min
    candle and the 10:35 5-min candle, both closing at 10:40) is allowed -- both are known at the
    same instant, so acting on the pair right then is legitimate in live and backtest alike.
    """
    return candle_close_time(candle_ts, interval_minutes) >= \
        candle_close_time(reference_ts, reference_interval_minutes)


def _is_in_last_week_of_month(trade_date):
    """True if trade_date falls within the last 7 calendar days of its month."""
    last_day = calendar.monthrange(trade_date.year, trade_date.month)[1]
    return trade_date.day > last_day - 7


def resolve_front_month_future_symbol(broker, index_name, trade_date_str):
    """
    The monthly future contract that was front-month on trade_date_str (no network call).
    NIFTY here trades MONTHLY futures (confirmed via search_scrip -- only ~1 contract/month is
    ever listed, e.g. NIFTY28JUL26F / NIFTY25AUG26F / NIFTY29SEP26F), not weekly, despite the
    "F" suffix looking similar to a weekly naming convention. generate_monthly_expiry_dates()
    returns the last <p_expiry_day> weekday of each month from trade_date's month onward, but
    doesn't itself account for trade_date possibly falling after that month's own expiry -- so
    the first entry isn't always >= trade_date. Pick the first one that actually is.

    During the last calendar week of the month, rolls to next month's contract early instead of
    the current month's -- liquidity in the current month's contract thins out sharply in its
    final week as the market rolls over, so testing/trading against it that late isn't
    representative of what would actually be tradable. Used identically by the live engine
    (against today's date) and the backtest engine (against each historical trade date).
    """
    trade_date = datetime.strptime(trade_date_str, "%Y-%m-%d")

    lookup_date = trade_date
    if _is_in_last_week_of_month(trade_date):
        lookup_date = (trade_date.replace(day=28) + timedelta(days=4)).replace(day=1)

    for expiry_str in generate_monthly_expiry_dates(lookup_date, 1):
        if datetime.strptime(expiry_str, "%d-%b-%Y") >= lookup_date:
            return broker.get_future_name(index_name, expiry_str), expiry_str
    # shouldn't happen within the same calendar year, but fall back to the last one generated
    last_expiry = generate_monthly_expiry_dates(lookup_date, 1)[-1]
    return broker.get_future_name(index_name, last_expiry), last_expiry
