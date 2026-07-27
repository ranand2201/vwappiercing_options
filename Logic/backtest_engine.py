# -*- coding: utf-8 -*-
"""
backtest_engine.py

Historical replay of the Piercing -> Reclaim -> Confirm -> SL/Exit pattern
for a single past trading day, using the same pattern_rules predicates the
live engine uses -- including the piercing-window gating (start+15min to
14:30), the 14:50 force-exit cutoff, and SL-triggers-next-trade behavior (no
one-trade/day cap). This is the single implementation of the full day-replay
state machine: both run_backtest.py (writes to BackTestData) and
test_pattern_dry_run.py (prints a verbose trace) call this rather than
keeping their own copies, so they can't drift from what the live engine
actually does.

BUY and SELL setups are tracked as two fully independent state machines (see
_TradeState), mirroring the live engine, so a piercing in one direction is
never blocked or lost while the other direction already has a setup/trade in
progress.

Entry fires once Reclaim is confirmed and price crosses back through VWAP in
the piercing direction (BUY: back above VWAP; SELL: back below VWAP) --
approximated from candle Close vs VWAP since there are no intrabar LTP ticks
available historically. SL is the ONLY real exit -- Exit-1..4 (including
Bollinger) are all parallel hypotheses tracked via candle High/Low crossing
their levels, for comparison only; breaching one is logged but doesn't close
the trade. Any trade still open at 14:50 is force-closed at that candle's
Close, regardless of SL/Exit-1..4 state.

Rows are built as paper_trade_row (same dataclass the live engine writes to
PaperTradeData) since BackTestData now uses an identical sheet layout --
option_name/option_price fields are just left blank/0.0 here since there's
no real historical option premium data to fill them with. exit5_eod is
populated with the exit price for any trade closed by the 14:50 force-exit
(or the day's last close, as a fallback, for the rare case a trade is still
open past that), mirroring the live engine's EOD finalize.
"""
from datetime import datetime

from DataTypes.defines import *
from Utility.utility import compute_vwap, compute_bollinger_bands, get_target_price_by_percentage, generate_monthly_expiry_dates
from ..DataTypes.paper_trade_data import paper_trade_row, candle_snapshot, exit_hit
from . import pattern_rules

STATE_SEEK_PIERCING = "SEEK_PIERCING"
STATE_SEEK_RECLAIM = "SEEK_RECLAIM"
STATE_SEEK_CONFIRM_ENTRY = "SEEK_CONFIRM_ENTRY"
STATE_IN_TRADE = "IN_TRADE"

STATUS_NO_DATA = "no_data"
STATUS_OK = "ok"

DAY_START_TIME = "09:15:00"
BOLLINGER_PERIOD = 20
BOLLINGER_STD_DEV = 2


class _TradeState:
    """All the mutable pattern/trade-tracking state for one direction (BUY or SELL)."""

    def __init__(self, direction):
        self.direction = direction
        self.state = STATE_SEEK_PIERCING
        self.piercing_row = None
        self.reclaim_row = None
        self.current_trade = None
        self.sl_level = 0.0
        self.exit1_level = 0.0
        self.exit2_level = 0.0
        self.exit3_level = 0.0
        self.mae = 0.0
        self.mfe = 0.0
        self.mae_time = ""
        self.mfe_time = ""


def resolve_front_month_future_symbol(broker, index_name, trade_date_str):
    """
    The monthly future contract that was front-month on trade_date_str (no network call).
    NIFTY here trades MONTHLY futures (confirmed via search_scrip -- only ~1 contract/month is
    ever listed, e.g. NIFTY28JUL26F / NIFTY25AUG26F / NIFTY29SEP26F), not weekly, despite the
    "F" suffix looking similar to a weekly naming convention. generate_monthly_expiry_dates()
    returns the last <p_expiry_day> weekday of each month from trade_date's month onward, but
    doesn't itself account for trade_date possibly falling after that month's own expiry -- so
    the first entry isn't always >= trade_date. Pick the first one that actually is.
    """
    trade_date = datetime.strptime(trade_date_str, "%Y-%m-%d")
    for expiry_str in generate_monthly_expiry_dates(trade_date, 1):
        if datetime.strptime(expiry_str, "%d-%b-%Y") >= trade_date:
            return broker.get_future_name(index_name, expiry_str), expiry_str
    # shouldn't happen within the same calendar year, but fall back to the last one generated
    last_expiry = generate_monthly_expiry_dates(trade_date, 1)[-1]
    return broker.get_future_name(index_name, last_expiry), last_expiry


def run_backtest_for_day(broker, index_name, trade_date_str, candle_interval_minutes, log_fn=None):
    """
    Returns (list_of_paper_trade_row, future_symbol, status) for one trading day.
    log_fn, if given, is called with a string for each pattern event (Piercing/Reclaim/
    invalidation/Confirm-Entry/SL-close) -- pass print for a verbose dry-run trace.
    """
    log = log_fn or (lambda msg: None)

    future_symbol, _ = resolve_front_month_future_symbol(broker, index_name, trade_date_str)

    str_from_date = f"{trade_date_str} {DAY_START_TIME}"
    str_to_date = f"{trade_date_str} 15:30:00"
    candle_data = broker.fetchOHLC(future_symbol, str_from_date, str_to_date,
                                   interval=f"{candle_interval_minutes}minute",
                                   all_data=True, market_type="FUT")
    if candle_data is None or len(candle_data) == 0:
        return [], future_symbol, STATUS_NO_DATA

    # Zebu's "intvwap" field is a per-candle (interval) VWAP, not a cumulative session VWAP from
    # day open -- always compute the real cumulative VWAP ourselves instead of trusting it.
    candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]

    # Bollinger bands are rolling (causal, only look back), so precomputing over the whole day
    # upfront and indexing by row is equivalent to recomputing fresh at each candle -- no
    # lookahead bias.
    upper_band, middle_band, lower_band = compute_bollinger_bands(candle_data, period=BOLLINGER_PERIOD,
                                                                   std_dev=BOLLINGER_STD_DEV)

    piercing_start_time = pattern_rules.compute_piercing_start_time(DAY_START_TIME)

    states = {"BUY": _TradeState("BUY"), "SELL": _TradeState("SELL")}
    results = []

    for i in range(len(candle_data)):
        row = candle_data.iloc[i]
        ts = str(row[DATE_TIME])

        for direction, ds in states.items():
            if ds.state == STATE_SEEK_PIERCING:
                if not pattern_rules.is_piercing_window_open(pattern_rules.time_of_day(ts), piercing_start_time):
                    continue
                if pattern_rules.piercing_direction(row) == direction:
                    ds.piercing_row = row
                    ds.state = STATE_SEEK_RECLAIM
                    log(f"[{ts}] ({direction}) PIERCING {_fmt_candle(row)}")
                else:
                    log(f"[{ts}] ({direction}) seeking piercing {_fmt_candle(row)}")

            elif ds.state == STATE_SEEK_RECLAIM:
                if pattern_rules.is_reclaimed(row, direction):
                    ds.reclaim_row = row
                    ds.state = STATE_SEEK_CONFIRM_ENTRY
                    log(f"[{ts}] ({direction}) RECLAIM {_fmt_candle(row)}")
                else:
                    # Not reclaimed yet -- keep waiting on subsequent candles rather than
                    # abandoning after just one miss. The piercing candle stays the reference point.
                    log(f"[{ts}] ({direction}) no reclaim yet, still waiting {_fmt_candle(row)}")

            elif ds.state == STATE_SEEK_CONFIRM_ENTRY:
                # entry fires when this candle's Close crosses back through VWAP in the piercing
                # direction (BUY: back above VWAP; SELL: back below VWAP) -- no invalidation here,
                # the setup just keeps waiting, candle after candle, until this happens or EOD.
                triggered = pattern_rules.is_vwap_reentry_triggered(row[CLOSE_PRICE], row[VWAP], direction)
                log(f"[{ts}] ({direction}) waiting for entry: close={row[CLOSE_PRICE]} vs VWAP={row[VWAP]:.2f} "
                   f"{_fmt_candle(row)} {'-> TRIGGERED' if triggered else ''}")
                if not triggered:
                    continue

                entry_price = float(row[CLOSE_PRICE])
                trade = paper_trade_row()
                trade.date = trade_date_str
                trade.future = future_symbol
                trade.option_name = ""
                trade.trade_type = direction
                trade.piercing_candle = _to_snapshot(ds.piercing_row)
                trade.reclaim_candle = _to_snapshot(ds.reclaim_row)
                trade.confirm_candle = _to_snapshot(row)
                trade.entry_future_price = entry_price
                trade.entry_option_price = 0.0
                trade.entry_timestamp = ts

                piercing_high = float(ds.piercing_row[HIGH_PRICE])
                piercing_low = float(ds.piercing_row[LOW_PRICE])
                piercing_length = piercing_high - piercing_low

                if direction == "BUY":
                    ds.sl_level = piercing_low
                    ds.exit1_level = entry_price + piercing_length
                    ds.exit2_level = get_target_price_by_percentage(entry_price, 0.5, "buy")
                    ds.exit3_level = get_target_price_by_percentage(entry_price, 0.75, "buy")
                else:
                    ds.sl_level = piercing_high
                    ds.exit1_level = entry_price - piercing_length
                    ds.exit2_level = get_target_price_by_percentage(entry_price, 0.5, "sell")
                    ds.exit3_level = get_target_price_by_percentage(entry_price, 0.75, "sell")

                ds.mae, ds.mfe = 0.0, 0.0
                ds.mae_time = ds.mfe_time = ts
                ds.current_trade = trade
                ds.state = STATE_IN_TRADE

                # Exit-4 (Bollinger) isn't fixed at entry like SL/Exit-1..3 -- it moves every
                # candle (checked live in the STATE_IN_TRADE branch below). Shown here is just
                # its value at the moment of entry, for visibility.
                entry_bollinger_level = upper_band.iloc[i] if direction == "BUY" else lower_band.iloc[i]
                exit4_str = f"{entry_bollinger_level:.2f}" if entry_bollinger_level == entry_bollinger_level else "n/a"

                log(f"[{ts}] ({direction}) CONFIRM/ENTRY @ {entry_price} "
                   f"(piercing: {ds.piercing_row[DATE_TIME]}, reclaim: {ds.reclaim_row[DATE_TIME]}) "
                   f"SL={ds.sl_level} Exit1={ds.exit1_level:.2f} Exit2={ds.exit2_level:.2f} Exit3={ds.exit3_level:.2f} "
                   f"Exit4(@entry)={exit4_str}")

            elif ds.state == STATE_IN_TRADE:
                trade = ds.current_trade
                high = float(row[HIGH_PRICE])
                low = float(row[LOW_PRICE])
                worst = (low - trade.entry_future_price) if direction == "BUY" else (trade.entry_future_price - high)
                best = (high - trade.entry_future_price) if direction == "BUY" else (trade.entry_future_price - low)
                if worst < ds.mae:
                    ds.mae, ds.mae_time = worst, ts
                if best > ds.mfe:
                    ds.mfe, ds.mfe_time = best, ts

                # any trade still open at 14:50 is force-closed at this candle's Close, regardless
                # of SL/Exit-1..4 state.
                if pattern_rules.is_force_exit_time_reached(pattern_rules.time_of_day(ts)):
                    close_price = float(row[CLOSE_PRICE])
                    trade.exit5_eod.future_price = close_price
                    trade.exit5_eod.option_price = 0.0
                    trade.mae, trade.mae_time = ds.mae, ds.mae_time
                    trade.mfe, trade.mfe_time = ds.mfe, ds.mfe_time
                    results.append(trade)
                    log(f"[{ts}] ({direction}) force-exit (14:50 cutoff) @ {close_price} -- trade closed, resuming scan")
                    ds.current_trade = None
                    ds.piercing_row = None
                    ds.reclaim_row = None
                    ds.state = STATE_SEEK_PIERCING
                    continue

                # capture before-state so we can log the exact candle each hypothesis first hits
                # -- none of Exit-1..4 stop the trade (only SL does), so without this the trace
                # gives no visibility into when/whether they fired.
                was_hit = {
                    "Exit1 (Length of Piercing)": trade.exit1_hit.is_hit,
                    "Exit2 (0.5%)": trade.exit2_hit.is_hit,
                    "Exit3 (0.75%)": trade.exit3_hit.is_hit,
                    "Exit4 (Bollinger)": trade.exit4_hit.is_hit,
                }

                _mark_level_if_hit(trade.sl_hit, ds.sl_level, row, direction, ts, is_stop=True)
                _mark_level_if_hit(trade.exit1_hit, ds.exit1_level, row, direction, ts)
                _mark_level_if_hit(trade.exit2_hit, ds.exit2_level, row, direction, ts)
                _mark_level_if_hit(trade.exit3_hit, ds.exit3_level, row, direction, ts)

                # Exit-4 target: Bollinger upper band for BUY, lower band for SELL -- price
                # reaching the band in the trade's favor, same target-style semantics as
                # Exit-1..3 -- but it's a hypothesis only, same as Exit-1..3: breaching it is
                # logged, not a real exit.
                bollinger_level = upper_band.iloc[i] if direction == "BUY" else lower_band.iloc[i]
                bollinger_str = f"{bollinger_level:.2f}" if bollinger_level == bollinger_level else "n/a"  # NaN check
                if bollinger_level == bollinger_level:  # (first BOLLINGER_PERIOD candles have no band yet)
                    _mark_level_if_hit(trade.exit4_hit, float(bollinger_level), row, direction, ts)

                for label, hit_obj in (("Exit1 (Length of Piercing)", trade.exit1_hit),
                                       ("Exit2 (0.5%)", trade.exit2_hit),
                                       ("Exit3 (0.75%)", trade.exit3_hit),
                                       ("Exit4 (Bollinger)", trade.exit4_hit)):
                    if not was_hit[label] and hit_obj.is_hit:
                        log(f"[{ts}] ({direction}) {label} target BREACHED @ {hit_obj.future_price} "
                           f"(hypothesis only -- trade continues, only SL closes it)")

                log(f"[{ts}] ({direction}) in-trade {_fmt_candle(row)} SL={ds.sl_level} Exit4(Bollinger)={bollinger_str}")

                if trade.sl_hit.is_hit:
                    trade.mae, trade.mae_time = ds.mae, ds.mae_time
                    trade.mfe, trade.mfe_time = ds.mfe, ds.mfe_time
                    results.append(trade)
                    log(f"[{ts}] ({direction}) SL hit @ {ds.sl_level} -- trade closed, resuming scan")
                    ds.current_trade = None
                    ds.piercing_row = None
                    ds.reclaim_row = None
                    ds.state = STATE_SEEK_PIERCING

    # end of day: any direction still IN_TRADE (SL never hit) gets its exit5_eod stamped with the
    # day's last close, mirroring the live engine's EOD finalize.
    for direction, ds in states.items():
        if ds.state == STATE_IN_TRADE and ds.current_trade is not None:
            trade = ds.current_trade
            last_close = float(candle_data.iloc[-1][CLOSE_PRICE])
            trade.exit5_eod.future_price = last_close
            trade.exit5_eod.option_price = 0.0
            trade.mae, trade.mae_time = ds.mae, ds.mae_time
            trade.mfe, trade.mfe_time = ds.mfe, ds.mfe_time
            results.append(trade)
            log(f"End of day ({direction}): trade still open (SL not hit), closed at last price {last_close}")

    return results, future_symbol, STATUS_OK


def _exit_pnl_points(trade: paper_trade_row, hit_obj: exit_hit):
    """Signed profit/loss in future-price points for a hit exit, relative to entry."""
    if trade.trade_type == "BUY":
        return hit_obj.future_price - trade.entry_future_price
    return trade.entry_future_price - hit_obj.future_price


def determine_best_case_exit(trade: paper_trade_row):
    """
    Which of Exit-1..4 would have been the best-case exit for a finalized trade, i.e. whichever
    hit target represents the largest profit in points. Returns "SL" if none of Exit-1..4 were
    ever hit before the trade closed. BackTestData-only -- not written to PaperTradeData.
    """
    candidates = [(label, _exit_pnl_points(trade, hit_obj))
                 for label, hit_obj in (("Exit1", trade.exit1_hit), ("Exit2", trade.exit2_hit),
                                        ("Exit3", trade.exit3_hit), ("Exit4", trade.exit4_hit))
                 if hit_obj.is_hit]
    if not candidates:
        return "SL"
    best_label, _ = max(candidates, key=lambda c: c[1])
    return best_label


def describe_exit_outcomes(trade: paper_trade_row):
    """
    Full description for the Best Case Exit column: profit/loss in points for every Exit-1..4
    that was hit, plus which one was best. e.g. "Exit1:+23.90, Exit3:+65.20 (Best: Exit3)".
    Returns "SL" if none of Exit-1..4 were ever hit before the trade closed.
    """
    parts = []
    best_label, best_pnl = None, None
    for label, hit_obj in (("Exit1", trade.exit1_hit), ("Exit2", trade.exit2_hit),
                           ("Exit3", trade.exit3_hit), ("Exit4", trade.exit4_hit)):
        if not hit_obj.is_hit:
            continue
        pnl = _exit_pnl_points(trade, hit_obj)
        parts.append(f"{label}:{pnl:+.2f}")
        if best_pnl is None or pnl > best_pnl:
            best_label, best_pnl = label, pnl

    if not parts:
        return "SL"
    return f"{', '.join(parts)} (Best: {best_label})"


def _mark_level_if_hit(exit_hit_obj: exit_hit, level, row, direction, ts, is_stop=False):
    if exit_hit_obj.is_hit:
        return
    high = float(row[HIGH_PRICE])
    low = float(row[LOW_PRICE])
    if is_stop:
        hit = (low <= level) if direction == "BUY" else (high >= level)
    else:
        hit = (high >= level) if direction == "BUY" else (low <= level)
    if hit:
        exit_hit_obj.future_price = level
        exit_hit_obj.option_price = 0.0
        exit_hit_obj.timestamp = ts
        exit_hit_obj.is_hit = True


def _fmt_candle(row):
    # No intrabar ticks are available historically, so LTP is approximated as this candle's Close.
    return (f"O={row[OPEN_PRICE]} H={row[HIGH_PRICE]} L={row[LOW_PRICE]} C={row[CLOSE_PRICE]} "
           f"LTP={row[CLOSE_PRICE]} VWAP={row[VWAP]:.2f}")


def _to_snapshot(row):
    return candle_snapshot(timestamp=str(row[DATE_TIME]), open=float(row[OPEN_PRICE]),
                           high=float(row[HIGH_PRICE]), low=float(row[LOW_PRICE]),
                           close=float(row[CLOSE_PRICE]), vwap=float(row[VWAP]))
