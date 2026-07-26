# -*- coding: utf-8 -*-
"""
interfaces.py
"""
from .Logic.vwap_piercing_options import *
from .UserInterface.adapter import *
from ..interfaces.ILogic_Interface import *
from BrokerUtility.pal.utility_manager import *
from Utility.quotes_utility import *


class LogicVwapPiercingOptionsInterface(ILogicInterface):

    def __init__(self):
        self.broker = None
        self.obj_logic = None

    def create(self, args, broker_utility_manager:utility_manager, quotes_utility:QuoteUtility):
        print("Creating VWAP Piercing Options Logic Object")
        self.obj_logic: LogicVwapPiercingOptions = LogicVwapPiercingOptions(args, broker_utility_manager, quotes_utility)

    def wait_for_completion(self):
        print("Wait For Completion", self.obj_logic.__class__.__name__)
        if self.obj_logic:
            print("Before Joining thread")
            self.obj_logic.get_thread_info().join()
            print("After Joining thread")

    def get_broker_utility(self):
        return self.obj_logic.get_broker_utility()
