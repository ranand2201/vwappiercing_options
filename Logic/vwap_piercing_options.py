# -*- coding: utf-8 -*-
"""
vwap_piercing_options.py

Paper-trade signal logger for the VWAP Piercing Options strategy (see
VWAPPiercingOptions.xlsx at the repo root for the design spec). Detects the
Piercing -> Reclaim -> Confirm candle pattern on the NIFTY future, and for
each entry logs the forward price path to the PaperTradeData sheet, tracking
5 independent hypothetical exits (SL, Length-of-Piercing, 0.5%, 0.75%,
Bollinger Band) plus EOD and MAE/MFE. No real orders are placed.

BUY and SELL setups are tracked as two fully independent state machines
(see _DirectionState) so a piercing in one direction is never blocked or
lost while the other direction already has a setup/trade in progress.
"""
import threading
import time
from datetime import datetime, date, timedelta

from BusinessLogic.interfaces.ILogic import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *
from Utility.utility import *
from DataTypes.defines import *
from DataTypes.trade_data import *
from ..DataTypes.paper_trade_data import *
from ..UserInterface.adapter.login.login import *
from ..UserInterface.adapter.config.config import *
from ..UserInterface.gsheet.paper_trade.paper_trade import *
from . import pattern_rules
from .backtest_engine import resolve_front_month_future_symbol, describe_exit_outcomes
from .option_selection import select_by_premium

STATE_SEEK_PIERCING = "SEEK_PIERCING"
STATE_SEEK_RECLAIM = "SEEK_RECLAIM"
STATE_SEEK_CONFIRM_ENTRY = "SEEK_CONFIRM_ENTRY"
STATE_IN_TRADE = "IN_TRADE"


class _DirectionState:
    """All the mutable pattern/trade-tracking state for one direction (BUY or SELL)."""

    def __init__(self, direction):
        self.direction = direction
        self.state = STATE_SEEK_PIERCING
        self.piercing_candle = None
        self.reclaim_candle = None
        # ticks are polled every few seconds, but the periodic waiting-for-entry/in-trade status
        # line should only print once per candle close, not every poll -- this tracks the candle
        # timestamp that was last logged for that purpose.
        self.last_logged_candle_ts = None

        self.current_trade: paper_trade_row = None
        self.option_symbol = ""
        self.sl_level = 0.0
        self.exit1_level = 0.0
        self.exit2_level = 0.0
        self.exit3_level = 0.0
        self.mae = 0.0
        self.mfe = 0.0
        self.mae_time = ""
        self.mfe_time = ""


class LogicVwapPiercingOptions(ILogic):

    STATE_SEEK_PIERCING = STATE_SEEK_PIERCING
    STATE_SEEK_RECLAIM = STATE_SEEK_RECLAIM
    STATE_SEEK_CONFIRM_ENTRY = STATE_SEEK_CONFIRM_ENTRY
    STATE_IN_TRADE = STATE_IN_TRADE

    # Zebu's fetchOHLC/get_quotes/place_order treat market_type="" as "this is an option
    # symbol, parse strike/expiry out of it via __get_option_name" and market_type="EQ" as
    # "append -EQ". A future symbol needs any other non-empty value so it's used as-is and
    # resolved to NFO. Options are trickier: __get_option_name's re-parse assumes a
    # "<STRIKE><CE|PE>" suffix format, but real Zebu option symbols (from get_option_chain) are
    # "<SYMBOL><DDMONYY><C|P><STRIKE>" -- re-parsing an already-correct symbol through that
    # mismatched format would corrupt it. Since our option symbols always come pre-resolved from
    # get_option_chain, use a non-"" sentinel for them too so they're passed through as-is.
    FUTURE_MARKET_TYPE = "FUT"
    OPTION_MARKET_TYPE = "OPT"

    def __init__(self, args, broker_utility_manager: utility_manager, quotes_utility: QuoteUtility):
        self.logic_name = "LogicVwapPiercingOptions"
        self.obj_utility_manager = broker_utility_manager
        self.obj_ui_adapter_login: UserInterfaceAdapterLogin = UserInterfaceAdapterLogin(args)
        self.obj_ui_adapter_config: UserInterfaceAdapterConfig = UserInterfaceAdapterConfig(args)
        self.trade_utility = self.obj_utility_manager.get_utility_object(self.obj_ui_adapter_login.get_data())
        self.quotes_utility: QuoteUtility = quotes_utility
        # wire this up now, before any threads start -- pre_requisite_thread calls
        # quotes_utility.add_stocks() almost immediately, which needs trade_utility to already be
        # set. executor.py also calls set_trade_utility() after create() returns, but that's too
        # late to win the race against pre_requisite_thread; this call makes that one redundant
        # but harmless (idempotent), and fixes the actual race here at the source.
        self.quotes_utility.set_trade_utility(self.trade_utility)
        self.config_data = self.obj_ui_adapter_config.get_data()
        self.obj_paper_trade_writer = UserInterfacePaperTrade(args.key)

        # strategy constants
        self.index_name = "NIFTY"
        self.strike_step = 50
        self.target_premium_low = 100.0
        self.target_premium_high = 110.0
        self.bollinger_period = 20
        self.bollinger_std_dev = 2

        self.pre_requisite_complete_event = threading.Event()
        self.day_preset = 0

        self.pre_requisite_start_time = (datetime.now() + timedelta(seconds=10)).strftime('%H:%M:%S')
        self.execution_start_time = self.config_data.start_time
        self.execution_stop_time = self.config_data.end_time
        self.candle_interval_minutes = int(self.config_data.candle_interval)
        self.piercing_start_time = pattern_rules.compute_piercing_start_time(self.execution_start_time)

        # pattern state -- BUY and SELL are tracked as two fully independent state machines so a
        # setup in one direction never blocks or gets clobbered by the other.
        self.future_symbol = ""
        self.directions = {"BUY": _DirectionState("BUY"), "SELL": _DirectionState("SELL")}
        self.processed_candle_count = 0
        self.last_candle_data = None

        self.pre_requisite_thread = threading.Thread(target=self.pre_requisite_thread_handler)
        self.execute_thread = threading.Thread(target=self.execute)
        self.exit_thread = threading.Thread(target=self.exit_execution_thread)

        self.pre_requisite_thread.start()
        self.execute_thread.start()
        self.exit_thread.start()

    def get_broker_utility(self):
        return self.trade_utility

    def get_thread_info(self):
        return self.exit_thread

    # ------------------------------------------------------------------
    # thread handlers
    # ------------------------------------------------------------------
    def pre_requisite_thread_handler(self):
        print(self.logic_name, ": Inside pre-requisite thread")
        broker = self.trade_utility.get_broker_utility()
        # NIFTY here trades MONTHLY futures, not weekly (see backtest_engine.resolve_front_month_
        # future_symbol) -- reuse that same resolution (incl. its last-week-of-month rollover to
        # next month's contract) rather than nse_utility.get_index_expiry_date's weekly-oriented
        # logic, so the live engine and backtest can never disagree on which contract is "front
        # month" on a given day.
        today_str = date.today().strftime("%Y-%m-%d")
        self.future_symbol, self.current_expiry = resolve_front_month_future_symbol(
            broker, self.index_name, today_str)
        print(self.logic_name, ": Future symbol resolved: ", self.future_symbol)
        # Zebu's fetchOHLC/get_quotes treat market_type="" as "parse this as an option symbol"
        # (see zebumynt_utitlity.fetchOHLC / __get_option_name); a future needs any non-empty,
        # non-"EQ" value so the symbol is used as-is and resolved to NFO.
        self.quotes_utility.add_stocks([self.future_symbol], [self.FUTURE_MARKET_TYPE])
        self.pre_requisite_complete_event.set()
        print(self.logic_name, ": Exiting pre-requisite thread")

    def execute(self):
        self.pre_requisite_complete_event.wait()
        print(self.logic_name, ": Execution Started", self.execution_start_time)
        has_started = False
        piercing_window_announced = False

        while not self.__is_time_reached(self.execution_stop_time):
            if not self.__is_time_reached(self.execution_start_time):
                time.sleep(2)
                continue

            if not has_started:
                print(self.logic_name, f": [{datetime.now().strftime('%H:%M:%S')}] execution start time reached, beginning candle/tick processing")
                has_started = True
            if not piercing_window_announced and self.__is_piercing_window_open():
                print(self.logic_name, f": [{datetime.now().strftime('%H:%M:%S')}] piercing window open (>= {self.piercing_start_time})")
                piercing_window_announced = True

            self.__process_new_candles()

            for ds in self.directions.values():
                if ds.state == STATE_SEEK_CONFIRM_ENTRY:
                    self.__check_entry_trigger(ds)
                elif ds.state == STATE_IN_TRADE:
                    self.__check_exit_hits(ds)

            time.sleep(3)

        for ds in self.directions.values():
            if ds.state == STATE_IN_TRADE:
                self.__finalize_trade_at_eod(ds)

        print(self.logic_name, ": Execution loop ended")

    def exit_execution_thread(self):
        print(self.logic_name, ": Start of Exiting Thread")
        self.execute_thread.join()
        self.quotes_utility.stop()
        self.quotes_utility.get_thread_info().join()
        print(self.logic_name, ": End of Exiting Thread")

    # ------------------------------------------------------------------
    # candle pattern state machine
    # ------------------------------------------------------------------
    def __process_new_candles(self):
        broker = self.trade_utility.get_broker_utility()
        # zebumynt_utitlity.getTimeFrame() returns epoch-second strings (via strftime('%s'), which
        # isn't even portable on Windows) -- but fetchOHLC expects "YYYY-MM-DD HH:MM:SS" and does
        # its own epoch conversion internally, so build the strings directly instead, matching the
        # backtest/dry-run scripts' already-proven-working pattern.
        today_str = date.today().strftime("%Y-%m-%d")
        str_from_date = f"{today_str} 09:15:00"
        str_to_date = f"{today_str} {datetime.now().strftime('%H:%M:%S')}"
        candle_data = broker.fetchOHLC(self.future_symbol, str_from_date, str_to_date,
                                       interval=f"{self.candle_interval_minutes}minute",
                                       all_data=True, market_type=self.FUTURE_MARKET_TYPE)
        if candle_data is None or len(candle_data) == 0:
            return

        # Zebu's "intvwap" field is a per-candle (interval) VWAP, not a cumulative session VWAP
        # from day open -- confirmed by its volatility mirroring price itself rather than
        # smoothing out as the session progresses. The Piercing/Reclaim pattern needs the real
        # cumulative session VWAP, so always compute it ourselves rather than trusting intvwap.
        candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]

        self.last_candle_data = candle_data

        new_rows = candle_data.iloc[self.processed_candle_count:]
        for _, row in new_rows.iterrows():
            for ds in self.directions.values():
                if ds.state in (STATE_SEEK_PIERCING, STATE_SEEK_RECLAIM):
                    self.__on_candle_close(ds, row)

        self.processed_candle_count = len(candle_data)

    def __on_candle_close(self, ds: _DirectionState, row):
        ts = str(row[DATE_TIME])
        if ds.state == STATE_SEEK_PIERCING:
            if not self.__test_piercing(ds, row):
                print(self.logic_name, f": [{ts}] ({ds.direction}) seeking piercing {self.__fmt_candle(row)}")
        elif ds.state == STATE_SEEK_RECLAIM:
            if pattern_rules.is_reclaimed(row, ds.direction):
                ds.reclaim_candle = row
                ds.state = STATE_SEEK_CONFIRM_ENTRY
                print(self.logic_name, f": [{ts}] ({ds.direction}) RECLAIM {self.__fmt_candle(row)}")
            else:
                # Not reclaimed yet -- keep waiting on subsequent candles rather than abandoning
                # after just one miss. The piercing candle stays the reference point.
                print(self.logic_name, f": [{ts}] ({ds.direction}) no reclaim yet, still waiting {self.__fmt_candle(row)}")
        # STATE_SEEK_CONFIRM_ENTRY needs no candle-close handling: entry is now a live LTP-vs-VWAP
        # check (see __check_entry_trigger), not a candle-level event.

    def __test_piercing(self, ds: _DirectionState, row):
        # gate on the candle's OWN timestamp, not wall-clock now() -- fetchOHLC backfills the
        # whole day (09:15 onward) on every poll, so if the engine started late (or briefly lost
        # connectivity) and is catching up on old candles, wall-clock time would already satisfy
        # the window-open check for all of them, letting pre-window candles through incorrectly.
        if not pattern_rules.is_piercing_window_open(pattern_rules.time_of_day(str(row[DATE_TIME])),
                                                      self.piercing_start_time):
            return False
        direction = pattern_rules.piercing_direction(row)
        if direction != ds.direction:
            return False
        self.__set_piercing(ds, row)
        print(self.logic_name, f": [{str(row[DATE_TIME])}] PIERCING ({ds.direction}) {self.__fmt_candle(row)}")
        return True

    def __is_piercing_window_open(self):
        return pattern_rules.is_piercing_window_open(datetime.now().strftime("%H:%M:%S"),
                                                      self.piercing_start_time)

    def __set_piercing(self, ds: _DirectionState, row):
        ds.piercing_candle = row
        ds.state = STATE_SEEK_RECLAIM

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------
    def __check_entry_trigger(self, ds: _DirectionState):
        quote_data = self.quotes_utility.get_quote_data()
        if self.future_symbol not in quote_data:
            return
        ltp = quote_data[self.future_symbol].ltp
        current_vwap = self.__get_latest_vwap()
        if current_vwap is None:
            return
        triggered = pattern_rules.is_vwap_reentry_triggered(ltp, current_vwap, ds.direction)
        now_str = datetime.now().strftime("%H:%M:%S")
        if triggered:
            # a real event -- always print, not subject to the once-per-candle throttle below.
            print(self.logic_name, f": [{now_str}] ({ds.direction}) waiting for entry: LTP={ltp} vs VWAP={current_vwap:.2f} -> TRIGGERED")
        else:
            self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) waiting for entry: LTP={ltp} vs VWAP={current_vwap:.2f}")
        if not triggered:
            return
        self.__enter_trade(ds, ltp)

    def __get_latest_vwap(self):
        if self.last_candle_data is None or len(self.last_candle_data) == 0:
            return None
        return float(self.last_candle_data[VWAP].iloc[-1])

    def __latest_candle_ts(self):
        if self.last_candle_data is None or len(self.last_candle_data) == 0:
            return None
        return str(self.last_candle_data[DATE_TIME].iloc[-1])

    def __log_once_per_candle(self, ds: _DirectionState, message):
        # ticks come in every few seconds, but this status line should only print once per candle
        # close -- suppress repeats until the underlying candle data actually advances.
        candle_ts = self.__latest_candle_ts()
        if candle_ts is not None and candle_ts == ds.last_logged_candle_ts:
            return
        if candle_ts is not None:
            ds.last_logged_candle_ts = candle_ts
        print(self.logic_name, message)

    def __enter_trade(self, ds: _DirectionState, entry_future_price):
        broker = self.trade_utility.get_broker_utility()
        option_type = "CE" if ds.direction == "BUY" else "PE"
        atm_strike = round(entry_future_price / self.strike_step) * self.strike_step
        option_symbol, option_price = self.__select_option_by_premium(broker, atm_strike, option_type)
        if option_symbol is None:
            print(self.logic_name, f": ({ds.direction}) No option found near target premium band; dropping setup")
            ds.state = STATE_SEEK_PIERCING
            ds.piercing_candle = None
            ds.reclaim_candle = None
            return

        now_str = datetime.now().strftime("%H:%M:%S")
        row = paper_trade_row()
        row.date = date.today().strftime("%Y-%m-%d")
        row.future = self.future_symbol
        row.option_name = option_symbol
        row.trade_type = ds.direction
        row.piercing_candle = self.__row_to_snapshot(ds.piercing_candle)
        row.reclaim_candle = self.__row_to_snapshot(ds.reclaim_candle)
        row.confirm_candle = candle_snapshot(timestamp=now_str, open=entry_future_price, high=entry_future_price,
                                             low=entry_future_price, close=entry_future_price, vwap=0.0)
        row.entry_future_price = entry_future_price
        row.entry_option_price = option_price
        row.entry_timestamp = now_str

        piercing_high = float(ds.piercing_candle[HIGH_PRICE])
        piercing_low = float(ds.piercing_candle[LOW_PRICE])
        piercing_length = piercing_high - piercing_low

        if ds.direction == "BUY":
            ds.sl_level = piercing_low
            ds.exit1_level = entry_future_price + piercing_length
            ds.exit2_level = get_target_price_by_percentage(entry_future_price, 0.5, "buy")
            ds.exit3_level = get_target_price_by_percentage(entry_future_price, 0.75, "buy")
        else:
            ds.sl_level = piercing_high
            ds.exit1_level = entry_future_price - piercing_length
            ds.exit2_level = get_target_price_by_percentage(entry_future_price, 0.5, "sell")
            ds.exit3_level = get_target_price_by_percentage(entry_future_price, 0.75, "sell")

        ds.current_trade = row
        ds.option_symbol = option_symbol
        ds.mae = 0.0
        ds.mfe = 0.0
        ds.mae_time = now_str
        ds.mfe_time = now_str

        self.quotes_utility.add_stocks([option_symbol], [self.OPTION_MARKET_TYPE])

        ds.state = STATE_IN_TRADE
        # reset the once-per-candle throttle on entry so the first in-trade status line isn't
        # suppressed by the candle timestamp already logged during the waiting-for-entry phase.
        ds.last_logged_candle_ts = None
        print(self.logic_name, ": Entered paper trade", ds.direction, option_symbol, "@", option_price)

    def __select_option_by_premium(self, broker, atm_strike, option_type):
        # wide net: near expiry, premiums decay fast per strike, so a narrow range around ATM
        # can miss the 100-110 target band entirely (observed in testing: +/-250 points wasn't
        # enough close to expiry). +/-1000 points (40 strikes @ 50-pt steps) covers that.
        lst_contracts = broker.get_option_chain(self.index_name, atm_strike, option_type,
                                                 self.current_expiry, p_count=40)
        if not lst_contracts:
            return None, 0.0

        lst_get_quote_req = [get_quote_request_data(p_symbol=tsym, p_market_type=self.OPTION_MARKET_TYPE)
                             for tsym, strike, opt in lst_contracts]
        dict_quotes = broker.get_quotes(lst_get_quote_req)

        return select_by_premium(lst_contracts, dict_quotes, self.target_premium_low, self.target_premium_high)

    # ------------------------------------------------------------------
    # in-trade exit tracking. SL is the ONLY real exit -- Exit-1..4 are all parallel
    # hypotheses tracked for comparison only; breaching one is logged but does not close
    # the trade.
    # ------------------------------------------------------------------
    def __check_exit_hits(self, ds: _DirectionState):
        quote_data = self.quotes_utility.get_quote_data()
        if self.future_symbol not in quote_data:
            return
        future_ltp = quote_data[self.future_symbol].ltp
        option_ltp = quote_data[ds.option_symbol].ltp if ds.option_symbol in quote_data else ds.current_trade.entry_option_price
        now_str = datetime.now().strftime("%H:%M:%S")

        excursion = (future_ltp - ds.current_trade.entry_future_price) if ds.direction == "BUY" \
            else (ds.current_trade.entry_future_price - future_ltp)
        if excursion < ds.mae:
            ds.mae = excursion
            ds.mae_time = now_str
        if excursion > ds.mfe:
            ds.mfe = excursion
            ds.mfe_time = now_str

        # any trade still open at 14:50 is force-closed at the prevailing price, regardless of
        # SL/Exit-1..4 state.
        if pattern_rules.is_force_exit_time_reached(now_str):
            self.__finalize_trade_at_eod(ds, "Force Exit 14:50")
            return

        # capture before-state so we can print the moment each hypothesis first breaches --
        # none of these (besides SL) close the trade, so without this there's no visibility
        # into when/whether they fired.
        was_hit = {
            "Exit1 (Length of Piercing)": ds.current_trade.exit1_hit.is_hit,
            "Exit2 (0.5%)": ds.current_trade.exit2_hit.is_hit,
            "Exit3 (0.75%)": ds.current_trade.exit3_hit.is_hit,
            "Exit4 (Bollinger)": ds.current_trade.exit4_hit.is_hit,
        }

        self.__mark_exit_if_hit(ds, ds.current_trade.sl_hit, ds.sl_level, future_ltp, option_ltp, now_str, is_stop=True)
        self.__mark_exit_if_hit(ds, ds.current_trade.exit1_hit, ds.exit1_level, future_ltp, option_ltp, now_str)
        self.__mark_exit_if_hit(ds, ds.current_trade.exit2_hit, ds.exit2_level, future_ltp, option_ltp, now_str)
        self.__mark_exit_if_hit(ds, ds.current_trade.exit3_hit, ds.exit3_level, future_ltp, option_ltp, now_str)

        bollinger_level = self.__get_bollinger_exit_level(ds)
        bollinger_str = f"{bollinger_level:.2f}" if bollinger_level is not None else "n/a"
        if bollinger_level is not None:
            self.__mark_exit_if_hit(ds, ds.current_trade.exit4_hit, bollinger_level, future_ltp, option_ltp, now_str)

        for label, hit_obj in (("Exit1 (Length of Piercing)", ds.current_trade.exit1_hit),
                               ("Exit2 (0.5%)", ds.current_trade.exit2_hit),
                               ("Exit3 (0.75%)", ds.current_trade.exit3_hit),
                               ("Exit4 (Bollinger)", ds.current_trade.exit4_hit)):
            if not was_hit[label] and hit_obj.is_hit:
                print(self.logic_name, f": ({ds.direction}) {label} target BREACHED @ {hit_obj.future_price}",
                     "(hypothesis only -- trade continues, only SL closes it)")

        self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) in-trade LTP={future_ltp} SL={ds.sl_level} "
             f"Exit1={ds.exit1_level:.2f} Exit2={ds.exit2_level:.2f} Exit3={ds.exit3_level:.2f} "
             f"Exit4(Bollinger)={bollinger_str}")

        # SL is the one real exit here. Once it fires, the position is closed for real, so log
        # the trade now and go back to scanning for the next Piercing setup -- not capped at
        # one trade per day.
        if ds.current_trade.sl_hit.is_hit:
            ds.current_trade.mae = ds.mae
            ds.current_trade.mae_time = ds.mae_time
            ds.current_trade.mfe = ds.mfe
            ds.current_trade.mfe_time = ds.mfe_time
            self.__finalize_and_reset(ds, "SL")

    def __mark_exit_if_hit(self, ds: _DirectionState, exit_hit_obj: exit_hit, level, future_ltp, option_ltp, now_str, is_stop=False):
        if exit_hit_obj.is_hit or level == 0.0:
            return
        if is_stop:
            hit = (future_ltp <= level) if ds.direction == "BUY" else (future_ltp >= level)
        else:
            hit = (future_ltp >= level) if ds.direction == "BUY" else (future_ltp <= level)
        if hit:
            exit_hit_obj.future_price = future_ltp
            exit_hit_obj.option_price = option_ltp
            exit_hit_obj.timestamp = now_str
            exit_hit_obj.is_hit = True

    def __get_bollinger_exit_level(self, ds: _DirectionState):
        # Exit-4 target: Bollinger upper band for BUY, lower band for SELL -- price reaching the
        # band in the trade's favor, same target-style semantics as Exit-1..3 (not a stop).
        if self.last_candle_data is None or len(self.last_candle_data) < self.bollinger_period:
            return None
        upper_band, middle_band, lower_band = compute_bollinger_bands(self.last_candle_data,
                                                                       period=self.bollinger_period,
                                                                       std_dev=self.bollinger_std_dev)
        band_value = upper_band.iloc[-1] if ds.direction == "BUY" else lower_band.iloc[-1]
        if band_value != band_value:  # NaN check without importing pandas/numpy here
            return None
        return float(band_value)

    def __finalize_trade_at_eod(self, ds: _DirectionState, reason="EOD"):
        quote_data = self.quotes_utility.get_quote_data()
        future_ltp = quote_data[self.future_symbol].ltp if self.future_symbol in quote_data else ds.current_trade.entry_future_price
        option_ltp = quote_data[ds.option_symbol].ltp if ds.option_symbol in quote_data else ds.current_trade.entry_option_price

        ds.current_trade.exit5_eod.future_price = future_ltp
        ds.current_trade.exit5_eod.option_price = option_ltp
        ds.current_trade.mae = ds.mae
        ds.current_trade.mae_time = ds.mae_time
        ds.current_trade.mfe = ds.mfe
        ds.current_trade.mfe_time = ds.mfe_time

        self.__finalize_and_reset(ds, reason)

    def __finalize_and_reset(self, ds: _DirectionState, reason="EOD"):
        self.obj_paper_trade_writer.write_trade(ds.current_trade, self.candle_interval_minutes,
                                                describe_exit_outcomes(ds.current_trade))
        print(self.logic_name, f": ({ds.direction}) Trade closed ({reason}), logged to PaperTradeData:",
             ds.current_trade.trade_type, ds.current_trade.option_name)
        ds.current_trade = None
        ds.option_symbol = ""
        ds.piercing_candle = None
        ds.reclaim_candle = None
        ds.last_logged_candle_ts = None
        # if the piercing window is already closed (past 14:30 or before start+15min at EOD),
        # this just idles in SEEK_PIERCING harmlessly -- __test_piercing gates on the window.
        ds.state = STATE_SEEK_PIERCING

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def __row_to_snapshot(self, row):
        return candle_snapshot(timestamp=str(row[DATE_TIME]), open=float(row[OPEN_PRICE]),
                               high=float(row[HIGH_PRICE]), low=float(row[LOW_PRICE]),
                               close=float(row[CLOSE_PRICE]), vwap=float(row[VWAP]))

    def __fmt_candle(self, row):
        return (f"O={row[OPEN_PRICE]} H={row[HIGH_PRICE]} L={row[LOW_PRICE]} C={row[CLOSE_PRICE]} "
               f"VWAP={row[VWAP]:.2f}")

    def __is_time_reached(self, str_time):
        target_time = datetime.strptime(str_time, "%H:%M:%S").time()
        return datetime.now().time() >= target_time
