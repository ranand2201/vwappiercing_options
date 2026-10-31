# -*- coding: utf-8 -*-
"""
paper_trade.py
"""
import traceback
import threading

from Utility.gsheet_utility import *
from ....DataTypes.paper_trade_data import *


_WRITE_LOCK = threading.Lock()


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
            # Rows 1-2 are headers; column A is the Date field for every trade. Ignore unrelated
            # content elsewhere on the sheet so it cannot push live trades far below the table.
            with _WRITE_LOCK:
                dates = self.gworksheet_paper_trade.get_col(1, include_tailing_empty=False)
                next_row = max(len(dates) + 1, 3)
                self.gworksheet_paper_trade.update_values(crange=f"A{next_row}", values=[values], extend=True)
            return True
        except:
            print("Exception while writing paper trade row to PaperTradeData")
            traceback.print_exc()
            return False
