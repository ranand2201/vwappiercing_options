# -*- coding: utf-8 -*-
"""
paper_trade.py
"""
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.paper_trade_data import *


class UserInterfacePaperTrade:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAPPiercingOptions")
        self.gworksheet_paper_trade = self.googlesheet_utility.get_work_sheet("PaperTradeData")

    def write_trade(self, p_paper_trade_row: paper_trade_row, p_interval, p_best_case_exit):
        # Interval + Best Case Exit mirror the same two extra columns BackTestData has -- appended
        # at the end, same as UserInterfaceBackTest.write_trade.
        try:
            values = p_paper_trade_row.to_sheet_row() + [p_interval, p_best_case_exit]
            # append_table()'s "find the last table and append after it" heuristic drifts further
            # right on every call once any row's data doesn't start at column A (confirmed in
            # practice -- each botched write becomes the next call's "table", compounding the
            # drift run after run). Find the next empty row from column A's own populated count
            # instead, and write there explicitly, so every row always starts at column A.
            next_row = len(self.gworksheet_paper_trade.get_col(1, include_tailing_empty=False)) + 1
            self.gworksheet_paper_trade.update_values(crange=f"A{next_row}", values=[values], extend=True)
        except:
            print("Exception while writing paper trade row to PaperTradeData")
            traceback.print_exc()
