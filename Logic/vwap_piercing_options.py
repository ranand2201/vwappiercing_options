# -*- coding: utf-8 -*-
"""
vwap_piercing_options.py

Paper-trade signal logger for the VWAP Piercing Options strategy (see
VWAPPiercingOptions.xlsx at the repo root for the design spec). Detects the
Piercing -> Reclaim -> Confirm candle pattern on the NIFTY future, and for
each entry logs the forward price path to the PaperTradeData sheet, tracking
5 independent hypothetical exits (SL, Length-of-Piercing, 0.5%, 0.75%,
Bollinger Band) plus EOD and MAE/MFE. No real orders are placed.
"""
import threading
import time
from datetime import datetime, date, timedelta

from BusinessLogic.interfaces.ILogic import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *
from Utility.nse_utility import *
from Utility.utility import *
from DataTypes.defines import *
from DataTypes.trade_data import *
from ..DataTypes.paper_trade_data import *
from ..UserInterface.adapter.login.login import *
from ..UserInterface.adapter.config.config import *
from ..UserInterface.gsheet.paper_trade.paper_trade import *
from . import pattern_rules
from .option_selection import select_by_premium


class LogicVwapPiercingOptions(ILogic):

    STATE_SEEK_PIERCING = "SEEK_PIERCING"
    STATE_SEEK_RECLAIM = "SEEK_RECLAIM"
    STATE_SEEK_CONFIRM_ENTRY = "SEEK_CONFIRM_ENTRY"
    STATE_IN_TRADE = "IN_TRADE"

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
        self.nse_utility = nse_utitlity()
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

        # pattern state
        self.future_symbol = ""
        self.state = self.STATE_SEEK_PIERCING
        self.piercing_candle = None
        self.reclaim_candle = None
        self.direction = ""
        self.processed_candle_count = 0
        self.last_candle_data = None

        # in-trade tracking
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
        current_week_expiry, next_week_expiry, monthly_expiry, is_expiry_day, \
            is_current_week_monthly_expiry, is_next_week_monthly_expiry = \
            self.nse_utility.get_index_expiry_date(self.index_name)
        self.current_week_expiry = current_week_expiry
        self.future_symbol = broker.get_future_name(self.index_name, current_week_expiry)
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

        while not self.__is_time_reached(self.execution_stop_time):
            if not self.__is_time_reached(self.execution_start_time):
                time.sleep(2)
                continue

            self.__process_new_candles()

            if self.state == self.STATE_SEEK_CONFIRM_ENTRY:
                self.__check_entry_trigger()
            elif self.state == self.STATE_IN_TRADE:
                self.__check_exit_hits()

            time.sleep(3)

        if self.state == self.STATE_IN_TRADE:
            self.__finalize_trade_at_eod()

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

        if self.state in (self.STATE_SEEK_PIERCING, self.STATE_SEEK_RECLAIM, self.STATE_SEEK_CONFIRM_ENTRY):
            new_rows = candle_data.iloc[self.processed_candle_count:]
            for _, row in new_rows.iterrows():
                self.__on_candle_close(row)

        self.processed_candle_count = len(candle_data)

    def __on_candle_close(self, row):
        if self.state == self.STATE_SEEK_PIERCING:
            self.__test_piercing(row)
        elif self.state == self.STATE_SEEK_RECLAIM:
            if pattern_rules.is_reclaimed(row, self.direction):
                self.reclaim_candle = row
                self.state = self.STATE_SEEK_CONFIRM_ENTRY
            else:
                self.state = self.STATE_SEEK_PIERCING
                self.piercing_candle = None
                self.__test_piercing(row)
        # STATE_SEEK_CONFIRM_ENTRY needs no candle-close handling: entry is now a live LTP-vs-VWAP
        # check (see __check_entry_trigger), not a candle-level event.

    def __test_piercing(self, row):
        if not self.__is_piercing_window_open():
            return
        direction = pattern_rules.piercing_direction(row)
        if direction is not None:
            self.__set_piercing(row, direction)

    def __is_piercing_window_open(self):
        return pattern_rules.is_piercing_window_open(datetime.now().strftime("%H:%M:%S"),
                                                      self.piercing_start_time)

    def __set_piercing(self, row, direction):
        self.piercing_candle = row
        self.direction = direction
        self.state = self.STATE_SEEK_RECLAIM

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------
    def __check_entry_trigger(self):
        quote_data = self.quotes_utility.get_quote_data()
        if self.future_symbol not in quote_data:
            return
        ltp = quote_data[self.future_symbol].ltp
        current_vwap = self.__get_latest_vwap()
        if current_vwap is None:
            return
        if not pattern_rules.is_vwap_reentry_triggered(ltp, current_vwap, self.direction):
            return
        self.__enter_trade(ltp)

    def __get_latest_vwap(self):
        if self.last_candle_data is None or len(self.last_candle_data) == 0:
            return None
        return float(self.last_candle_data[VWAP].iloc[-1])

    def __enter_trade(self, entry_future_price):
        broker = self.trade_utility.get_broker_utility()
        option_type = "CE" if self.direction == "BUY" else "PE"
        atm_strike = round(entry_future_price / self.strike_step) * self.strike_step
        option_symbol, option_price = self.__select_option_by_premium(broker, atm_strike, option_type)
        if option_symbol is None:
            print(self.logic_name, ": No option found near target premium band; dropping setup")
            self.state = self.STATE_SEEK_PIERCING
            self.piercing_candle = None
            self.reclaim_candle = None
            return

        now_str = datetime.now().strftime("%H:%M:%S")
        row = paper_trade_row()
        row.date = date.today().strftime("%Y-%m-%d")
        row.future = self.future_symbol
        row.option_name = option_symbol
        row.trade_type = self.direction
        row.piercing_candle = self.__row_to_snapshot(self.piercing_candle)
        row.reclaim_candle = self.__row_to_snapshot(self.reclaim_candle)
        row.confirm_candle = candle_snapshot(timestamp=now_str, open=entry_future_price, high=entry_future_price,
                                             low=entry_future_price, close=entry_future_price, vwap=0.0)
        row.entry_future_price = entry_future_price
        row.entry_option_price = option_price
        row.entry_timestamp = now_str

        piercing_high = float(self.piercing_candle[HIGH_PRICE])
        piercing_low = float(self.piercing_candle[LOW_PRICE])
        piercing_length = piercing_high - piercing_low

        if self.direction == "BUY":
            self.sl_level = piercing_low
            self.exit1_level = entry_future_price + piercing_length
            self.exit2_level = get_target_price_by_percentage(entry_future_price, 0.5, "buy")
            self.exit3_level = get_target_price_by_percentage(entry_future_price, 0.75, "buy")
        else:
            self.sl_level = piercing_high
            self.exit1_level = entry_future_price - piercing_length
            self.exit2_level = get_target_price_by_percentage(entry_future_price, 0.5, "sell")
            self.exit3_level = get_target_price_by_percentage(entry_future_price, 0.75, "sell")

        self.current_trade = row
        self.option_symbol = option_symbol
        self.mae = 0.0
        self.mfe = 0.0
        self.mae_time = now_str
        self.mfe_time = now_str

        self.quotes_utility.add_stocks([option_symbol], [self.OPTION_MARKET_TYPE])

        self.state = self.STATE_IN_TRADE
        print(self.logic_name, ": Entered paper trade", self.direction, option_symbol, "@", option_price)

    def __select_option_by_premium(self, broker, atm_strike, option_type):
        # wide net: near expiry, premiums decay fast per strike, so a narrow range around ATM
        # can miss the 100-110 target band entirely (observed in testing: +/-250 points wasn't
        # enough close to expiry). +/-1000 points (40 strikes @ 50-pt steps) covers that.
        lst_contracts = broker.get_option_chain(self.index_name, atm_strike, option_type,
                                                 self.current_week_expiry, p_count=40)
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
    def __check_exit_hits(self):
        quote_data = self.quotes_utility.get_quote_data()
        if self.future_symbol not in quote_data:
            return
        future_ltp = quote_data[self.future_symbol].ltp
        option_ltp = quote_data[self.option_symbol].ltp if self.option_symbol in quote_data else self.current_trade.entry_option_price
        now_str = datetime.now().strftime("%H:%M:%S")

        excursion = (future_ltp - self.current_trade.entry_future_price) if self.direction == "BUY" \
            else (self.current_trade.entry_future_price - future_ltp)
        if excursion < self.mae:
            self.mae = excursion
            self.mae_time = now_str
        if excursion > self.mfe:
            self.mfe = excursion
            self.mfe_time = now_str

        # any trade still open at 14:50 is force-closed at the prevailing price, regardless of
        # SL/Exit-1..4 state.
        if pattern_rules.is_force_exit_time_reached(now_str):
            self.__finalize_trade_at_eod("Force Exit 14:50")
            return

        # capture before-state so we can print the moment each hypothesis first breaches --
        # none of these (besides SL) close the trade, so without this there's no visibility
        # into when/whether they fired.
        was_hit = {
            "Exit1 (Length of Piercing)": self.current_trade.exit1_hit.is_hit,
            "Exit2 (0.5%)": self.current_trade.exit2_hit.is_hit,
            "Exit3 (0.75%)": self.current_trade.exit3_hit.is_hit,
            "Exit4 (Bollinger)": self.current_trade.exit4_hit.is_hit,
        }

        self.__mark_exit_if_hit(self.current_trade.sl_hit, self.sl_level, future_ltp, option_ltp, now_str, is_stop=True)
        self.__mark_exit_if_hit(self.current_trade.exit1_hit, self.exit1_level, future_ltp, option_ltp, now_str)
        self.__mark_exit_if_hit(self.current_trade.exit2_hit, self.exit2_level, future_ltp, option_ltp, now_str)
        self.__mark_exit_if_hit(self.current_trade.exit3_hit, self.exit3_level, future_ltp, option_ltp, now_str)

        bollinger_level = self.__get_bollinger_exit_level()
        if bollinger_level is not None:
            self.__mark_exit_if_hit(self.current_trade.exit4_hit, bollinger_level, future_ltp, option_ltp, now_str)

        for label, hit_obj in (("Exit1 (Length of Piercing)", self.current_trade.exit1_hit),
                               ("Exit2 (0.5%)", self.current_trade.exit2_hit),
                               ("Exit3 (0.75%)", self.current_trade.exit3_hit),
                               ("Exit4 (Bollinger)", self.current_trade.exit4_hit)):
            if not was_hit[label] and hit_obj.is_hit:
                print(self.logic_name, f": {label} target BREACHED @ {hit_obj.future_price}",
                     "(hypothesis only -- trade continues, only SL closes it)")

        # SL is the one real exit here. Once it fires, the position is closed for real, so log
        # the trade now and go back to scanning for the next Piercing setup -- not capped at
        # one trade per day.
        if self.current_trade.sl_hit.is_hit:
            self.current_trade.mae = self.mae
            self.current_trade.mae_time = self.mae_time
            self.current_trade.mfe = self.mfe
            self.current_trade.mfe_time = self.mfe_time
            self.__finalize_and_reset("SL")

    def __mark_exit_if_hit(self, exit_hit_obj: exit_hit, level, future_ltp, option_ltp, now_str, is_stop=False):
        if exit_hit_obj.is_hit or level == 0.0:
            return
        if is_stop:
            hit = (future_ltp <= level) if self.direction == "BUY" else (future_ltp >= level)
        else:
            hit = (future_ltp >= level) if self.direction == "BUY" else (future_ltp <= level)
        if hit:
            exit_hit_obj.future_price = future_ltp
            exit_hit_obj.option_price = option_ltp
            exit_hit_obj.timestamp = now_str
            exit_hit_obj.is_hit = True

    def __get_bollinger_exit_level(self):
        # Exit-4 target: Bollinger upper band for BUY, lower band for SELL -- price reaching the
        # band in the trade's favor, same target-style semantics as Exit-1..3 (not a stop).
        if self.last_candle_data is None or len(self.last_candle_data) < self.bollinger_period:
            return None
        upper_band, middle_band, lower_band = compute_bollinger_bands(self.last_candle_data,
                                                                       period=self.bollinger_period,
                                                                       std_dev=self.bollinger_std_dev)
        band_value = upper_band.iloc[-1] if self.direction == "BUY" else lower_band.iloc[-1]
        if band_value != band_value:  # NaN check without importing pandas/numpy here
            return None
        return float(band_value)

    def __finalize_trade_at_eod(self, reason="EOD"):
        quote_data = self.quotes_utility.get_quote_data()
        future_ltp = quote_data[self.future_symbol].ltp if self.future_symbol in quote_data else self.current_trade.entry_future_price
        option_ltp = quote_data[self.option_symbol].ltp if self.option_symbol in quote_data else self.current_trade.entry_option_price

        self.current_trade.exit5_eod.future_price = future_ltp
        self.current_trade.exit5_eod.option_price = option_ltp
        self.current_trade.mae = self.mae
        self.current_trade.mae_time = self.mae_time
        self.current_trade.mfe = self.mfe
        self.current_trade.mfe_time = self.mfe_time

        self.__finalize_and_reset(reason)

    def __finalize_and_reset(self, reason="EOD"):
        self.obj_paper_trade_writer.write_trade(self.current_trade)
        print(self.logic_name, f": Trade closed ({reason}), logged to PaperTradeData:",
             self.current_trade.trade_type, self.current_trade.option_name)
        self.current_trade = None
        self.option_symbol = ""
        self.piercing_candle = None
        self.reclaim_candle = None
        # if the piercing window is already closed (past 14:30 or before start+15min at EOD),
        # this just idles in SEEK_PIERCING harmlessly -- __test_piercing gates on the window.
        self.state = self.STATE_SEEK_PIERCING

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def __row_to_snapshot(self, row):
        return candle_snapshot(timestamp=str(row[DATE_TIME]), open=float(row[OPEN_PRICE]),
                               high=float(row[HIGH_PRICE]), low=float(row[LOW_PRICE]),
                               close=float(row[CLOSE_PRICE]), vwap=float(row[VWAP]))

    def __is_time_reached(self, str_time):
        target_time = datetime.strptime(str_time, "%H:%M:%S").time()
        return datetime.now().time() >= target_time
