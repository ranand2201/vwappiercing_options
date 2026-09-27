# -*- coding: utf-8 -*-
"""
order_log_data.py
"""
from dataclasses import dataclass


@dataclass
class order_log_row:
    timestamp: str = ""
    date: str = ""
    future: str = ""
    option_name: str = ""
    trade_type: str = ""
    order_type: str = ""  # "Entry" / "Exit"
    transaction_type: str = ""  # "BUY" / "SELL" -- the literal broker side sent
    quantity: int = 0
    order_id: str = ""
    status: str = ""  # "PLACED" / "FAILED"
    reason: str = ""  # blank for Entry; SL / target_exit label / EOD for Exit
    reference_price: float = 0.0  # option LTP used to select/estimate, not a real fill price

    def to_sheet_row(self):
        return [self.timestamp, self.date, self.future, self.option_name, self.trade_type,
                self.order_type, self.transaction_type, self.quantity, self.order_id,
                self.status, self.reason, self.reference_price]
