# -*- coding: utf-8 -*-
"""
paper_trade_data.py
"""
from dataclasses import dataclass, field


@dataclass
class candle_snapshot:
    timestamp: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    vwap: float = 0.0

    def to_row(self):
        return [self.timestamp, self.open, self.high, self.low, self.close, self.vwap]


@dataclass
class exit_hit:
    future_price: float = 0.0
    option_price: float = 0.0
    timestamp: str = ""
    is_hit: bool = False

    def to_row(self):
        return [self.future_price, self.option_price, self.timestamp]


@dataclass
class eod_exit:
    future_price: float = 0.0
    option_price: float = 0.0

    def to_row(self):
        return [self.future_price, self.option_price]


@dataclass
class paper_trade_row:
    date: str = ""
    future: str = ""
    option_name: str = ""
    trade_type: str = ""

    piercing_candle: candle_snapshot = field(default_factory=candle_snapshot)
    reclaim_candle: candle_snapshot = field(default_factory=candle_snapshot)
    confirm_candle: candle_snapshot = field(default_factory=candle_snapshot)

    entry_future_price: float = 0.0
    entry_option_price: float = 0.0
    entry_timestamp: str = ""

    sl_hit: exit_hit = field(default_factory=exit_hit)
    exit1_hit: exit_hit = field(default_factory=exit_hit)
    exit2_hit: exit_hit = field(default_factory=exit_hit)
    exit3_hit: exit_hit = field(default_factory=exit_hit)
    exit4_hit: exit_hit = field(default_factory=exit_hit)
    exit5_eod: eod_exit = field(default_factory=eod_exit)

    mae: float = 0.0
    mae_time: str = ""
    mfe: float = 0.0
    mfe_time: str = ""

    def to_sheet_row(self):
        row = [self.date, self.future, self.option_name, self.trade_type]
        row += self.piercing_candle.to_row()
        row += self.reclaim_candle.to_row()
        row += self.confirm_candle.to_row()
        row += [self.entry_future_price, self.entry_option_price, self.entry_timestamp]
        row += self.sl_hit.to_row()
        row += self.exit1_hit.to_row()
        row += self.exit2_hit.to_row()
        row += self.exit3_hit.to_row()
        row += self.exit4_hit.to_row()
        row += self.exit5_eod.to_row()
        row += [self.mae, self.mae_time, self.mfe, self.mfe_time]
        return row
