# vwappiercing_options

Paper-trade signal logger for the **VWAP Piercing Options** strategy (see `VWAPPiercingOptions.xlsx` at the
`trading` repo root for the original design spec). Detects a Piercing → Reclaim → Confirm candle pattern on
the NIFTY future (against a running VWAP), and for each entry logs the forward price path to a Google Sheet,
tracking SL plus 4 independent hypothetical exits (Length-of-Piercing, 0.5%, 0.75%, Bollinger Band) and
MAE/MFE.

**No real orders are placed.** This is a signal + logging engine only, meant to compare exit rules before
committing to a live one.

Two ways to run it:
- **Live** (`executor.py`) — polls live/delayed quotes during market hours, logs each completed trade to the
  `PaperTradeData` tab.
- **Backtest** (`run_backtest.py`) — replays historical candles for past trading days, logs each detected
  trade to the `BackTestData` tab. Same pattern-detection engine (`Logic/backtest_engine.py`) also powers
  `tests/test_pattern_dry_run.py`'s verbose trace, so the two can't drift apart.

**BUY and SELL setups run as two fully independent state machines** — a piercing in one direction never
blocks or gets clobbered by the other direction already having a setup or open trade in progress. Both can
be mid-pattern, or both in a trade, at the same time.

Piercing detection only runs between `execution start + 15 min` (lets VWAP settle) and `14:30:00`; an
already-open trade or in-progress setup still runs to its natural conclusion after that cutoff. A piercing
candle only counts once its Open starts at least 5 points clear of VWAP and its Close crosses to more than 6
points past VWAP on the other side; a reclaimed setup only enters once LTP (live) / Close (backtest) clears
back through VWAP by more than 5 points, in the piercing direction. Once pierced, reclaim is sought across as
many subsequent candles as it takes (no abandon-after-one-miss). **SL is the one real exit** — Exit-1..4
(Length-of-Piercing, 0.5%, 0.75%, Bollinger) are parallel hypotheses tracked purely for comparison, not real
closes; breaching one is logged but doesn't close the trade. Once SL hits, that trade is logged and the
engine immediately resumes scanning for the next Piercing setup in that direction — there is no
one-trade-per-day cap. Any trade still open at **14:50** is force-closed at the prevailing price regardless
of SL/Exit-1..4 state (logged to `Exit5 (EOD)`).

This package is meant to live at `Executor_One/BusinessLogic/vwappiercing_options/` inside the parent
`Executor_One` checkout — it imports from sibling packages there (`DataTypes`, `Utility`, `BrokerUtility`,
`BusinessLogic.interfaces`) and won't run standalone outside that tree.

## Layout

```
vwappiercing_options/
├── interfaces.py                  # LogicVwapPiercingOptionsInterface (ILogicInterface impl)
├── run_backtest.py                # backtest runner -- writes to BackTestData, see below
├── Logic/
│   ├── vwap_piercing_options.py   # LogicVwapPiercingOptions -- the live tick-driven engine
│   ├── backtest_engine.py         # candle-driven day replay -- shared by run_backtest.py and
│   │                               #   tests/test_pattern_dry_run.py
│   ├── pattern_rules.py           # pure Piercing/Reclaim/Confirm/window predicates (shared)
│   └── option_selection.py        # premium-nearest-to-band selection (shared w/ tests)
├── DataTypes/
│   └── paper_trade_data.py        # paper_trade_row -- shared row shape for both PaperTradeData
│                                    #   (live engine) and BackTestData (backtest engine)
├── UserInterface/
│   ├── adapter/                   # routes --userinterface to a concrete implementation
│   └── gsheet/                    # pygsheets-backed login / config / paper-trade / backtest I/O
└── tests/                         # standalone diagnostic scripts, see below
```

## Requirements

Everything here runs inside the same Python environment as the rest of `Executor_One`. Third-party packages
touched by this package and its dependencies (`Utility`, `BrokerUtility`, `DataTypes`):

```
pygsheets
pyyaml          # transitive dep of pygsheets/google-auth, not always pulled in automatically
myntapi         # Zebu Mynt broker SDK
nsepython
pandas
numpy
pandas_market_calendars
pyotp
requests
selenium
pyautogui
pynput
matplotlib
```

## Google Sheet setup

Login/config/trade data all come from a Google Sheet named **`VWAPPiercingOptions`**, shared with the
service-account email found in your `--key` json file. It needs 4 tabs:

- **`BrokerData`** — `Field | Value` rows 2-8: Broker, User Id, Password, Api Key, Api Secret Key,
  Phone Number/Mac, TOTP Key/DOB.
- **`Config`** — `Field | Value` pairs: Candle Interval / Start Time / End Time in columns A/B, Test Mode /
  Delta Days in columns C/D. Used by both the live engine and, by default, `run_backtest.py`.
- **`PaperTradeData`** — written by the **live** engine. One row per completed trade (Date, Future, Option
  Name, Trade Type, Piercing/Reclaim/Confirm candle snapshots, Entry, SL/Exit-1..4/EOD hits, MAE/MFE,
  including real option premiums), plus `Interval` and `Best Case Exit` (see below).
- **`BackTestData`** — written by **`run_backtest.py`**. Same row layout as `PaperTradeData` (both use the
  `paper_trade_row` dataclass, `DataTypes/paper_trade_data.py`) — but since a historical replay has no real
  option data, `Option Name`/`Option Price` columns are just left blank/0.0. `Exit5 (EOD)` is populated
  with the day's last close for any trade still open (SL never hit) at end of day.

Both `PaperTradeData` and `BackTestData` end with the same two extra columns: `Interval` (the candle
interval that run used) and `Best Case Exit` — profit/loss in points for every Exit-1..4 that was hit before
the trade closed (e.g. `Exit1:+23.90, Exit3:+65.20 (Best: Exit3)`), or `SL` if none of them were ever hit
(`Logic/backtest_engine.py`'s `describe_exit_outcomes()`, shared by both writers so they can't drift).

See `VWAPPiercingOptions.xlsx` for the original column layout reference (both sheets have since been
extended beyond the simpler layout shown there).

## Running the strategy (via executor.py)

From the `Executor_One` directory:

```
python executor.py --userinterface gsheet --key <path_to_service_account_json> --logic vwap_piercing_options
```

- `--key` — path to the Google service-account json (e.g. `bankniftyorb-2b0a4e15319b.json` at the `trading`
  repo root; from `Executor_One` that's `..\bankniftyorb-2b0a4e15319b.json`).
- `--logic vwap_piercing_options` — selects this strategy from `executor.py`'s `LOGIC_REGISTRY` (the other
  option is `example`, the scaffold template).
- Set `Config.Test Mode = TRUE` on the sheet for a quick end-to-end smoke run (compresses start/end time to
  a few minutes); leave it `FALSE` for a real trading-day run governed by `Config.Start Time`/`End Time`.

## Running a backtest (via run_backtest.py)

From the `Executor_One` directory:

```
python -m BusinessLogic.vwappiercing_options.run_backtest --key ..\bankniftyorb-2b0a4e15319b.json [--days 30] [--interval 5] [--index NIFTY]
```

Or an explicit date range instead of `--days`:

```
python -m BusinessLogic.vwappiercing_options.run_backtest --key ..\bankniftyorb-2b0a4e15319b.json --start-date 2026-06-25 --end-date 2026-07-24 [--interval 5]
```

- `--key` — same service-account json as above.
- `--days` — calendar-day lookback from yesterday (default 30 if neither this nor `--start-date`/
  `--end-date` is given). Only *trading* days in the window are replayed (via `pandas_market_calendars`).
- `--start-date` / `--end-date` — explicit inclusive date range (`YYYY-MM-DD`), used instead of `--days`.
  Must be given together.
- `--interval` — candle interval in minutes; defaults to `Config.candle_interval` on the sheet if omitted.
- `--index` — index name (default `NIFTY`).

**Important, confirmed in testing**: NIFTY here trades **monthly** futures (confirmed via `search_scrip` —
only ~1 contract/month is ever listed, e.g. `NIFTY28JUL26F` / `NIFTY25AUG26F` / `NIFTY29SEP26F`), not
weekly, despite the similar-looking `F`-suffixed naming. The front-month contract for each historical day
is resolved locally (no network call, via `Logic/backtest_engine.py:resolve_front_month_future_symbol`) —
during the **last 7 calendar days of a month**, this rolls forward to next month's contract instead of the
current month's, since liquidity in the current month's contract thins out sharply in its final week as the
market rolls over. The **live engine** (`executor.py`) resolves its future symbol the same way, against
today's date, at startup — so live and backtest can never disagree on which contract is "front month" on a
given day. The broker only retains historical OHLC for contracts that haven't expired yet — once a contract
rolls off, its data is no longer retrievable, regardless of how recently it expired — so any date whose
front-month contract has since expired prints `no candle data -- contract likely expired/delisted, skipping`
rather than failing the whole run.

Each run can write multiple rows per day (one per trade — SL closes a trade and the engine resumes scanning
within the same day), or zero if no valid Piercing/Reclaim/Confirm/entry sequence formed. Re-running is
safe (appends, doesn't dedupe) — clear the `BackTestData` tab first if you want a clean re-run.

## Tests (`tests/`)

Standalone diagnostic scripts — no test framework, just run-and-read-the-output. All bypass the live
threaded engine so they can be run any time, including when the market is closed (most use historical or
last-close data). Run from the `Executor_One` directory as `python -m ...`; all take `--key <path>`.

| Script | What it checks | Command |
|---|---|---|
| `test_read_login_config` | `BrokerData`/`Config` sheet tabs parse correctly | `python -m BusinessLogic.vwappiercing_options.tests.test_read_login_config --key ..\bankniftyorb-2b0a4e15319b.json` |
| `test_broker_login` | Zebu Mynt authentication (no market call) | `python -m BusinessLogic.vwappiercing_options.tests.test_broker_login --key ..\bankniftyorb-2b0a4e15319b.json` |
| `test_search_scrip` | Broker scrip search / token resolution, raw response | `python -m BusinessLogic.vwappiercing_options.tests.test_search_scrip --key ..\bankniftyorb-2b0a4e15319b.json [--symbol NIFTY28JUL26F] [--exchange NFO]` |
| `test_fetch_historical_candles` | Future symbol resolution + `fetchOHLC` (candles + VWAP column) | `python -m BusinessLogic.vwappiercing_options.tests.test_fetch_historical_candles --key ..\bankniftyorb-2b0a4e15319b.json [--date YYYY-MM-DD] [--interval 15]` |
| `test_debug_time_price_series` | Raw broker `get_time_price_series` call (bypasses `fetchOHLC`'s swallowed errors) | `python -m BusinessLogic.vwappiercing_options.tests.test_debug_time_price_series --key ..\bankniftyorb-2b0a4e15319b.json [--date YYYY-MM-DD] [--interval 15]` |
| `test_pattern_dry_run` | Verbose trace of the full Piercing→Reclaim→Confirm→SL/Exit engine (same code `run_backtest.py` uses) against one historical day -- window gating, multi-trade included | `python -m BusinessLogic.vwappiercing_options.tests.test_pattern_dry_run --key ..\bankniftyorb-2b0a4e15319b.json [--date YYYY-MM-DD] [--interval 15]` |
| `test_option_chain_quotes` | `get_option_chain` + `get_quotes` + premium-nearest-to-100-110 selection | `python -m BusinessLogic.vwappiercing_options.tests.test_option_chain_quotes --key ..\bankniftyorb-2b0a4e15319b.json [--strike 24600]` |
| `test_write_sample_trade` | `PaperTradeData` gsheet write path with one fake row (no broker/market call at all) | `python -m BusinessLogic.vwappiercing_options.tests.test_write_sample_trade --key ..\bankniftyorb-2b0a4e15319b.json` |

Suggested order for a fresh setup / before market open: `test_read_login_config` →
`test_write_sample_trade` (validates sheet access end-to-end with zero market dependency) →
`test_broker_login` → `test_fetch_historical_candles` → `test_pattern_dry_run` →
`test_option_chain_quotes`. If any broker-facing script behaves unexpectedly, `test_search_scrip` and
`test_debug_time_price_series` are the two lowest-level tools for seeing raw broker responses.

Once `test_pattern_dry_run` looks right for a given day, `run_backtest.py` (see above) is the way to
actually populate `BackTestData` across a date range rather than eyeballing one day at a time.
