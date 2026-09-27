# -*- coding: utf-8 -*-
"""
order_log.py
"""
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.order_log_data import *


class UserInterfaceOrderLog:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAPPiercingOptions")
        self.gworksheet_order_log = self.googlesheet_utility.get_work_sheet("OrderLog")

    def write_order(self, p_order_log_row: order_log_row):
        # Same "find the next empty row explicitly" approach as UserInterfacePaperTrade.write_trade
        # -- append_table()'s heuristic drifts once a row doesn't start at column A.
        try:
            values = p_order_log_row.to_sheet_row()
            rows = self.gworksheet_order_log.get_all_values()
            last_populated_row = max(
                (row_number for row_number, row in enumerate(rows, start=1)
                 if any(str(cell).strip() for cell in row)),
                default=0,
            )
            next_row = max(last_populated_row + 1, 2)
            self.gworksheet_order_log.update_values(crange=f"A{next_row}", values=[values], extend=True)
        except:
            print("Exception while writing order log row to OrderLog")
            traceback.print_exc()
