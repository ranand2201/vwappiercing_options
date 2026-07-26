# -*- coding: utf-8 -*-
"""
config.py
"""
import sys
from datetime import datetime, timedelta
sys.path.append('../interfaces')   # path to directory containing helper.py

from BusinessLogic.interfaces.Iuser_Interface import *
from DataTypes.config import *
from Utility.gsheet_utility import *

LOGIC_CANDLE_INTERVAL = 0
LOGIC_START_TIME = 1
LOGIC_START_TIME = 2
TEST_MODE_STATUS = 0
TEST_MODE_START_TIME = 1
TEST_MODE_END_TIME = 2


class UserInterfaceConfig(IUserInterface):

    def __init__(self, key):
        self.data = ConfigData()
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAPPiercingOptions")
        self.gworksheet_config_data = self.googlesheet_utility.get_work_sheet("Config")
        self.__set_data()

    def get_data(self):
        return self.data

    def __set_data(self):
        data = self.gworksheet_config_data.get_all_values()  # All rows as a list of lists
        # rows 2-4, columns B-D (Field|Value pairs: Candle Interval/Start Time/End Time in A/B,
        # Test Mode/Delta Days in C/D)
        subset = [row[1:4] for row in data[1:5]]
        print("subset", subset)
        self.data.test_mode_status = True if subset[0][2] == "TRUE" else False
        self.data.start_time = (datetime.now() + timedelta(seconds=60)).strftime("%H:%M:%S") if self.data.test_mode_status else subset[1][0]
        self.data.end_time = (datetime.now() + timedelta(seconds=2500)).strftime("%H:%M:%S") if self.data.test_mode_status else subset[2][0]
        self.data.start_time = self.data.start_time.replace(" AM", "")
        self.data.end_time = self.data.end_time.replace(" AM", "")
        self.data.start_time = self.data.start_time.replace(" PM", "")
        self.data.end_time = self.data.end_time.replace(" PM", "")
        # NOTE: example_logic's UserInterfaceConfig reads this as subset[0][1] (col C, the
        # "Test Mode" label) which is a pre-existing off-by-one bug there. This sheet's layout
        # puts the actual Candle Interval value in col B (subset[0][0]).
        self.data.candle_interval = subset[0][0]
        self.data.test_mode_delta_days = int(subset[1][2]) if self.data.test_mode_status else 0
        print("data: ", self.data.start_time, self.data.end_time, self.data.candle_interval, self.data.test_mode_delta_days, self.data.test_mode_status)
