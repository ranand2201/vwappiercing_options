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
            # drift run after run). Find the next empty row explicitly instead, so every row
            # always starts at column A.
            #
            # Scan whole rows, not just column A: the header's second row is blank in column A
            # (its per-candle "Time Stamp/O/H/L/C/VWAP" sub-labels start further right), so a
            # column-A count sees only 1 populated row and sends the first trade on top of that
            # sub-header. Floor at row 3, the first real data row.
            rows = self.gworksheet_paper_trade.get_all_values()
            last_populated_row = max(
                (row_number for row_number, row in enumerate(rows, start=1)
                 if any(str(cell).strip() for cell in row)),
                default=0,
            )
            next_row = max(last_populated_row + 1, 3)
            self.gworksheet_paper_trade.update_values(crange=f"A{next_row}", values=[values], extend=True)
        except:
            print("Exception while writing paper trade row to PaperTradeData")
            traceback.print_exc()
