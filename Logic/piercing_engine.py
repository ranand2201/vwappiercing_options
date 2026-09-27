# -*- coding: utf-8 -*-
"""
piercing_engine.py

Single engine for the VWAP Piercing Options pattern (Piercing -> Reclaim ->
Confirm/Entry -> SL/Exit-1..4/EOD), used identically by both the live
paper-trade logger and the historical backtest replay. A `mode` (see `Mode`)
tells the engine which of the two it's running as -- LIVE polls a broker's
live quotes/candles on a background thread and writes each completed trade
to PaperTradeData as it closes; BACKTEST replays one historical day's
candles minute by minute through the very same per-pass step LIVE test mode
(executor.py --test_mode true) runs (see __run_pass), so the two produce the
same trades, and returns the list of completed trades for the caller to
write to BackTestData. Everything about the actual trading rules
(piercing/reclaim/entry conditions, SL and Exit-1..4 level formulas, MAE/MFE
tracking, the 14:50 force-exit cutoff, best-case-exit description) lives
here exactly once, so a rule change can't require touching two engines and
drifting between them -- only the handful of genuinely different
touchpoints (candle/tick sourcing, real option quotes vs none, threads vs a
plain loop, log wording/destination) branch on `mode`.

BUY and SELL setups are tracked as two fully independent state machines (see
_DirectionState) so a piercing in one direction is never blocked or lost
while the other direction already has a setup/trade in progress.

The three legs deliberately run on three different timeframes, and both
modes drive them from the same candle series: Piercing on the configurable
main interval (Config.candle_interval), Reclaim on 5-min candles, and the
entry trigger on 1-min candles (see pattern_rules' RECLAIM_/ENTRY_
CANDLE_INTERVAL_MINUTES). Because those candles close at different times,
the sub-candle legs are fed through the state machine in true close-time
order with an explicit ordering guard, so a Reclaim can never be taken from
a candle still forming inside the Piercing candle, nor an entry from a
1-min candle that closed before the Reclaim confirmed.

SL is the ONLY real exit -- Exit-1..4 (Length-of-Piercing, 0.2%, 0.75%,
Bollinger) are all parallel hypotheses tracked for comparison only; breaching
one is logged but doesn't close the trade. Any trade still open at 14:50 is
force-closed at the prevailing price regardless of SL/Exit-1..4 state.
"""
import threading
import time
import traceback
from datetime import datetime, date, timedelta
from enum import Enum

import pandas as pd

from BusinessLogic.interfaces.ILogic import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *
from Utility.utility import compute_vwap, compute_bollinger_bands, get_target_price_by_percentage, generate_weekly_expiry_dates, \
    generate_monthly_expiry_dates
from DataTypes.defines import *
from ..DataTypes.paper_trade_data import paper_trade_row, candle_snapshot, exit_hit
from ..DataTypes.order_log_data import order_log_row
from ..UserInterface.adapter.login.login import *
from ..UserInterface.adapter.config.config import *
from ..UserInterface.gsheet.paper_trade.paper_trade import *
from ..UserInterface.gsheet.order_log.order_log import *
from ..Config.config_loader import load_config
from . import pattern_rules
from .option_selection import select_by_premium, select_cheapest_in_band

# Which paper_trade_row exit_hit field + display label each Config/strategy_config.json
# "target_exit" value maps to. None/unrecognized -> SL is the only real exit (see
# VwapPiercingEngine.__init__ / __configured_real_exit_hit).
TARGET_EXIT_FIELD_MAP = {
    "exit1": ("exit1_hit", "Exit1"),
    "exit2": ("exit2_hit", "Exit2"),
    "exit3": ("exit3_hit", "Exit3"),
    "exit4": ("exit4_hit", "Exit4"),
}


class Mode(Enum):
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


STATE_SEEK_PIERCING = "SEEK_PIERCING"
STATE_SEEK_RECLAIM = "SEEK_RECLAIM"
STATE_SEEK_CONFIRM_ENTRY = "SEEK_CONFIRM_ENTRY"
STATE_IN_TRADE = "IN_TRADE"

STATUS_NO_DATA = "no_data"
STATUS_OK = "ok"

# Day start, test-mode clock, historical-option-lookup pacing, and the Bollinger settings are all
# tunable trading parameters now -- see Config/strategy_config.json, loaded once into
# self.day_start_time / self.test_mode_start_time / self.test_mode_step_seconds /
# self.historical_option_lookup_delay_seconds / self.bollinger_period / self.bollinger_std_dev in
# __init__. (LIVE test mode, executor.py --test_mode true --date ...: the simulated clock starts
# at test_mode_start_time on the test date and runs in fast mode, each pass of the execute loop
# moving it forward by test_mode_step_seconds instead of sleeping, so a whole day replays in
# seconds -- one minute per pass matches the 1-min candles prices are taken from, so no candle is
# skipped.)

# Fyers' real option-chain-query symbol for each index's underlying (confirmed via
# test_fyers_option_chain.py against the live API: "NSE:NIFTY50-INDEX"). getOptionChain()
# prepends the exchange itself, so only the bare symbol goes here.
CHAIN_UNDERLYING_SYMBOL = {"NIFTY": "NIFTY50-INDEX", "BANKNIFTY": "NIFTYBANK-INDEX"}


class _DirectionState:
    """All the mutable pattern/trade-tracking state for one direction (BUY or SELL)."""

    def __init__(self, direction):
        self.direction = direction
        self.state = STATE_SEEK_PIERCING
        self.piercing_candle = None
        self.reclaim_candle = None
        # 5-min reclaim candles carry no VWAP of their own -- the main-interval VWAP they were
        # checked against is tracked separately here.
        self.reclaim_vwap = 0.0
        # LIVE only: ticks are polled every few seconds, but the periodic waiting-for-entry/
        # in-trade status line should only print once per candle close, not every poll -- this
        # tracks the candle timestamp that was last logged for that purpose.
        self.last_logged_candle_ts = None
        # replay (BACKTEST / LIVE test mode): the last 1-min candle run through the in-trade check
        # -- the entry candle right after entry, so exits start on the very next minute.
        self.last_exit_bar_ts = None

        self.current_trade: paper_trade_row = None
        self.option_symbol = ""
        # LIVE real trading only: the quantity a real BUY order for this position was placed for,
        # so the exit's real SELL order squares off exactly what was actually bought.
        self.order_quantity = 0
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

        # Config/strategy_config.json -- the single source of truth for every tunable trading
        # parameter, shared by LIVE and BACKTEST alike (see Config/config_loader.py). Applied to
        # pattern_rules' module-level constants too, so a config change can't apply to only one
        # of the piercing/reclaim/entry predicates and the engine that drives them.
        self.cfg = load_config()
        pattern_rules.configure(self.cfg)

        # strategy constants
        self.index_name = self.cfg["index_name"]
        self.strike_step = self.cfg["strike_step"]
        self.target_premium_low = self.cfg["option_premium_band_low"]
        self.target_premium_high = self.cfg["option_premium_band_high"]
        self.bollinger_period = self.cfg["bollinger_period"]
        self.bollinger_std_dev = self.cfg["bollinger_std_dev"]
        self.day_start_time = self.cfg["day_start_time"]
        self.test_mode_start_time = self.cfg["test_mode_start_time"]
        self.test_mode_step_seconds = self.cfg["test_mode_step_seconds"]
        self.historical_option_lookup_delay_seconds = self.cfg["historical_option_lookup_delay_seconds"]
        self.order_cfg = self.cfg["order"]

        # Real order placement (LIVE only, never test mode/BACKTEST): live_trading_enabled is a
        # master safety switch -- absent/false keeps LIVE fully paper-trade, exactly as before
        # this feature existed, regardless of what target_exit says. SL is ALWAYS a real exit once
        # live trading is enabled; target_exit additionally makes ONE of Exit1-4 real too, so the
        # position closes on whichever of the two actually hits first.
        self.live_trading_enabled = bool(self.cfg.get("live_trading_enabled", False))
        self.target_exit_field, self.target_exit_label = TARGET_EXIT_FIELD_MAP.get(
            (self.cfg.get("target_exit") or "").lower(), (None, None))
        self.exit_labels = {
            "exit1_hit": "Exit1 (Length of Piercing)",
            "exit2_hit": f"Exit2 ({self.cfg['exit2_percent']}%)",
            "exit3_hit": f"Exit3 ({self.cfg['exit3_percent']}%)",
            "exit4_hit": "Exit4 (Bollinger)",
        }

        # pattern state -- BUY and SELL are tracked as two fully independent state machines so a
        # setup in one direction never blocks or gets clobbered by the other.
        self.future_symbol = ""
        self.current_expiry = ""
        self.directions = {"BUY": _DirectionState("BUY"), "SELL": _DirectionState("SELL")}
        self.last_candle_data = None  # main-interval candle series (with VWAP column)
        self.test_mode = False  # LIVE may switch this on from args (see __init_live)

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
        self.obj_order_log_writer = UserInterfaceOrderLog(args.key)

        # executor.py --test_mode true --date YYYY-MM-DD: run against that trading date's candles
        # instead of today's. The clock (start/stop/piercing window) still runs on real time; only
        # the date part changes.
        self.test_mode = bool(getattr(args, "test_mode", False))
        self.session_date_str = args.date if self.test_mode else date.today().strftime("%Y-%m-%d")
        # simulated test-mode clock (see __now / __advance_or_sleep)
        self.test_clock = datetime.strptime(f"{self.session_date_str} {self.test_mode_start_time}",
                                            "%Y-%m-%d %H:%M:%S")
        if self.test_mode:
            print(self.logic_name, ": TEST MODE -- using candles for", self.session_date_str,
                  "with the clock starting at", self.test_mode_start_time)
            # test mode prices everything from the test date's history, reusing the BACKTEST
            # helpers (__select_historical_option etc.), which read these attributes.
            self.broker = self.trade_utility.get_broker_utility()
            self.trade_date_str = self.session_date_str
            self.log_fn = lambda msg: print(self.logic_name, ":", msg)
            self.test_symbol_candles = {}

        self.pre_requisite_complete_event = threading.Event()
        self.day_preset = 0

        self.pre_requisite_start_time = (self.__now() + timedelta(seconds=10)).strftime('%H:%M:%S')
        self.execution_start_time = self.config_data.start_time
        self.execution_stop_time = self.config_data.end_time
        self.candle_interval_minutes = int(self.config_data.candle_interval)
        self.piercing_start_time = pattern_rules.compute_piercing_start_time(self.execution_start_time)

        # one cursor per series: main-interval (piercing), 5-min (reclaim), 1-min (entry).
        self.processed_candle_count = 0
        self.processed_candle_count_reclaim = 0
        self.processed_candle_count_entry = 0
        # test mode only: the whole test day's candles per interval (minutes), fetched once at
        # start-up and then revealed candle by candle as the simulated clock advances.
        self.test_day_candles = {}

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
        self.piercing_start_time = pattern_rules.compute_piercing_start_time(self.day_start_time)
        self.results = []
        # replay state, same as LIVE test mode's (see __init_live / __run_pass)
        self.session_date_str = trade_date_str
        self.processed_candle_count = 0
        self.processed_candle_count_reclaim = 0
        self.processed_candle_count_entry = 0
        self.test_day_candles = {}

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
        self.future_symbol, self.current_expiry = pattern_rules.resolve_front_month_future_symbol(
            broker, self.index_name, self.session_date_str)
        print(self.logic_name, ": Future symbol resolved: ", self.future_symbol)
        # Zebu's fetchOHLC/get_quotes treat market_type="" as "parse this as an option symbol"
        # (see zebumynt_utitlity.fetchOHLC / __get_option_name); a future needs any non-empty,
        # non-"EQ" value so the symbol is used as-is and resolved to NFO.
        self.quotes_utility.add_stocks([self.future_symbol], [self.FUTURE_MARKET_TYPE])
        if self.test_mode:
            for interval_minutes in sorted({self.candle_interval_minutes,
                                            pattern_rules.RECLAIM_CANDLE_INTERVAL_MINUTES,
                                            pattern_rules.ENTRY_CANDLE_INTERVAL_MINUTES}):
                self.__load_test_day_candles(broker, interval_minutes)
        self.pre_requisite_complete_event.set()
        print(self.logic_name, ": Exiting pre-requisite thread")

    def execute(self):
        self.pre_requisite_complete_event.wait()
        print(self.logic_name, ": Execution Started", self.execution_start_time)
        has_started = False
        piercing_window_announced = False

        while not self.__is_time_reached(self.execution_stop_time):
            if not self.__is_time_reached(self.execution_start_time):
                self.__advance_or_sleep(2)
                continue

            if not has_started:
                print(self.logic_name, f": [{self.__now().strftime('%H:%M:%S')}] execution start time reached, beginning candle/tick processing")
                has_started = True
            if not piercing_window_announced and self.__is_piercing_window_open():
                print(self.logic_name, f": [{self.__now().strftime('%H:%M:%S')}] piercing window open (>= {self.piercing_start_time})")
                piercing_window_announced = True

            self.__run_pass(self.__now().strftime("%Y-%m-%d %H:%M:%S"))
            self.__advance_or_sleep(3)

        for ds in self.directions.values():
            if ds.state == STATE_IN_TRADE:
                self.__finalize_trade_at_eod_live(ds)

        print(self.logic_name, ": Execution loop ended")

    def __run_pass(self, now_str):
        """
        One pass of the engine at now_str ("YYYY-MM-DD HH:MM:SS"), shared by the LIVE execute loop
        and the BACKTEST replay (run_backtest_day), so both walk the day through the exact same
        steps in the same order.
        """
        self.__process_new_candles_live(now_str)

        for ds in self.directions.values():
            if ds.state in (STATE_SEEK_RECLAIM, STATE_SEEK_CONFIRM_ENTRY) \
                    and self.__check_abandon_incomplete_setup(ds, now_str):
                continue
            # Reclaim and entry are both candle-close-driven (5-min / 1-min) and are handled
            # in __process_new_candles_live above; only the exit side still needs per-tick
            # polling, since SL/Exit-1..4 are level breaches that can happen intrabar.
            if ds.state == STATE_IN_TRADE:
                if self.__is_replay():
                    self.__check_exit_hits_replay(ds, now_str)
                else:
                    self.__check_exit_hits_live(ds)

    def __is_replay(self):
        # BACKTEST and LIVE test mode both replay a past day's candles on a simulated clock
        return self.mode == Mode.BACKTEST or self.test_mode

    def __broker(self):
        return self.trade_utility.get_broker_utility() if self.mode == Mode.LIVE else self.broker

    def exit_execution_thread(self):
        print(self.logic_name, ": Start of Exiting Thread")
        self.execute_thread.join()
        self.quotes_utility.stop()
        self.quotes_utility.get_thread_info().join()
        print(self.logic_name, ": End of Exiting Thread")

    # ------------------------------------------------------------------
    # LIVE candle/tick sourcing
    # ------------------------------------------------------------------
    def __process_new_candles_live(self, now_str):
        # now_str ("YYYY-MM-DD HH:MM:SS") is the execute loop's timestamp for this pass -- its date is
        # the session date (the --date given in test mode, else today) -- so the candle fetch
        # window and the loop's own checks (e.g. setup abandonment) all use the same moment.
        broker = self.__broker()
        # zebumynt_utitlity.getTimeFrame() returns epoch-second strings (via strftime('%s'), which
        # isn't even portable on Windows) -- but fetchOHLC expects "YYYY-MM-DD HH:MM:SS" and does
        # its own epoch conversion internally, so build the strings directly instead, matching the
        # backtest/dry-run scripts' already-proven-working pattern.
        str_from_date = f"{self.session_date_str} 09:15:00"
        str_to_date = now_str
        candle_data = self.__fetch_candles_live(broker, self.candle_interval_minutes, str_from_date, str_to_date)
        if candle_data is not None and len(candle_data) > 0:
            # Zebu's "intvwap" field is a per-candle (interval) VWAP, not a cumulative session VWAP
            # from day open -- confirmed by its volatility mirroring price itself rather than
            # smoothing out as the session progresses. The Piercing/Reclaim pattern needs the real
            # cumulative session VWAP, so always compute it ourselves rather than trusting intvwap.
            # Replay already carries it: computed once over the whole day (__add_session_vwap),
            # which is identical for every closed prefix since the VWAP is cumulative.
            if not self.__is_replay():
                self.__add_session_vwap(candle_data)

            self.last_candle_data = candle_data

            new_rows = candle_data.iloc[self.processed_candle_count:]
            for _, row in new_rows.iterrows():
                for ds in self.directions.values():
                    if ds.state == STATE_SEEK_PIERCING:
                        ts = str(row[DATE_TIME])
                        self.__check_seek_piercing(ds, row, ts)

            self.processed_candle_count = len(candle_data)

        # Reclaim runs on 5-min candles and the entry trigger on 1-min candles, both independent
        # of candle_interval_minutes -- but both still measured against the main interval's own
        # session VWAP (self.last_candle_data), not a separate per-series VWAP. When the main
        # interval already IS 5 min, the series just fetched above is the reclaim series; reuse it
        # rather than asking the broker for the same candles twice.
        if self.candle_interval_minutes == pattern_rules.RECLAIM_CANDLE_INTERVAL_MINUTES:
            candle_data_reclaim = candle_data
        else:
            candle_data_reclaim = self.__fetch_candles_live(
                broker, pattern_rules.RECLAIM_CANDLE_INTERVAL_MINUTES, str_from_date, str_to_date)
        candle_data_entry = self.__fetch_candles_live(
            broker, pattern_rules.ENTRY_CANDLE_INTERVAL_MINUTES, str_from_date, str_to_date)

        new_reclaim_rows, new_entry_rows = [], []
        if candle_data_reclaim is not None and len(candle_data_reclaim) > 0:
            new_reclaim_rows = [row for _, row in
                                candle_data_reclaim.iloc[self.processed_candle_count_reclaim:].iterrows()]
            self.processed_candle_count_reclaim = len(candle_data_reclaim)
        if candle_data_entry is not None and len(candle_data_entry) > 0:
            new_entry_rows = [row for _, row in
                              candle_data_entry.iloc[self.processed_candle_count_entry:].iterrows()]
            self.processed_candle_count_entry = len(candle_data_entry)

        # note the cursors advance above regardless of any direction's state -- sub-candles that
        # elapsed while nothing was waiting on them are in the past and must not be replayed into
        # a setup that pierces later.
        current_vwap = self.__get_latest_vwap()
        if current_vwap is None:
            return
        for ds in self.directions.values():
            if ds.state in (STATE_SEEK_RECLAIM, STATE_SEEK_CONFIRM_ENTRY):
                self.__drive_setup_sub_candles(ds, new_reclaim_rows, new_entry_rows, current_vwap)

    def __fetch_candles_live(self, broker, interval_minutes, str_from_date, now_str):
        # Normal LIVE: ask the broker for today's candles up to now. Replay (BACKTEST / test
        # mode): no broker call -- serve the day's candles fetched once up front, only those that
        # have fully closed by the simulated time (a candle starting at T closes at T + interval),
        # so the engine never sees prices from later in the day.
        if not self.__is_replay():
            return broker.fetchOHLC(self.future_symbol, str_from_date, now_str,
                                    interval=f"{interval_minutes}minute",
                                    all_data=True, market_type=self.FUTURE_MARKET_TYPE)
        day = self.test_day_candles.get(interval_minutes)
        if day is None:
            day = self.__load_test_day_candles(broker, interval_minutes)
        return self.__closed_by(day, interval_minutes, now_str)

    def __closed_by(self, day, interval_minutes, now_str):
        # the rows of a test-day candle series that have fully closed by now_str
        if day is None or len(day) == 0:
            return day
        # compare on the session date + candle time-of-day, whatever date format the broker uses
        starts = pd.to_datetime(self.session_date_str + " "
                                + day[DATE_TIME].astype(str).map(pattern_rules.time_of_day))
        closes = starts + pd.Timedelta(minutes=interval_minutes)
        now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
        return day[(closes <= now).values].reset_index(drop=True).copy()

    def __ltp(self, symbol):
        # Price of symbol right now, or None if unavailable. Normal LIVE: the live quote. Test
        # mode: the close of the symbol's last 1-min candle closed by the simulated clock, from
        # its test-day candles (fetched once per symbol) -- never today's live market.
        if not symbol:
            return None
        if not self.test_mode:
            quote = self.quotes_utility.get_quote_data().get(symbol)
            return quote.ltp if quote is not None else None
        now_str = self.__now().strftime("%Y-%m-%d %H:%M:%S")
        broker = self.trade_utility.get_broker_utility()
        if symbol == self.future_symbol:
            rows = self.__fetch_candles_live(broker, 1, None, now_str)
        else:
            if symbol not in self.test_symbol_candles:
                self.test_symbol_candles[symbol] = broker.fetchOHLC(
                    symbol, f"{self.session_date_str} 09:15:00", f"{self.session_date_str} 15:30:00",
                    interval="1minute", all_data=True, market_type=self.__option_market_type())
            rows = self.__closed_by(self.test_symbol_candles[symbol], 1, now_str)
        if rows is None or len(rows) == 0:
            return None
        return float(rows[CLOSE_PRICE].iloc[-1])

    def __load_test_day_candles(self, broker, interval_minutes):
        data = broker.fetchOHLC(self.future_symbol, f"{self.session_date_str} 09:15:00",
                                f"{self.session_date_str} 15:30:00",
                                interval=f"{interval_minutes}minute",
                                all_data=True, market_type=self.FUTURE_MARKET_TYPE)
        if interval_minutes == self.candle_interval_minutes and data is not None and len(data) > 0:
            self.__add_session_vwap(data)
        self.test_day_candles[interval_minutes] = data
        msg = f"loaded {0 if data is None else len(data)} {interval_minutes}-min candles for {self.session_date_str}"
        if self.mode == Mode.LIVE:
            print(self.logic_name, ": TEST MODE --", msg)
        else:
            self.__log(msg)
        return data

    def __add_session_vwap(self, candle_data):
        candle_data[VWAP] = [compute_vwap(candle_data, last_loc=i + 1) for i in range(len(candle_data))]

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
        if self.mode == Mode.LIVE:
            print(self.logic_name, message)
        else:
            self.__log(message.lstrip(": "))

    def __check_exit_hits_replay(self, ds: _DirectionState, now_str):
        # Replay (BACKTEST and LIVE test mode): every 1-min future candle closed since the last one
        # checked goes through the shared in-trade rule (__in_trade_minute), in order.
        bars = self.__fetch_candles_live(self.__broker(), 1, None, now_str)
        if bars is None or len(bars) == 0:
            return
        if ds.last_exit_bar_ts is not None:
            starts = bars[DATE_TIME].astype(str).map(self.__session_time)
            bars = bars[(starts > self.__session_time(ds.last_exit_bar_ts)).values]
        for _, bar in bars.iterrows():
            bar_ts = ds.last_exit_bar_ts = str(bar[DATE_TIME])
            outcome = self.__in_trade_minute(ds, bar)
            self.__log_once_per_candle(ds, f": [{bar_ts}] ({ds.direction}) in-trade 1-min O={bar[OPEN_PRICE]} "
                                           f"H={bar[HIGH_PRICE]} L={bar[LOW_PRICE]} C={bar[CLOSE_PRICE]} "
                                           f"SL={ds.sl_level} Exit1={ds.exit1_level:.2f} "
                                           f"Exit2={ds.exit2_level:.2f} Exit3={ds.exit3_level:.2f}")
            if outcome is not None:
                self.__close_trade(ds, outcome)
                return

    def __in_trade_minute(self, ds: _DirectionState, bar):
        """
        One 1-min future candle of an open trade, identical in BACKTEST and LIVE test mode: MAE/MFE
        over its High/Low, the 14:50 force-exit at its Close, then SL and Exit-1..4 hit if its
        High/Low touches the level (recorded at the level). Returns "FORCE", "SL", or the
        configured target_exit's label if the trade closed on this candle, else None -- recording
        the closed trade is __close_trade's job. Mirrors LIVE's real SL-or-target_exit close so a
        replay (backtest, or LIVE test mode) previews exactly what real trading would have done --
        no real order is ever placed here, in either mode.
        """
        trade = ds.current_trade
        ts = str(bar[DATE_TIME])
        self.__update_mae_mfe_range(ds, float(bar[HIGH_PRICE]), float(bar[LOW_PRICE]), ts)

        # any trade still open at 14:50 is force-closed at this candle's Close, regardless of
        # SL/Exit-1..4 state.
        if pattern_rules.is_force_exit_time_reached(pattern_rules.time_of_day(ts)):
            trade.exit5_eod.future_price = float(bar[CLOSE_PRICE])
            trade.exit5_eod.option_price = self.__historical_option_close_near(ds.option_symbol, ts) or 0.0
            return "FORCE"

        was_hit = self.__snapshot_exit_hits(trade)
        self.__mark_exit_if_hit_range(ds, trade.sl_hit, ds.sl_level, bar, ds.direction, ts, is_stop=True)
        self.__mark_exit_if_hit_range(ds, trade.exit1_hit, ds.exit1_level, bar, ds.direction, ts)
        self.__mark_exit_if_hit_range(ds, trade.exit2_hit, ds.exit2_level, bar, ds.direction, ts)
        self.__mark_exit_if_hit_range(ds, trade.exit3_hit, ds.exit3_level, bar, ds.direction, ts)
        # Exit-4 target: the Bollinger band known while this minute trades, i.e. from the main
        # candles closed by its start -- a hypothesis only, like Exit-1..3.
        bollinger_level = self.__bollinger_level_at(ds, self.__session_time(ts))
        if bollinger_level is not None:
            self.__mark_exit_if_hit_range(ds, trade.exit4_hit, bollinger_level, bar, ds.direction, ts)
        self.__log_exit_breaches(ds, was_hit)
        if trade.sl_hit.is_hit:
            return "SL"
        if self.__configured_real_exit_hit(trade):
            return self.target_exit_label
        return None

    def __close_trade(self, ds: _DirectionState, outcome):
        # a replayed trade closed on a 1-min candle ("SL", "FORCE", or the configured target
        # exit's label): LIVE test mode writes it to PaperTradeData like any live trade (but never
        # places a real order, regardless of live_trading_enabled -- test mode is a preview only),
        # BACKTEST collects it for the caller.
        self.__stamp_mae_mfe(ds)
        if self.mode == Mode.LIVE:
            reason = "Force Exit 14:50" if outcome == "FORCE" else outcome
            self.__finalize_and_reset_live(ds, reason)
            return
        trade = ds.current_trade
        self.results.append(trade)
        if outcome == "SL":
            self.__log(f"[{trade.sl_hit.timestamp}] ({ds.direction}) SL hit @ {ds.sl_level} "
                      f"({self.__fmt_option_price(trade.sl_hit.option_price)}) -- trade closed, resuming scan")
        elif outcome == "FORCE":
            self.__log(f"[{ds.last_exit_bar_ts}] ({ds.direction}) force-exit (14:50 cutoff) @ "
                      f"{trade.exit5_eod.future_price} ({self.__fmt_option_price(trade.exit5_eod.option_price)}) "
                      f"-- trade closed, resuming scan")
        else:
            hit_obj = getattr(trade, self.target_exit_field)
            self.__log(f"[{hit_obj.timestamp}] ({ds.direction}) {outcome} hit @ {hit_obj.future_price} "
                      f"({self.__fmt_option_price(hit_obj.option_price)}) -- REAL exit, trade closed, resuming scan")
        self.__reset_direction_backtest(ds)

    def __bollinger_level_at(self, ds: _DirectionState, moment):
        # Exit-4 level from the main-interval candles that had fully closed by `moment`
        closed = self.__closed_by(self.last_candle_data, self.candle_interval_minutes,
                                  moment.strftime("%Y-%m-%d %H:%M:%S"))
        if closed is None or len(closed) < self.bollinger_period:
            return None
        upper_band, _, lower_band = compute_bollinger_bands(closed, period=self.bollinger_period,
                                                            std_dev=self.bollinger_std_dev)
        band = upper_band.iloc[-1] if ds.direction == "BUY" else lower_band.iloc[-1]
        if band != band:  # NaN check
            return None
        return float(band)

    def __session_time(self, candle_ts):
        # a candle timestamp as a datetime on the session date, whatever date format the broker uses
        return datetime.strptime(f"{self.session_date_str} {pattern_rules.time_of_day(str(candle_ts))}",
                                 "%Y-%m-%d %H:%M:%S")

    def __check_exit_hits_live(self, ds: _DirectionState):
        future_ltp = self.__ltp(self.future_symbol)
        if future_ltp is None:
            return
        option_ltp = self.__ltp(ds.option_symbol)
        if option_ltp is None:
            option_ltp = ds.current_trade.entry_option_price
        now_str = self.__now().strftime("%H:%M:%S")

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

        # SL is always a real exit; the configured target_exit (if any) additionally closes the
        # trade for real, whichever of the two hits first. Once either fires, the position is
        # closed for real, so log the trade now and go back to scanning for the next Piercing
        # setup -- not capped at one trade per day.
        if ds.current_trade.sl_hit.is_hit:
            self.__stamp_mae_mfe(ds)
            self.__finalize_and_reset_live(ds, "SL")
        elif self.__configured_real_exit_hit(ds.current_trade):
            self.__stamp_mae_mfe(ds)
            self.__finalize_and_reset_live(ds, self.target_exit_label)

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
        future_ltp = self.__ltp(self.future_symbol)
        if future_ltp is None:
            future_ltp = ds.current_trade.entry_future_price
        option_ltp = self.__ltp(ds.option_symbol)
        if option_ltp is None:
            option_ltp = ds.current_trade.entry_option_price

        ds.current_trade.exit5_eod.future_price = future_ltp
        ds.current_trade.exit5_eod.option_price = option_ltp
        self.__stamp_mae_mfe(ds)

        self.__finalize_and_reset_live(ds, reason)

    def __finalize_and_reset_live(self, ds: _DirectionState, reason="EOD"):
        if reason == "SL":
            close_option_price = ds.current_trade.sl_hit.option_price
        elif reason == self.target_exit_label:
            close_option_price = getattr(ds.current_trade, self.target_exit_field).option_price
        else:
            close_option_price = ds.current_trade.exit5_eod.option_price

        # Square off the real position first, if one was actually opened. A failed exit order is
        # the one failure mode that must NOT reset the state machine: if we think we're flat but
        # the real SELL never went through, we'd stop tracking a position we still hold. Keep the
        # trade open and let the next tick's exit check retry this same close.
        if self.live_trading_enabled and not self.test_mode and ds.option_symbol and ds.order_quantity:
            order_placed, _ = self.__place_real_order(ds.option_symbol, "SELL", ds.order_quantity,
                                                       trade_type=ds.direction, order_type="Exit",
                                                       reason=reason, reference_price=close_option_price)
            if not order_placed:
                print(self.logic_name, f": ({ds.direction}) REAL EXIT ORDER FAILED for {ds.option_symbol} "
                     f"({reason}) -- NOT resetting, will retry closing on the next check")
                return

        self.obj_paper_trade_writer.write_trade(ds.current_trade, self.candle_interval_minutes,
                                                describe_exit_outcomes(ds.current_trade))
        print(self.logic_name, f": ({ds.direction}) Trade closed ({reason}) @ {self.__fmt_option_price(close_option_price)}, logged to PaperTradeData:",
             ds.current_trade.trade_type, ds.current_trade.option_name)
        ds.current_trade = None
        ds.option_symbol = ""
        ds.order_quantity = 0
        ds.piercing_candle = None
        ds.reclaim_candle = None
        ds.last_logged_candle_ts = None
        # if the piercing window is already closed (past 14:30 or before start+15min at EOD),
        # this just idles in SEEK_PIERCING harmlessly -- __check_seek_piercing gates on the window.
        ds.state = STATE_SEEK_PIERCING

    def __option_market_type(self):
        broker = self.trade_utility.get_broker_utility() if self.mode == Mode.LIVE else self.broker
        return getattr(broker, "OPTION_MARKET_TYPE", self.OPTION_MARKET_TYPE)

    def __configured_real_exit_hit(self, trade: paper_trade_row):
        # True if the one exit Config/strategy_config.json's target_exit names (if any) has hit --
        # SL is checked separately by the caller and is always real regardless of this.
        if not self.target_exit_field:
            return False
        return getattr(trade, self.target_exit_field).is_hit

    def __order_quantity(self):
        return int(self.order_cfg["lot_size"]) * int(self.order_cfg["lot_count"])

    def __place_real_order(self, symbol, transaction_type, quantity, trade_type="", order_type="",
                            reason="", reference_price=0.0):
        """
        Places a real MARKET order via the broker -- LIVE mode only, and only when
        Config/strategy_config.json's live_trading_enabled is true (checked by callers before
        this is ever reached). Returns (success, order_id); place_order itself already retries
        and returns "" on failure, so no exception handling is needed here beyond a defensive
        catch-all, since a bad symbol/permission error must never be allowed to crash the engine
        mid-trade and leave a real position untracked.

        Every call -- success or failure -- is logged to OrderLog as its own row (trade_type/
        order_type/reason/reference_price are purely for that log; they don't affect placement).
        A failed exit retries on the next tick (see __finalize_and_reset_live), so a stuck exit
        naturally shows up as repeated FAILED rows until one succeeds.
        """
        broker = self.trade_utility.get_broker_utility()
        try:
            order_id = broker.place_order(
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                product=self.order_cfg["product_type"],
                order_type=self.order_cfg["order_type"],
                market_type=self.__option_market_type(),
            )
        except Exception:
            print(self.logic_name, f": REAL ORDER EXCEPTION placing {transaction_type} {quantity} x {symbol}")
            traceback.print_exc()
            order_id = ""
        success = bool(order_id)
        print(self.logic_name, f": REAL ORDER {'PLACED' if success else 'FAILED'} -- "
             f"{transaction_type} {quantity} x {symbol}", f"order_id={order_id!r}" if success else "")
        self.obj_order_log_writer.write_order(order_log_row(
            timestamp=datetime.now().strftime("%H:%M:%S"),
            date=date.today().strftime("%Y-%m-%d"),
            future=self.future_symbol,
            option_name=symbol,
            trade_type=trade_type,
            order_type=order_type,
            transaction_type=transaction_type,
            quantity=quantity,
            order_id=order_id,
            status="PLACED" if success else "FAILED",
            reason=reason,
            reference_price=reference_price,
        ))
        return success, order_id

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
        # the last weekly expiry of a month IS the monthly expiry, and brokers name it with the
        # monthly format (Fyers: NIFTY26SEP23200CE, not NIFTY2692923200CE -- the latter is
        # rejected as "Invalid symbol provided").
        is_month_expiry = weekly_expiry in generate_monthly_expiry_dates(trade_date, 1)

        # probe the ATM strike alone first -- if this whole weekly expiry has since been
        # delisted (confirmed in practice: Fyers returns "Invalid symbol provided" for expired
        # weekly option contracts, not just "no data"), every one of the other ~40 candidates
        # would fail identically. Bail out here instead of grinding through all of them.
        probe_symbol = self.broker.get_option_name(self.index_name, weekly_expiry, is_month_expiry, str(atm_strike), option_type)
        probe_price = self.__historical_option_close_near(probe_symbol, ts)
        if probe_price is None:
            self.__log(f"[{ts}] No historical option data available for expiry {weekly_expiry} "
                      f"(likely delisted) -- skipping the rest of the strike scan for this trade")
            return None, 0.0

        best_symbol, best_price = None, 0.0
        for offset in range(-20, 21):
            strike = atm_strike + offset * self.strike_step
            if offset == 0:
                symbol, price = probe_symbol, probe_price  # already fetched above, don't refetch
            else:
                symbol = self.broker.get_option_name(self.index_name, weekly_expiry, is_month_expiry, str(strike), option_type)
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
        str_from = f"{self.trade_date_str} {self.day_start_time}"
        # entry selection alone fires ~40 of these back-to-back (one per candidate strike) --
        # paced to avoid tripping the broker's per-second rate limit (seen in practice: Fyers
        # returning HTTP 429 "request limit reached" without this).
        time.sleep(self.historical_option_lookup_delay_seconds)
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
        return pattern_rules.is_piercing_window_open(self.__now().strftime("%H:%M:%S"),
                                                      self.piercing_start_time)

    def __is_time_reached(self, str_time):
        target_time = datetime.strptime(str_time, "%H:%M:%S").time()
        return self.__now().time() >= target_time

    def __now(self):
        # LIVE clock: real time, or in test mode the simulated clock, which only moves when the
        # execute loop advances it (fast mode).
        return self.test_clock if self.test_mode else datetime.now()

    def __advance_or_sleep(self, seconds):
        # end of an execute-loop pass: test mode (fast mode) jumps the simulated clock ahead
        # instead of waiting; normal LIVE sleeps.
        if self.test_mode:
            self.test_clock += timedelta(seconds=self.test_mode_step_seconds)
        else:
            time.sleep(seconds)

    # ------------------------------------------------------------------
    # BACKTEST driver
    # ------------------------------------------------------------------
    def run_backtest_day(self):
        """
        Returns (list_of_paper_trade_row, future_symbol, status) for one historical trading day.
        Replays one pre-fetched day through the same __run_pass LIVE test mode uses, on the same
        simulated clock, instead of polling threads -- so it produces the same trades.
        """
        broker = self.broker
        future_symbol, _ = pattern_rules.resolve_front_month_future_symbol(broker, self.index_name, self.trade_date_str)
        self.future_symbol = future_symbol

        # The day's three candle series (main interval for Piercing, 5-min for Reclaim, 1-min for
        # entry and exits), fetched once through the same loader LIVE test mode uses.
        candle_data = self.__load_test_day_candles(broker, self.candle_interval_minutes)
        if candle_data is None or len(candle_data) == 0:
            return [], future_symbol, STATUS_NO_DATA
        for interval_minutes in (pattern_rules.RECLAIM_CANDLE_INTERVAL_MINUTES,
                                 pattern_rules.ENTRY_CANDLE_INTERVAL_MINUTES):
            if interval_minutes not in self.test_day_candles:
                self.__load_test_day_candles(broker, interval_minutes)

        # Replay the day on the same simulated clock LIVE test mode runs (start at
        # test_mode_start_time, one test_mode_step_seconds step per pass), through the same
        # __run_pass -- so candles are revealed, and entries/exits decided, at exactly the same
        # moments in both.
        clock = datetime.strptime(f"{self.trade_date_str} {self.test_mode_start_time}", "%Y-%m-%d %H:%M:%S")
        day_end = datetime.strptime(f"{self.trade_date_str} 15:30:00", "%Y-%m-%d %H:%M:%S") \
            + timedelta(seconds=self.test_mode_step_seconds)
        while clock <= day_end:
            self.__run_pass(clock.strftime("%Y-%m-%d %H:%M:%S"))
            clock += timedelta(seconds=self.test_mode_step_seconds)

        # end of day: any direction still IN_TRADE (SL never hit, and no 14:50 candle to force-exit
        # on) gets its exit5_eod stamped with the day's last close.
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
    def __check_abandon_incomplete_setup(self, ds: _DirectionState, ts):
        """
        True (and resets ds) if a setup that pierced but hasn't entered yet (SEEK_RECLAIM /
        SEEK_CONFIRM_ENTRY) is still incomplete at PIERCING_CUTOFF_TIME -- same cutoff as new
        piercing detection, independent of FORCE_EXIT_TIME (which only applies once IN_TRADE).
        """
        if not pattern_rules.is_setup_abandon_time_reached(pattern_rules.time_of_day(ts)):
            return False
        self.__log_event(f"[{ts}] ({ds.direction}) giving up on incomplete setup (still {ds.state}) "
                         f"at cutoff -- resetting to seek piercing")
        ds.state = STATE_SEEK_PIERCING
        ds.piercing_candle = None
        ds.reclaim_candle = None
        return True

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

    def __drive_setup_sub_candles(self, ds: _DirectionState, reclaim_rows, entry_rows, vwap):
        """
        Feeds one batch of 5-min (Reclaim) and 1-min (entry) candles through the
        SEEK_RECLAIM -> SEEK_CONFIRM_ENTRY -> entry legs, in true close-time order rather than
        series by series. That ordering matters: a Reclaim printing part-way through the batch
        has to be able to be followed by an entry on a later 1-min candle from the SAME batch,
        which a series-at-a-time pass would either miss or take out of order. `vwap` is the
        main-interval session VWAP both legs are measured against -- neither sub-series carries
        a VWAP column of its own.

        Shared verbatim by both modes: LIVE passes the candles that are new since the last poll,
        BACKTEST the ones falling inside the current main-interval candle.
        """
        reclaim_interval = pattern_rules.RECLAIM_CANDLE_INTERVAL_MINUTES
        entry_interval = pattern_rules.ENTRY_CANDLE_INTERVAL_MINUTES

        # Reclaim events are listed first so that Python's stable sort breaks a close-time tie
        # (e.g. the 10:39 1-min candle and the 10:35 5-min candle both close at 10:40) in favour
        # of the Reclaim -- matching has_closed_at_or_after, which allows that same tie for entry.
        events = [(pattern_rules.candle_close_time(str(r[DATE_TIME]), reclaim_interval), reclaim_interval, r)
                  for r in reclaim_rows]
        events += [(pattern_rules.candle_close_time(str(r[DATE_TIME]), entry_interval), entry_interval, r)
                   for r in entry_rows]
        events.sort(key=lambda event: event[0])

        for _, interval, row in events:
            ts = str(row[DATE_TIME])
            if interval == reclaim_interval and ds.state == STATE_SEEK_RECLAIM:
                # a 5-min candle still forming inside the piercing candle isn't knowable yet --
                # only candles that close at or after the piercing candle did can reclaim it.
                piercing_ts = str(ds.piercing_candle[DATE_TIME])
                if not pattern_rules.has_closed_at_or_after(ts, reclaim_interval, piercing_ts,
                                                            self.candle_interval_minutes):
                    continue
                # when the main interval IS 5 min the two series are the same one, so the
                # piercing candle itself shows up here as a reclaim candidate -- its own close is
                # what defined the piercing, it can't also be the reclaim of it. (The piercing and
                # reclaim predicates are mutually exclusive anyway, so this never actually fires
                # today; it's here so that stays true if either predicate is ever loosened.)
                if ts == piercing_ts and reclaim_interval == self.candle_interval_minutes:
                    continue
                if self.__check_reclaim(ds, row, vwap, ts):
                    continue
                # Not reclaimed yet -- keep waiting on subsequent 5-min candles rather than
                # abandoning after just one miss. The piercing candle stays the reference point.
                self.__log_event(f"[{ts}] ({ds.direction}) no reclaim yet, still waiting "
                                 f"{self.__fmt_candle(row, vwap)}")
            elif interval == entry_interval and ds.state == STATE_SEEK_CONFIRM_ENTRY:
                # likewise, a 1-min candle that closed before the reclaim candle confirmed can't
                # be the entry trigger for it.
                if not pattern_rules.has_closed_at_or_after(ts, entry_interval,
                                                            str(ds.reclaim_candle[DATE_TIME]),
                                                            reclaim_interval):
                    continue
                self.__check_confirm_entry(ds, row, vwap, ts)

    def __check_confirm_entry(self, ds: _DirectionState, row, vwap, ts):
        """
        The entry leg, on a single 1-min candle: entry fires when its Close crosses back through
        VWAP in the piercing direction (BUY: back above VWAP; SELL: back below VWAP). There's no
        invalidation here -- the setup just keeps waiting, 1-min candle after 1-min candle, until
        this happens, the PIERCING_CUTOFF_TIME abandon kicks in, or the day ends.
        """
        close_price = float(row[CLOSE_PRICE])
        triggered = pattern_rules.is_vwap_reentry_triggered(close_price, vwap, ds.direction)
        self.__log_event(f"[{ts}] ({ds.direction}) waiting for entry: 1min close={close_price} "
                         f"vs VWAP={vwap:.2f} {self.__fmt_candle(row, vwap)}"
                         f"{' -> TRIGGERED' if triggered else ''}")
        if not triggered:
            return

        # LIVE fills at the prevailing LTP -- the 1-min close is what *triggers* the entry, but by
        # the time it's acted on the tradable price is the live one, so that's what gets logged as
        # the entry price. Replay (BACKTEST / test mode) has no intrabar ticks, so the triggering
        # candle's own Close stands in for it.
        entry_price = close_price
        if self.mode == Mode.LIVE and not self.test_mode:
            ltp = self.__ltp(self.future_symbol)
            if ltp is not None:
                entry_price = ltp
        self.__enter_trade(ds, entry_price, ts, entry_row=row, vwap=vwap)

    def __log_event(self, message):
        """Pattern-event logging to whichever sink this mode uses -- stdout for LIVE, log_fn for
        BACKTEST."""
        if self.mode == Mode.LIVE:
            print(self.logic_name, ":", message)
        else:
            self.__log(message)

    def __check_reclaim(self, ds: _DirectionState, row, vwap, ts):
        """True (and transitions state) if this row reclaims; caller logs the miss case."""
        if not pattern_rules.is_reclaimed(row, vwap, ds.direction):
            return False
        ds.reclaim_candle = row
        ds.reclaim_vwap = vwap
        ds.state = STATE_SEEK_CONFIRM_ENTRY
        self.__log_event(f"[{ts}] ({ds.direction}) RECLAIM {self.__fmt_candle(row, vwap)}")
        return True

    def __enter_trade(self, ds: _DirectionState, entry_future_price, ts, entry_row, vwap):
        option_symbol, option_price = "", 0.0
        order_quantity = 0
        option_type = "CE" if ds.direction == "BUY" else "PE"
        # test mode picks the option from the test date's historical premiums, like BACKTEST,
        # since today's live option chain says nothing about that date.
        if self.mode == Mode.LIVE and not self.test_mode:
            broker = self.trade_utility.get_broker_utility()
            option_symbol, option_price = self.__select_option_by_premium(broker, option_type)
            if option_symbol is None:
                print(self.logic_name, f": ({ds.direction}) No option found near target premium band; dropping setup")
                ds.state = STATE_SEEK_PIERCING
                ds.piercing_candle = None
                ds.reclaim_candle = None
                return

            order_quantity = self.__order_quantity()
            if self.live_trading_enabled:
                order_placed, _ = self.__place_real_order(option_symbol, "BUY", order_quantity,
                                                           trade_type=ds.direction, order_type="Entry",
                                                           reference_price=option_price)
                if not order_placed:
                    print(self.logic_name, f": ({ds.direction}) Real BUY order FAILED for {option_symbol} "
                         f"-- dropping setup, no position was opened")
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
        trade.date = self.session_date_str if self.mode == Mode.LIVE else self.trade_date_str
        trade.future = self.future_symbol
        trade.option_name = option_symbol
        trade.trade_type = ds.direction
        trade.piercing_candle = self.__to_snapshot(ds.piercing_candle)
        trade.reclaim_candle = self.__to_snapshot(ds.reclaim_candle, ds.reclaim_vwap)
        # the confirm candle is the 1-min candle whose close triggered the entry, in both modes
        # -- stamped with the main-interval VWAP it was measured against, since a 1-min candle
        # carries no VWAP of its own.
        trade.confirm_candle = self.__to_snapshot(entry_row, vwap)
        trade.entry_future_price = entry_future_price
        trade.entry_option_price = option_price
        trade.entry_timestamp = ts

        piercing_high = float(ds.piercing_candle[HIGH_PRICE])
        piercing_low = float(ds.piercing_candle[LOW_PRICE])
        piercing_length = piercing_high - piercing_low

        if ds.direction == "BUY":
            ds.sl_level = piercing_low
            ds.exit1_level = entry_future_price + piercing_length
            ds.exit2_level = get_target_price_by_percentage(entry_future_price, self.cfg["exit2_percent"], "buy")
            ds.exit3_level = get_target_price_by_percentage(entry_future_price, self.cfg["exit3_percent"], "buy")
        else:
            ds.sl_level = piercing_high
            ds.exit1_level = entry_future_price - piercing_length
            ds.exit2_level = get_target_price_by_percentage(entry_future_price, self.cfg["exit2_percent"], "sell")
            ds.exit3_level = get_target_price_by_percentage(entry_future_price, self.cfg["exit3_percent"], "sell")

        ds.current_trade = trade
        ds.option_symbol = option_symbol
        ds.order_quantity = order_quantity
        ds.mae = 0.0
        ds.mfe = 0.0
        ds.mae_time = ts
        ds.mfe_time = ts
        ds.last_exit_bar_ts = ts
        ds.state = STATE_IN_TRADE

        if self.mode == Mode.LIVE:
            if option_symbol and not self.test_mode:
                self.quotes_utility.add_stocks([option_symbol], [self.__option_market_type()])
            # reset the once-per-candle throttle on entry so the first in-trade status line isn't
            # suppressed by the candle timestamp already logged during the waiting-for-entry phase.
            ds.last_logged_candle_ts = None
            print(self.logic_name, ": Entered paper trade", ds.direction, option_symbol, "@", option_price)
        else:
            # Exit-4 (Bollinger) isn't fixed at entry like SL/Exit-1..3 -- it moves every candle
            # (checked per minute in __in_trade_minute). Shown here is just its value at the
            # moment of entry, for visibility.
            entry_bollinger_level = self.__bollinger_level_at(ds, self.__session_time(ts) + timedelta(minutes=1))
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
        # capture before-state so we can log the exact moment each hypothesis first hits -- only
        # SL, and whichever one (if any) Config/strategy_config.json's target_exit names, actually
        # stop the trade; without this there's no visibility into when/whether the rest fired.
        return {
            field: getattr(trade, field).is_hit
            for field in ("exit1_hit", "exit2_hit", "exit3_hit", "exit4_hit")
        }

    def __fmt_option_price(self, price):
        return f"opt@{price:.2f}" if price and price > 0 else "opt@n/a"

    def __log_exit_breaches(self, ds: _DirectionState, was_hit):
        trade = ds.current_trade
        for field in ("exit1_hit", "exit2_hit", "exit3_hit", "exit4_hit"):
            hit_obj = getattr(trade, field)
            if was_hit[field] or not hit_obj.is_hit:
                continue
            label = self.exit_labels[field]
            opt_str = self.__fmt_option_price(hit_obj.option_price)
            # the one field target_exit names is a REAL exit once live trading is enabled -- the
            # rest stay pure hypotheses regardless (only SL and that one ever close the trade).
            note = "(REAL exit -- this closes the position)" if field == self.target_exit_field \
                else "(hypothesis only -- trade continues, only SL/the configured target exit closes it)"
            if self.mode == Mode.LIVE:
                print(self.logic_name, f": ({ds.direction}) {label} target BREACHED @ {hit_obj.future_price} ({opt_str})",
                     note)
            else:
                self.__log(f"[{hit_obj.timestamp}] ({ds.direction}) {label} target BREACHED @ {hit_obj.future_price} ({opt_str}) "
                          f"{note}")

    def __mark_exit_if_hit_range(self, ds: _DirectionState, exit_hit_obj: exit_hit, level, row, direction, ts, is_stop=False):
        # Replay (BACKTEST and LIVE test mode) checks this 1-min candle's whole High/Low range
        # against the level, recorded at the level -- no intrabar ticks are available historically.
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
