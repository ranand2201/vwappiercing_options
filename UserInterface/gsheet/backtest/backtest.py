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
            self.gworksheet_backtest.append_table(values=values,
                                                   start='A1', dimension='ROWS', overwrite=False)
        except:
            print("Exception while writing backtest row to BackTestData")
            traceback.print_exc()
