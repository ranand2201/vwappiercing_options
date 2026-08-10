# -*- coding: utf-8 -*-
"""
backtest.py
"""
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.paper_trade_data import *


class UserInterfaceBackTest:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAPPiercingOptions")
        self.gworksheet_backtest = self.googlesheet_utility.get_work_sheet("BackTestData")

    def write_trade(self, p_trade_row: paper_trade_row, p_interval, p_best_case_exit):
        # Interval + Best Case Exit are BackTestData-only columns (appended at the end) -- not
        # part of paper_trade_row/PaperTradeData's layout.
        try:
            values = p_trade_row.to_sheet_row() + [p_interval, p_best_case_exit]
            # append_table()'s "find the last table and append after it" heuristic drifts further
            # right on every call once any row's data doesn't start at column A (confirmed in
            # practice -- each botched write becomes the next call's "table", compounding the
            # drift run after run). Find the next empty row from column A's own populated count
            # instead, and write there explicitly, so every row always starts at column A.
            next_row = len(self.gworksheet_backtest.get_col(1, include_tailing_empty=False)) + 1
            self.gworksheet_backtest.update_values(crange=f"A{next_row}", values=[values], extend=True)
        except:
            print("Exception while writing backtest row to BackTestData")
            traceback.print_exc()
