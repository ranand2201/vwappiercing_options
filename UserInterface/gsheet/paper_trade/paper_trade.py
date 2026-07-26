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

    def write_trade(self, p_paper_trade_row: paper_trade_row):
        try:
            self.gworksheet_paper_trade.append_table(values=p_paper_trade_row.to_sheet_row(),
                                                      start='A1', dimension='ROWS', overwrite=False)
        except:
            print("Exception while writing paper trade row to PaperTradeData")
            traceback.print_exc()
