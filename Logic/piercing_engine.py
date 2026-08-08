# -*- coding: utf-8 -*-
"""
piercing_engine.py

Single engine for the VWAP Piercing Options pattern (Piercing -> Reclaim ->
Confirm/Entry -> SL/Exit-1..4/EOD), used identically by both the live
paper-trade logger and the historical backtest replay. A `mode` (see `Mode`)
tells the engine which of the two it's running as -- LIVE polls a broker's
live quotes/candles on a background thread and writes each completed trade
to PaperTradeData as it closes; BACKTEST replays one historical day's
candles in a single pass and returns the list of completed trades for the
caller to write to BackTestData. Everything about the actual trading rules
(piercing/reclaim/entry conditions, SL and Exit-1..4 level formulas, MAE/MFE
tracking, the 14:50 force-exit cutoff, best-case-exit description) lives
here exactly once, so a rule change can't require touching two engines and
drifting between them -- only the handful of genuinely different
touchpoints (candle/tick sourcing, real option quotes vs none, threads vs a
plain loop, log wording/destination) branch on `mode`.

BUY and SELL setups are tracked as two fully independent state machines (see
_DirectionState) so a piercing in one direction is never blocked or lost
while the other direction already has a setup/trade in progress.

SL is the ONLY real exit -- Exit-1..4 (Length-of-Piercing, 0.5%, 0.75%,
Bollinger) are all parallel hypotheses tracked for comparison only; breaching
one is logged but doesn't close the trade. Any trade still open at 14:50 is
force-closed at the prevailing price regardless of SL/Exit-1..4 state.
"""
import threading
import time
from datetime import datetime, date, timedelta
from enum import Enum

from BusinessLogic.interfaces.ILogic import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *
from Utility.utility import compute_vwap, compute_bollinger_bands, get_target_price_by_percentage, generate_weekly_expiry_dates
from DataTypes.defines import *
from ..DataTypes.paper_trade_data import paper_trade_row, candle_snapshot, exit_hit
from ..UserInterface.adapter.login.login import *
from ..UserInterface.adapter.config.config import *
from ..UserInterface.gsheet.paper_trade.paper_trade import *
from . import pattern_rules
from .option_selection import select_by_premium, select_cheapest_in_band


class Mode(Enum):
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


STATE_SEEK_PIERCING = "SEEK_PIERCING"
STATE_SEEK_RECLAIM = "SEEK_RECLAIM"
STATE_SEEK_CONFIRM_ENTRY = "SEEK_CONFIRM_ENTRY"
STATE_IN_TRADE = "IN_TRADE"

STATUS_NO_DATA = "no_data"
STATUS_OK = "ok"

DAY_START_TIME = "09:15:00"
BOLLINGER_PERIOD = 20
BOLLINGER_STD_DEV = 2

# Fyers' real option-chain-query symbol for each index's underlying (confirmed via
# test_fyers_option_chain.py against the live API: "NSE:NIFTY50-INDEX"). getOptionChain()
# prepends the exchange itself, so only the bare symbol goes here.
CHAIN_UNDERLYING_SYMBOL = {"NIFTY": "NIFTY50-INDEX", "BANKNIFTY": "NIFTYBANK-INDEX"}

# BACKTEST-only historical option lookups are per-symbol calls (no batched chain-quote
# equivalent exists for a past date) -- pace them to stay under the broker's rate limit.
HISTORICAL_OPTION_LOOKUP_DELAY_SECONDS = 0.5


class _DirectionState:
    """All the mutable pattern/trade-tracking state for one direction (BUY or SELL)."""

    def __init__(self, direction):
        self.direction = direction
        self.state = STATE_SEEK_PIERCING
        self.piercing_candle = None
        self.reclaim_candle = None
        # 1-min reclaim candles carry no VWAP of their own -- the main-interval VWAP they were
        # checked against is tracked separately here.
        self.reclaim_vwap = 0.0
        # LIVE only: ticks are polled every few seconds, but the periodic waiting-for-entry/
        # in-trade status line should only print once per candle close, not every poll -- this
        # tracks the candle timestamp that was last logged for that purpose.
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


class VwapPiercingEngine(ILogic):

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
    # mismatched format would corrupt it. Since our option symbols always come pre-resolved,
    # a non-"" sentinel bypasses that reparse. Fyers has the OPPOSITE quirk (any non-"", non-"FUT"
    # value gets a "-{market_type}" suffix appended, which corrupts an already-complete Fyers
    # option symbol) -- so the correct sentinel is genuinely broker-specific. Each broker utility
    # class exposes its own correct value via `OPTION_MARKET_TYPE`; see __option_market_type().
    FUTURE_MARKET_TYPE = "FUT"
    OPTION_MARKET_TYPE = "OPT"  # fallback default if a broker doesn't declare its own

    def __init__(self, mode: Mode, **kwargs):
        self.mode = mode
        self.logic_name = "LogicVwapPiercingOptions"

        # strategy constants
        self.index_name = "NIFTY"
        self.strike_step = 50
        self.target_premium_low = 100.0
        self.target_premium_high = 130.0
        self.bollinger_period = BOLLINGER_PERIOD
        self.bollinger_std_dev = BOLLINGER_STD_DEV

        # pattern state -- BUY and SELL are tracked as two fully independent state machines so a
        # setup in one direction never blocks or gets clobbered by the other.
        self.future_symbol = ""
        self.current_expiry = ""
        self.directions = {"BUY": _DirectionState("BUY"), "SELL": _DirectionState("SELL")}
        self.last_candle_data = None  # main-interval candle series (with VWAP column)

        if mode == Mode.LIVE:
            self.__init_live(**kwargs)
        else:
            self.__init_backtest(**kwargs)

    # ------------------------------------------------------------------
    # mode-specific setup
    # ------------------------------------------------------------------
    def __init_live(self, args, broker_utility_manager: utility_manager, quotes_utility: QuoteUtility):
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

        self.pre_requisite_complete_event = threading.Event()
        self.day_preset = 0

        self.pre_requisite_start_time = (datetime.now() + timedelta(seconds=10)).strftime('%H:%M:%S')
        self.execution_start_time = self.config_data.start_time
        self.execution_stop_time = self.config_data.end_time
        self.candle_interval_minutes = int(self.config_data.candle_interval)
        self.piercing_start_time = pattern_rules.compute_piercing_start_time(self.execution_start_time)

        self.processed_candle_count = 0
        self.processed_candle_count_1min = 0

        self.pre_requisite_thread = threading.Thread(target=self.pre_requisite_thread_handler)
        self.execute_thread = threading.Thread(target=self.execute)
        self.exit_thread = threading.Thread(target=self.exit_execution_thread)

        self.pre_requisite_thread.start()
        self.execute_thread.start()
        self.exit_thread.start()

    def __init_backtest(self, broker, index_name, trade_date_str, candle_interval_minutes, log_fn=None):
        self.broker = broker
        self.index_name = index_name
        self.trade_date_str = trade_date_str
        self.candle_interval_minutes = candle_interval_minutes
        self.log_fn = log_fn or (lambda msg: None)
        self.piercing_start_time = pattern_rules.compute_piercing_start_time(DAY_START_TIME)
        self.results = []

    def get_broker_utility(self):
        return self.trade_utility

    def get_thread_info(self):
        return self.exit_thread

    # ------------------------------------------------------------------
    # LIVE thread handlers
    # ------------------------------------------------------------------
    def pre_requisite_thread_handler(self):
        print(self.logic_name, ": Inside pre-requisite thread")
        broker = self.trade_utility.get_broker_utility()
        # NIFTY here trades MONTHLY futures, not weekly -- reuse the same resolution the backtest
        # engine uses (incl. its last-week-of-month rollover to next month's contract) so live and
        # backtest can never disagree on which contract is "front month" on a given day.
        today_str = date.today().strftime("%Y-%m-%d")
        self.future_symbol, self.current_expiry = pattern_rules.resolve_front_month_future_symbol(
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

            self.__process_new_candles_live()

            for ds in self.directions.values():
                if ds.state == STATE_SEEK_CONFIRM_ENTRY:
                    self.__check_entry_trigger_live(ds)
                elif ds.state == STATE_IN_TRADE:
                    self.__check_exit_hits_live(ds)

            time.sleep(3)

        for ds in self.directions.values():
            if ds.state == STATE_IN_TRADE:
                self.__finalize_trade_at_eod_live(ds)

        print(self.logic_name, ": Execution loop ended")

    def exit_execution_thread(self):
        print(self.logic_name, ": Start of Exiting Thread")
        self.execute_thread.join()
        self.quotes_utility.stop()
        self.quotes_utility.get_thread_info().join()
        print(self.logic_name, ": End of Exiting Thread")

    # ------------------------------------------------------------------
    # LIVE candle/tick sourcing
    # ------------------------------------------------------------------
    def __process_new_candles_live(self):
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
        if candle_data is not None and len(candle_data) > 0:
            # Zebu's "intvwap" field is a per-candle (interval) VWAP, not a cumulative session VWAP
            # from day open -- confirmed by its volatility mirroring price itself rather than
            # smoothing out as the session progresses. The Piercing/Reclaim pattern needs the real
            # cumulative session VWAP, so always compute it ourselves rather than trusting intvwap.
            candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]

            self.last_candle_data = candle_data

            new_rows = candle_data.iloc[self.processed_candle_count:]
            for _, row in new_rows.iterrows():
                for ds in self.directions.values():
                    if ds.state == STATE_SEEK_PIERCING:
                        ts = str(row[DATE_TIME])
                        self.__check_seek_piercing(ds, row, ts)

            self.processed_candle_count = len(candle_data)

        # Reclaim is checked against 1-min candles (finer granularity than
        # candle_interval_minutes) so a reclaim isn't missed/delayed by waiting for the next full
        # main-interval candle to close -- but still measured against the main interval's own
        # VWAP (self.last_candle_data), not a separate 1-min VWAP.
        candle_data_1min = broker.fetchOHLC(self.future_symbol, str_from_date, str_to_date,
                                            interval="1minute",
                                            all_data=True, market_type=self.FUTURE_MARKET_TYPE)
        if candle_data_1min is not None and len(candle_data_1min) > 0:
            new_rows_1min = candle_data_1min.iloc[self.processed_candle_count_1min:]
            for _, row in new_rows_1min.iterrows():
                for ds in self.directions.values():
                    if ds.state == STATE_SEEK_RECLAIM:
                        self.__on_reclaim_1min_close_live(ds, row)

            self.processed_candle_count_1min = len(candle_data_1min)

    def __on_reclaim_1min_close_live(self, ds: _DirectionState, row):
        # row is a 1-min candle; vwap is looked up from the main-interval series so reclaim is
        # still measured against the same VWAP the rest of the pattern uses.
        current_vwap = self.__get_latest_vwap()
        if current_vwap is None:
            return
        ts = str(row[DATE_TIME])
        if self.__check_reclaim(ds, row, current_vwap, ts):
            return
        # Not reclaimed yet -- keep waiting on subsequent 1-min candles rather than abandoning
        # after just one miss. The piercing candle stays the reference point.
        print(self.logic_name, f": [{ts}] ({ds.direction}) no reclaim yet, still waiting {self.__fmt_candle(row, current_vwap)}")

    def __check_entry_trigger_live(self, ds: _DirectionState):
        quote_data = self.quotes_utility.get_quote_data()
        if self.future_symbol not in quote_data:
            return
        ltp = quote_data[self.future_symbol].ltp
        current_vwap = self.__get_latest_vwap()
        if current_vwap is None:
            return
        now_str = datetime.now().strftime("%H:%M:%S")
        triggered = pattern_rules.is_vwap_reentry_triggered(ltp, current_vwap, ds.direction)
        if triggered:
            # a real event -- always print, not subject to the once-per-candle throttle below.
            print(self.logic_name, f": [{now_str}] ({ds.direction}) waiting for entry: LTP={ltp} vs VWAP={current_vwap:.2f} -> TRIGGERED")
        else:
            self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) waiting for entry: LTP={ltp} vs VWAP={current_vwap:.2f}")
        if not triggered:
            return
        self.__enter_trade(ds, ltp, now_str)

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

    def __check_exit_hits_live(self, ds: _DirectionState):
        quote_data = self.quotes_utility.get_quote_data()
        if self.future_symbol not in quote_data:
            return
        future_ltp = quote_data[self.future_symbol].ltp
        option_ltp = quote_data[ds.option_symbol].ltp if ds.option_symbol in quote_data else ds.current_trade.entry_option_price
        now_str = datetime.now().strftime("%H:%M:%S")

        self.__update_mae_mfe_point(ds, future_ltp, now_str)

        # any trade still open at 14:50 is force-closed at the prevailing price, regardless of
        # SL/Exit-1..4 state.
        if pattern_rules.is_force_exit_time_reached(now_str):
            self.__finalize_trade_at_eod_live(ds, "Force Exit 14:50")
            return

        was_hit = self.__snapshot_exit_hits(ds.current_trade)

        self.__mark_exit_if_hit_point(ds, ds.current_trade.sl_hit, ds.sl_level, future_ltp, option_ltp, now_str, is_stop=True)
        self.__mark_exit_if_hit_point(ds, ds.current_trade.exit1_hit, ds.exit1_level, future_ltp, option_ltp, now_str)
        self.__mark_exit_if_hit_point(ds, ds.current_trade.exit2_hit, ds.exit2_level, future_ltp, option_ltp, now_str)
        self.__mark_exit_if_hit_point(ds, ds.current_trade.exit3_hit, ds.exit3_level, future_ltp, option_ltp, now_str)

        bollinger_level = self.__get_bollinger_exit_level_live(ds)
        bollinger_str = f"{bollinger_level:.2f}" if bollinger_level is not None else "n/a"
        if bollinger_level is not None:
            self.__mark_exit_if_hit_point(ds, ds.current_trade.exit4_hit, bollinger_level, future_ltp, option_ltp, now_str)

        self.__log_exit_breaches(ds, was_hit)

        self.__log_once_per_candle(ds, f": [{now_str}] ({ds.direction}) in-trade LTP={future_ltp} "
             f"({self.__fmt_option_price(option_ltp)}) SL={ds.sl_level} "
             f"Exit1={ds.exit1_level:.2f} Exit2={ds.exit2_level:.2f} Exit3={ds.exit3_level:.2f} "
             f"Exit4(Bollinger)={bollinger_str}")

        # SL is the one real exit here. Once it fires, the position is closed for real, so log
        # the trade now and go back to scanning for the next Piercing setup -- not capped at
        # one trade per day.
        if ds.current_trade.sl_hit.is_hit:
            self.__stamp_mae_mfe(ds)
            self.__finalize_and_reset_live(ds, "SL")

    def __mark_exit_if_hit_point(self, ds: _DirectionState, exit_hit_obj: exit_hit, level, future_ltp, option_ltp, now_str, is_stop=False):
        # LIVE checks a single LTP point against the level (tick-driven; no High/Low range).
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

    def __get_bollinger_exit_level_live(self, ds: _DirectionState):
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

    def __finalize_trade_at_eod_live(self, ds: _DirectionState, reason="EOD"):
        quote_data = self.quotes_utility.get_quote_data()
        future_ltp = quote_data[self.future_symbol].ltp if self.future_symbol in quote_data else ds.current_trade.entry_future_price
        option_ltp = quote_data[ds.option_symbol].ltp if ds.option_symbol in quote_data else ds.current_trade.entry_option_price

        ds.current_trade.exit5_eod.future_price = future_ltp
        ds.current_trade.exit5_eod.option_price = option_ltp
        self.__stamp_mae_mfe(ds)

        self.__finalize_and_reset_live(ds, reason)

    def __finalize_and_reset_live(self, ds: _DirectionState, reason="EOD"):
        self.obj_paper_trade_writer.write_trade(ds.current_trade, self.candle_interval_minutes,
                                                describe_exit_outcomes(ds.current_trade))
        close_option_price = ds.current_trade.sl_hit.option_price if reason == "SL" \
            else ds.current_trade.exit5_eod.option_price
        print(self.logic_name, f": ({ds.direction}) Trade closed ({reason}) @ {self.__fmt_option_price(close_option_price)}, logged to PaperTradeData:",
             ds.current_trade.trade_type, ds.current_trade.option_name)
        ds.current_trade = None
        ds.option_symbol = ""
        ds.piercing_candle = None
        ds.reclaim_candle = None
        ds.last_logged_candle_ts = None
        # if the piercing window is already closed (past 14:30 or before start+15min at EOD),
        # this just idles in SEEK_PIERCING harmlessly -- __check_seek_piercing gates on the window.
        ds.state = STATE_SEEK_PIERCING

    def __option_market_type(self):
        broker = self.trade_utility.get_broker_utility() if self.mode == Mode.LIVE else self.broker
        return getattr(broker, "OPTION_MARKET_TYPE", self.OPTION_MARKET_TYPE)

    def __select_option_by_premium(self, broker, option_type):
        # Fyers' real getOptionChain() returns strike/type/live-premium for every contract around
        # the current ATM in one call (no separate get_quotes step needed, unlike the old
        # per-strike-symbol + batch-quote approach).
        underlying = CHAIN_UNDERLYING_SYMBOL.get(self.index_name, self.index_name)
        chain_df, _, _ = broker.getOptionChain(underlying)
        return select_cheapest_in_band(chain_df, option_type, self.target_premium_low, self.target_premium_high)

    def __select_historical_option(self, entry_future_price, option_type, ts):
        # BACKTEST only: there's no historical equivalent of getOptionChain (it only ever answers
        # "what's the premium right now"), so the ~40 candidate strikes around ATM have to be
        # checked individually via their own historical 1-min close near the entry minute.
        # NIFTY options here trade WEEKLY (unlike the future, which is monthly) -- resolve the
        # week's expiry as of the historical trade date, not the future's monthly expiry.
        atm_strike = round(entry_future_price / self.strike_step) * self.strike_step
        trade_date = datetime.strptime(self.trade_date_str, "%Y-%m-%d")
        weekly_expiry = generate_weekly_expiry_dates(trade_date, 1)[0]

        best_symbol, best_price = None, 0.0
        for offset in range(-20, 21):
            strike = atm_strike + offset * self.strike_step
            symbol = self.broker.get_option_name(self.index_name, weekly_expiry, False, str(strike), option_type)
            price = self.__historical_option_close_near(symbol, ts)
            if price is None or price <= 0:
                continue
            if self.target_premium_low <= price <= self.target_premium_high:
                if best_symbol is None or price < best_price:
                    best_symbol, best_price = symbol, price
        return best_symbol, best_price

    def __historical_option_close_near(self, option_symbol, ts):
        # BACKTEST only: the Close of the option's own 1-min candle nearest (at or before) ts --
        # "nearest" here, not an exact real-time LTP, since no intrabar ticks exist historically.
        if not option_symbol:
            return None
        str_from = f"{self.trade_date_str} {DAY_START_TIME}"
        # entry selection alone fires ~40 of these back-to-back (one per candidate strike) --
        # paced to avoid tripping the broker's per-second rate limit (seen in practice: Fyers
        # returning HTTP 429 "request limit reached" without this).
        time.sleep(HISTORICAL_OPTION_LOOKUP_DELAY_SECONDS)
        data = self.broker.fetchOHLC(option_symbol, str_from, ts, interval="1minute",
                                     all_data=True, market_type=self.__option_market_type())
        if data is None or len(data) == 0:
            return None
        # Some brokers (Fyers confirmed) only support DATE-granularity historical range filters
        # and silently ignore the time-of-day portion of str_to_date -- returning the WHOLE day's
        # candles regardless of ts. Trusting the broker to have already cut it off at ts would
        # (and did) return the same end-of-day candle for every lookup on a given day, no matter
        # when ts actually was. Filter down to the candle nearest (at or before) ts ourselves.
        filtered = data[data[DATE_TIME].astype(str) <= ts]
        if len(filtered) == 0:
            return None
        return float(filtered.iloc[-1][CLOSE_PRICE])

    def __is_piercing_window_open(self):
        return pattern_rules.is_piercing_window_open(datetime.now().strftime("%H:%M:%S"),
                                                      self.piercing_start_time)

    def __is_time_reached(self, str_time):
        target_time = datetime.strptime(str_time, "%H:%M:%S").time()
        return datetime.now().time() >= target_time

    # ------------------------------------------------------------------
    # BACKTEST driver
    # ------------------------------------------------------------------
    def run_backtest_day(self):
        """
        Returns (list_of_paper_trade_row, future_symbol, status) for one historical trading day.
        Mirrors the live engine's state machine exactly (same shared handler methods below) but
        replays a single pre-fetched day of candles in one pass instead of polling threads.
        """
        broker = self.broker
        future_symbol, _ = pattern_rules.resolve_front_month_future_symbol(broker, self.index_name, self.trade_date_str)
        self.future_symbol = future_symbol

        str_from_date = f"{self.trade_date_str} {DAY_START_TIME}"
        str_to_date = f"{self.trade_date_str} 15:30:00"
        candle_data = broker.fetchOHLC(future_symbol, str_from_date, str_to_date,
                                       interval=f"{self.candle_interval_minutes}minute",
                                       all_data=True, market_type="FUT")
        if candle_data is None or len(candle_data) == 0:
            return [], future_symbol, STATUS_NO_DATA

        # Zebu's "intvwap" field is a per-candle (interval) VWAP, not a cumulative session VWAP
        # from day open -- always compute the real cumulative VWAP ourselves instead of trusting it.
        candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]
        self.last_candle_data = candle_data

        # Reclaim is checked against 1-min candles (finer granularity than
        # candle_interval_minutes) so a reclaim isn't missed/delayed by waiting for the next full
        # main-interval candle to close -- but still measured against the main interval's own
        # VWAP, not a separate 1-min VWAP. Falls back to the main-interval series (old
        # per-main-candle behavior) if 1-min data isn't available.
        candle_data_1min = broker.fetchOHLC(future_symbol, str_from_date, str_to_date,
                                            interval="1minute", all_data=True, market_type="FUT")
        if candle_data_1min is None or len(candle_data_1min) == 0:
            candle_data_1min = candle_data

        # Bollinger bands are rolling (causal, only look back), so precomputing over the whole day
        # upfront and indexing by row is equivalent to recomputing fresh at each candle -- no
        # lookahead bias.
        self.upper_band, self.middle_band, self.lower_band = compute_bollinger_bands(
            candle_data, period=self.bollinger_period, std_dev=self.bollinger_std_dev)

        one_min_idx = 0

        for i in range(len(candle_data)):
            row = candle_data.iloc[i]
            ts = str(row[DATE_TIME])
            self._backtest_row_index = i

            # 1-min candles closing within this main-interval candle's window, consumed in
            # order -- used for the reclaim check below at finer granularity than
            # candle_interval_minutes.
            sub_rows = []
            while one_min_idx < len(candle_data_1min) and str(candle_data_1min.iloc[one_min_idx][DATE_TIME]) <= ts:
                sub_rows.append(candle_data_1min.iloc[one_min_idx])
                one_min_idx += 1

            for direction, ds in self.directions.items():
                if ds.state == STATE_SEEK_PIERCING:
                    self.__check_seek_piercing(ds, row, ts)

                elif ds.state == STATE_SEEK_RECLAIM:
                    self.__check_reclaim_backtest(ds, row, sub_rows, ts)

                elif ds.state == STATE_SEEK_CONFIRM_ENTRY:
                    self.__check_confirm_entry_backtest(ds, row, sub_rows, ts)

                elif ds.state == STATE_IN_TRADE:
                    self.__check_in_trade_backtest(ds, row, ts)

        # end of day: any direction still IN_TRADE (SL never hit) gets its exit5_eod stamped with
        # the day's last close, mirroring the live engine's EOD finalize.
        for direction, ds in self.directions.items():
            if ds.state == STATE_IN_TRADE and ds.current_trade is not None:
                trade = ds.current_trade
                last_ts = str(candle_data.iloc[-1][DATE_TIME])
                last_close = float(candle_data.iloc[-1][CLOSE_PRICE])
                trade.exit5_eod.future_price = last_close
                trade.exit5_eod.option_price = self.__historical_option_close_near(ds.option_symbol, last_ts) or 0.0
                trade.mae, trade.mae_time = ds.mae, ds.mae_time
                trade.mfe, trade.mfe_time = ds.mfe, ds.mfe_time
                self.results.append(trade)
                self.__log(f"End of day ({direction}): trade still open (SL not hit), closed at last price "
                          f"{last_close} ({self.__fmt_option_price(trade.exit5_eod.option_price)})")

        return self.results, future_symbol, STATUS_OK

    def __check_reclaim_backtest(self, ds: _DirectionState, row, sub_rows, ts):
        for r in sub_rows:
            if self.__check_reclaim(ds, r, row[VWAP], str(r[DATE_TIME])):
                return
        # Not reclaimed yet -- keep waiting on subsequent candles rather than abandoning after
        # just one miss. The piercing candle stays the reference point.
        self.__log(f"[{ts}] ({ds.direction}) no reclaim yet, still waiting {self.__fmt_candle(row)}")

    def __check_confirm_entry_backtest(self, ds: _DirectionState, row, sub_rows, ts):
        # entry fires when this candle's Close crosses back through VWAP in the piercing
        # direction (BUY: back above VWAP; SELL: back below VWAP) -- no invalidation here, the
        # setup just keeps waiting, candle after candle, until this happens or EOD.
        triggered = pattern_rules.is_vwap_reentry_triggered(row[CLOSE_PRICE], row[VWAP], ds.direction)
        self.__log(f"[{ts}] ({ds.direction}) waiting for entry: close={row[CLOSE_PRICE]} vs VWAP={row[VWAP]:.2f} "
                  f"{self.__fmt_candle(row)} {'-> TRIGGERED' if triggered else ''}")
        if not triggered:
            return

        # entry price itself is read from the 1-min candle aligned with this main-interval
        # candle's close (finer-grained than the main-interval Close), same source reclaim uses
        # -- everything else about this trade (timestamp, confirm_candle, etc.) stays keyed to
        # the main-interval candle.
        entry_price = float(sub_rows[-1][CLOSE_PRICE]) if sub_rows else float(row[CLOSE_PRICE])
        self.__enter_trade(ds, entry_price, ts, main_row=row)

    def __check_in_trade_backtest(self, ds: _DirectionState, row, ts):
        trade = ds.current_trade
        high = float(row[HIGH_PRICE])
        low = float(row[LOW_PRICE])
        self.__update_mae_mfe_range(ds, high, low, ts)

        # any trade still open at 14:50 is force-closed at this candle's Close, regardless of
        # SL/Exit-1..4 state.
        if pattern_rules.is_force_exit_time_reached(pattern_rules.time_of_day(ts)):
            close_price = float(row[CLOSE_PRICE])
            trade.exit5_eod.future_price = close_price
            trade.exit5_eod.option_price = self.__historical_option_close_near(ds.option_symbol, ts) or 0.0
            self.__stamp_mae_mfe(ds)
            self.results.append(trade)
            self.__log(f"[{ts}] ({ds.direction}) force-exit (14:50 cutoff) @ {close_price} "
                      f"({self.__fmt_option_price(trade.exit5_eod.option_price)}) -- trade closed, resuming scan")
            self.__reset_direction_backtest(ds)
            return

        was_hit = self.__snapshot_exit_hits(trade)

        self.__mark_exit_if_hit_range(ds, trade.sl_hit, ds.sl_level, row, ds.direction, ts, is_stop=True)
        self.__mark_exit_if_hit_range(ds, trade.exit1_hit, ds.exit1_level, row, ds.direction, ts)
        self.__mark_exit_if_hit_range(ds, trade.exit2_hit, ds.exit2_level, row, ds.direction, ts)
        self.__mark_exit_if_hit_range(ds, trade.exit3_hit, ds.exit3_level, row, ds.direction, ts)

        # Exit-4 target: Bollinger upper band for BUY, lower band for SELL -- price reaching
        # the band in the trade's favor, same target-style semantics as Exit-1..3 -- but it's a
        # hypothesis only, same as Exit-1..3: breaching it is logged, not a real exit.
        bollinger_level = self.__get_bollinger_exit_level_backtest(ds)
        bollinger_str = f"{bollinger_level:.2f}" if bollinger_level is not None else "n/a"
        if bollinger_level is not None:
            self.__mark_exit_if_hit_range(ds, trade.exit4_hit, bollinger_level, row, ds.direction, ts)

        self.__log_exit_breaches(ds, was_hit)

        self.__log(f"[{ts}] ({ds.direction}) in-trade {self.__fmt_candle(row)} SL={ds.sl_level} Exit4(Bollinger)={bollinger_str}")

        if trade.sl_hit.is_hit:
            self.__stamp_mae_mfe(ds)
            self.results.append(trade)
            self.__log(f"[{ts}] ({ds.direction}) SL hit @ {ds.sl_level} "
                      f"({self.__fmt_option_price(trade.sl_hit.option_price)}) -- trade closed, resuming scan")
            self.__reset_direction_backtest(ds)

    def __get_bollinger_exit_level_backtest(self, ds: _DirectionState):
        i = self._backtest_row_index
        band = self.upper_band.iloc[i] if ds.direction == "BUY" else self.lower_band.iloc[i]
        if band != band:  # NaN check -- first BOLLINGER_PERIOD candles have no band yet
            return None
        return float(band)

    def __reset_direction_backtest(self, ds: _DirectionState):
        ds.current_trade = None
        ds.option_symbol = ""
        ds.piercing_candle = None
        ds.reclaim_candle = None
        ds.state = STATE_SEEK_PIERCING

    def __log(self, message):
        self.log_fn(message)

    # ------------------------------------------------------------------
    # shared pattern-transition logic (identical for both modes)
    # ------------------------------------------------------------------
    def __check_seek_piercing(self, ds: _DirectionState, row, ts):
        window_open = pattern_rules.is_piercing_window_open(pattern_rules.time_of_day(ts), self.piercing_start_time)
        if not window_open:
            # LIVE polls every candle regardless of the window and always reports "seeking";
            # BACKTEST silently skips pre/post-window candles without a "seeking" line -- both
            # match their pre-existing behavior.
            if self.mode == Mode.LIVE:
                print(self.logic_name, f": [{ts}] ({ds.direction}) seeking piercing {self.__fmt_candle(row)}")
            return
        direction = pattern_rules.piercing_direction(row)
        if direction != ds.direction:
            msg = f"[{ts}] ({ds.direction}) seeking piercing {self.__fmt_candle(row)}"
            if self.mode == Mode.LIVE:
                print(self.logic_name, ":", msg)
            else:
                self.__log(msg)
            return
        ds.piercing_candle = row
        ds.state = STATE_SEEK_RECLAIM
        if self.mode == Mode.LIVE:
            # NOTE: LIVE's original wording puts direction after "PIERCING" (unlike its own
            # "seeking piercing" line, and unlike BACKTEST's "(direction) PIERCING" below) --
            # preserved exactly as-is rather than unified, since this task is about sharing
            # logic, not changing existing log wording.
            print(self.logic_name, f": [{ts}] PIERCING ({ds.direction}) {self.__fmt_candle(row)}")
        else:
            self.__log(f"[{ts}] ({ds.direction}) PIERCING {self.__fmt_candle(row)}")

    def __check_reclaim(self, ds: _DirectionState, row, vwap, ts):
        """True (and transitions state) if this row reclaims; caller logs the miss case."""
        if not pattern_rules.is_reclaimed(row, vwap, ds.direction):
            return False
        ds.reclaim_candle = row
        ds.reclaim_vwap = vwap
        ds.state = STATE_SEEK_CONFIRM_ENTRY
        msg = f"[{ts}] ({ds.direction}) RECLAIM {self.__fmt_candle(row, vwap)}"
        if self.mode == Mode.LIVE:
            print(self.logic_name, ":", msg)
        else:
            self.__log(msg)
        return True

    def __enter_trade(self, ds: _DirectionState, entry_future_price, ts, main_row=None):
        option_symbol, option_price = "", 0.0
        option_type = "CE" if ds.direction == "BUY" else "PE"
        if self.mode == Mode.LIVE:
            broker = self.trade_utility.get_broker_utility()
            option_symbol, option_price = self.__select_option_by_premium(broker, option_type)
            if option_symbol is None:
                print(self.logic_name, f": ({ds.direction}) No option found near target premium band; dropping setup")
                ds.state = STATE_SEEK_PIERCING
                ds.piercing_candle = None
                ds.reclaim_candle = None
                return
        else:
            # BACKTEST: real historical premiums, unlike the live path, aren't available from a
            # single batched call -- resolve the ~40 candidate strikes around ATM the same way
            # live's option chain would, and check each one's own historical 1-min close near the
            # entry minute individually. Unlike LIVE, a miss here does NOT drop the setup -- the
            # trade still proceeds and is logged, just without option pricing (per requirement:
            # this is a reporting enhancement, not a precondition for the trade existing).
            option_symbol, option_price = self.__select_historical_option(entry_future_price, option_type, ts)
            if option_symbol is None:
                option_symbol, option_price = "", 0.0
                self.__log(f"[{ts}] ({ds.direction}) No option found in "
                          f"{self.target_premium_low:.0f}-{self.target_premium_high:.0f} band at entry -- "
                          f"option price data unavailable for this trade")

        trade = paper_trade_row()
        trade.date = date.today().strftime("%Y-%m-%d") if self.mode == Mode.LIVE else self.trade_date_str
        trade.future = self.future_symbol
        trade.option_name = option_symbol
        trade.trade_type = ds.direction
        trade.piercing_candle = self.__to_snapshot(ds.piercing_candle)
        trade.reclaim_candle = self.__to_snapshot(ds.reclaim_candle, ds.reclaim_vwap)
        if self.mode == Mode.LIVE:
            trade.confirm_candle = candle_snapshot(timestamp=ts, open=entry_future_price, high=entry_future_price,
                                                   low=entry_future_price, close=entry_future_price, vwap=0.0)
        else:
            trade.confirm_candle = self.__to_snapshot(main_row)
        trade.entry_future_price = entry_future_price
        trade.entry_option_price = option_price
        trade.entry_timestamp = ts

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

        ds.current_trade = trade
        ds.option_symbol = option_symbol
        ds.mae = 0.0
        ds.mfe = 0.0
        ds.mae_time = ts
        ds.mfe_time = ts
        ds.state = STATE_IN_TRADE

        if self.mode == Mode.LIVE:
            self.quotes_utility.add_stocks([option_symbol], [self.__option_market_type()])
            # reset the once-per-candle throttle on entry so the first in-trade status line isn't
            # suppressed by the candle timestamp already logged during the waiting-for-entry phase.
            ds.last_logged_candle_ts = None
            print(self.logic_name, ": Entered paper trade", ds.direction, option_symbol, "@", option_price)
        else:
            # Exit-4 (Bollinger) isn't fixed at entry like SL/Exit-1..3 -- it moves every candle
            # (checked in __check_in_trade_backtest below). Shown here is just its value at the
            # moment of entry, for visibility.
            entry_bollinger_level = self.__get_bollinger_exit_level_backtest(ds)
            exit4_str = f"{entry_bollinger_level:.2f}" if entry_bollinger_level is not None else "n/a"
            option_str = f"{option_symbol} @{option_price:.2f}" if option_symbol else "none found"
            self.__log(f"[{ts}] ({ds.direction}) CONFIRM/ENTRY @ {entry_future_price} "
                      f"(piercing: {ds.piercing_candle[DATE_TIME]}, reclaim: {ds.reclaim_candle[DATE_TIME]}) "
                      f"SL={ds.sl_level} Exit1={ds.exit1_level:.2f} Exit2={ds.exit2_level:.2f} Exit3={ds.exit3_level:.2f} "
                      f"Exit4(@entry)={exit4_str} Option={option_str}")

    def __update_mae_mfe_point(self, ds: _DirectionState, future_ltp, ts):
        # LIVE: single LTP point excursion from entry, per tick.
        excursion = (future_ltp - ds.current_trade.entry_future_price) if ds.direction == "BUY" \
            else (ds.current_trade.entry_future_price - future_ltp)
        if excursion < ds.mae:
            ds.mae, ds.mae_time = excursion, ts
        if excursion > ds.mfe:
            ds.mfe, ds.mfe_time = excursion, ts

    def __update_mae_mfe_range(self, ds: _DirectionState, high, low, ts):
        # BACKTEST: worst/best excursion across this candle's whole High/Low range, per candle.
        trade = ds.current_trade
        worst = (low - trade.entry_future_price) if ds.direction == "BUY" else (trade.entry_future_price - high)
        best = (high - trade.entry_future_price) if ds.direction == "BUY" else (trade.entry_future_price - low)
        if worst < ds.mae:
            ds.mae, ds.mae_time = worst, ts
        if best > ds.mfe:
            ds.mfe, ds.mfe_time = best, ts

    def __stamp_mae_mfe(self, ds: _DirectionState):
        ds.current_trade.mae = ds.mae
        ds.current_trade.mae_time = ds.mae_time
        ds.current_trade.mfe = ds.mfe
        ds.current_trade.mfe_time = ds.mfe_time

    def __snapshot_exit_hits(self, trade: paper_trade_row):
        # capture before-state so we can log the exact moment each hypothesis first hits -- none
        # of Exit-1..4 stop the trade (only SL does), so without this there's no visibility into
        # when/whether they fired.
        return {
            "Exit1 (Length of Piercing)": trade.exit1_hit.is_hit,
            "Exit2 (0.5%)": trade.exit2_hit.is_hit,
            "Exit3 (0.75%)": trade.exit3_hit.is_hit,
            "Exit4 (Bollinger)": trade.exit4_hit.is_hit,
        }

    def __fmt_option_price(self, price):
        return f"opt@{price:.2f}" if price and price > 0 else "opt@n/a"

    def __log_exit_breaches(self, ds: _DirectionState, was_hit):
        trade = ds.current_trade
        for label, hit_obj in (("Exit1 (Length of Piercing)", trade.exit1_hit),
                               ("Exit2 (0.5%)", trade.exit2_hit),
                               ("Exit3 (0.75%)", trade.exit3_hit),
                               ("Exit4 (Bollinger)", trade.exit4_hit)):
            if was_hit[label] or not hit_obj.is_hit:
                continue
            opt_str = self.__fmt_option_price(hit_obj.option_price)
            if self.mode == Mode.LIVE:
                print(self.logic_name, f": ({ds.direction}) {label} target BREACHED @ {hit_obj.future_price} ({opt_str})",
                     "(hypothesis only -- trade continues, only SL closes it)")
            else:
                self.__log(f"[{hit_obj.timestamp}] ({ds.direction}) {label} target BREACHED @ {hit_obj.future_price} ({opt_str}) "
                          f"(hypothesis only -- trade continues, only SL closes it)")

    def __mark_exit_if_hit_range(self, ds: _DirectionState, exit_hit_obj: exit_hit, level, row, direction, ts, is_stop=False):
        # BACKTEST checks this candle's whole High/Low range against the level (no intrabar
        # ticks available historically) -- this is deliberately unchanged from before the
        # option-price feature: only the *reported* option price is new, not the exit itself.
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
            # nearest available option data (its own 1-min candle's Close near this exit's
            # timestamp) stands in for a real LTP -- there's no intrabar option tick data
            # historically either.
            exit_hit_obj.option_price = self.__historical_option_close_near(ds.option_symbol, ts) or 0.0
            exit_hit_obj.timestamp = ts
            exit_hit_obj.is_hit = True

    def __to_snapshot(self, row, vwap=None):
        # vwap is an explicit override for 1-min reclaim rows, which carry no VWAP of their own
        # -- they're measured against the main-interval candle's VWAP instead.
        v = row[VWAP] if vwap is None else vwap
        return candle_snapshot(timestamp=str(row[DATE_TIME]), open=float(row[OPEN_PRICE]),
                               high=float(row[HIGH_PRICE]), low=float(row[LOW_PRICE]),
                               close=float(row[CLOSE_PRICE]), vwap=float(v))

    def __fmt_candle(self, row, vwap=None):
        v = row[VWAP] if vwap is None else vwap
        if self.mode == Mode.LIVE:
            return (f"O={row[OPEN_PRICE]} H={row[HIGH_PRICE]} L={row[LOW_PRICE]} C={row[CLOSE_PRICE]} "
                   f"VWAP={v:.2f}")
        # BACKTEST: no intrabar ticks are available historically, so LTP is approximated as this
        # candle's Close -- shown explicitly, unlike the live format above.
        return (f"O={row[OPEN_PRICE]} H={row[HIGH_PRICE]} L={row[LOW_PRICE]} C={row[CLOSE_PRICE]} "
               f"LTP={row[CLOSE_PRICE]} VWAP={v:.2f}")


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
    that was hit, plus which one was best, each annotated with the option's own premium at that
    exit -- e.g. "NIFTY2681122050PE entry@105.00 | Exit1:+23.90pts(opt@112.50),
    Exit3:+65.20pts(opt@98.50) (Best: Exit3)". If none of Exit-1..4 were ever hit before the trade
    closed, the exits portion is just "SL". If no option was ever resolved for this trade (e.g.
    backtest couldn't find one in the target premium band), the option prefix is replaced with an
    explicit "[No option data found]" note rather than silently omitted.
    """
    parts = []
    best_label, best_pnl = None, None
    for label, hit_obj in (("Exit1", trade.exit1_hit), ("Exit2", trade.exit2_hit),
                           ("Exit3", trade.exit3_hit), ("Exit4", trade.exit4_hit)):
        if not hit_obj.is_hit:
            continue
        pnl = _exit_pnl_points(trade, hit_obj)
        opt_str = f"(opt@{hit_obj.option_price:.2f})" if hit_obj.option_price > 0 else "(opt@n/a)"
        parts.append(f"{label}:{pnl:+.2f}pts{opt_str}")
        if best_pnl is None or pnl > best_pnl:
            best_label, best_pnl = label, pnl

    exits_desc = f"{', '.join(parts)} (Best: {best_label})" if parts else "SL"

    if trade.option_name:
        return f"{trade.option_name} entry@{trade.entry_option_price:.2f} | {exits_desc}"
    return f"{exits_desc} [No option data found]"
