# vwappiercing_options

Paper-trade signal logger for the **VWAP Piercing Options** strategy (see `VWAPPiercingOptions.xlsx` at the
`trading` repo root for the original design spec). Detects a Piercing → Reclaim → Confirm candle pattern on
the NIFTY future (against a running VWAP), and for each entry logs the forward price path to a Google Sheet,
tracking SL plus 4 independent hypothetical exits (Length-of-Piercing, 0.2%, 0.75%, Bollinger Band) and
MAE/MFE.

**Real orders are placed only if you explicitly turn that on.** By default (`live_trading_enabled: false` in
`Config/strategy_config.json`) this is still a pure signal + logging engine, same as always. See
**Configuration** below before ever flipping that switch.

Two ways to run it:
- **Live** (`executor.py`) — polls live/delayed quotes during market hours, logs each completed trade to the
  `PaperTradeData` tab.
- **Backtest** (`run_backtest.py`) — replays historical candles for past trading days, logs each detected
  trade to the `BackTestData` tab. Also powers `tests/test_pattern_dry_run.py`'s verbose trace.

Both run through the exact same state machine, `Logic/piercing_engine.py`'s `VwapPiercingEngine` — a `mode`
flag (`LIVE`/`BACKTEST`) tells it which it's running as, but every actual trading rule (piercing/reclaim/entry
conditions, SL and Exit-1..4 level formulas, MAE/MFE, the 14:50 force-exit cutoff) is defined exactly once, so
changing a rule can't require touching two engines and drifting between them. Only the handful of genuinely
different touchpoints (how candles/ticks are sourced, real option quotes vs none, threads vs a plain replay
loop, log wording/destination) branch on mode.

**BUY and SELL setups run as two fully independent state machines** — a piercing in one direction never
blocks or gets clobbered by the other direction already having a setup or open trade in progress. Both can
be mid-pattern, or both in a trade, at the same time.

Piercing detection only runs between `execution start + 15 min` (lets VWAP settle) and `14:30:00`; an
already-open trade or in-progress setup still runs to its natural conclusion after that cutoff. A piercing
candle only counts once its Open starts at least 5 points clear of VWAP and its Close crosses to more than 6
points past VWAP on the other side; a reclaimed setup only enters once LTP (live) / Close (backtest) clears
back through VWAP by more than 5 points, in the piercing direction. Once pierced, reclaim is sought across as
many subsequent candles as it takes (no abandon-after-one-miss). **SL is always a real exit** — Exit-1..4
(Length-of-Piercing, 0.2%, 0.75%, Bollinger) are parallel hypotheses tracked purely for comparison; breaching
one is logged but doesn't close the trade, unless it's the one named by `Config.target_exit` (see
**Configuration**), in which case it closes the trade for real too, whichever of it or SL hits first. Once
the trade closes, it's logged and the engine immediately resumes scanning for the next Piercing setup in
that direction — there is no one-trade-per-day cap. Any trade still open at **14:50** is force-closed at the
prevailing price regardless of SL/Exit-1..4 state (logged to `Exit5 (EOD)`).

This package is meant to live at `Executor_One/BusinessLogic/vwappiercing_options/` inside the parent
`Executor_One` checkout — it imports from sibling packages there (`DataTypes`, `Utility`, `BrokerUtility`,
`BusinessLogic.interfaces`) and won't run standalone outside that tree.

## Layout

```
vwappiercing_options/
├── interfaces.py                  # LogicVwapPiercingOptionsInterface (ILogicInterface impl)
├── run_backtest.py                # backtest runner -- writes to BackTestData, see below
├── Logic/
│   ├── piercing_engine.py         # VwapPiercingEngine -- THE state machine (Piercing/Reclaim/
│   │                               #   Confirm-Entry/SL/Exit-1..4/EOD), used identically by live
│   │                               #   and backtest via a `mode` (LIVE/BACKTEST) flag. All actual
│   │                               #   trading-rule logic lives here exactly once; only the
│   │                               #   handful of genuinely different touchpoints (candle/tick
│   │                               #   sourcing, real option quotes vs none, threads vs a plain
│   │                               #   loop, log wording/destination) branch on mode.
│   ├── vwap_piercing_options.py   # LogicVwapPiercingOptions -- thin LIVE-mode entry point
│   │                               #   (constructor shape executor.py/interfaces.py expect)
│   ├── backtest_engine.py         # thin BACKTEST-mode entry point (run_backtest_for_day, plus
│   │                               #   describe_exit_outcomes/determine_best_case_exit) -- shared
│   │                               #   by run_backtest.py and tests/test_pattern_dry_run.py
│   ├── pattern_rules.py           # pure Piercing/Reclaim/Confirm/window predicates, and
│   │                               #   resolve_front_month_future_symbol (shared)
│   └── option_selection.py        # premium-nearest-to-band selection (shared w/ tests)
├── Config/
│   ├── strategy_config.json       # every tunable trading parameter (see Configuration below) --
│   │                               #   read once at startup by both LIVE and BACKTEST
│   └── config_loader.py           # load_config() -- JSON + hardcoded fallback defaults, so a
│                                    #   missing/partial file never breaks or silently disables it
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

## Configuration (`Config/strategy_config.json`)

Every tunable trading parameter lives in one JSON file, loaded once at startup by `Config/config_loader.py`
and applied identically to LIVE and BACKTEST (via `pattern_rules.configure()` for the pure predicates, and
directly as instance attributes on `VwapPiercingEngine` for everything else) — a change here can never apply
to only one of the two engines. A missing file, or a file missing individual keys, falls back to the same
hardcoded defaults this strategy has always used, so it's always safe to delete or partially edit.

| Key | What it controls |
|---|---|
| `day_start_time` | Session start (`09:15:00`) used for VWAP/candle windows |
| `piercing_start_delay_minutes` / `piercing_cutoff_time` | How long after start to wait before seeking a Piercing, and when to stop looking for new ones (also when an incomplete setup gets abandoned) |
| `force_exit_time` | Any open trade still open at this time is force-closed regardless of exit state |
| `piercing_min_vwap_gap` / `piercing_min_open_vwap_gap` | Noise filters on how far Open/Close must clear VWAP to count as a real Piercing |
| `entry_min_vwap_gap` | How far price must clear back through VWAP to trigger entry |
| `reclaim_candle_interval_minutes` / `entry_candle_interval_minutes` | Fixed candle timeframes for the Reclaim and Entry legs |
| `bollinger_period` / `bollinger_std_dev` | Exit-4's Bollinger Band settings |
| `option_premium_band_low` / `option_premium_band_high` | The premium band the cheapest option is picked from |
| `exit2_percent` / `exit3_percent` | Exit-2 and Exit-3's target move, as a % of entry price |
| `index_name` / `strike_step` | Which index, and its strike spacing |
| `test_mode_start_time` / `test_mode_step_seconds` | LIVE test mode's simulated-clock start time and step size (see `--test_mode` below) |
| `historical_option_lookup_delay_seconds` | Pacing between BACKTEST's per-strike historical option lookups, to stay under the broker's rate limit |
| **`live_trading_enabled`** | **Master safety switch for real orders — see below** |
| **`target_exit`** | **Which exit (if any) also closes the trade for real — see below** |
| `order.order_type` / `order.product_type` / `order.lot_size` / `order.lot_count` | Real order details: e.g. `"MARKET"`, `"MIS"`, `75`, `1` |

### Real order placement

By default, `live_trading_enabled` is `false` and this strategy behaves exactly as it always has: a signal +
logging engine that places no real orders, in LIVE or BACKTEST/test mode alike. Setting it to `true` makes
the **LIVE, non-test-mode** engine place a real MARKET order for the selected option at entry, and a real
squaring-off order when the trade closes. It is never consulted in BACKTEST or `--test_mode` — those remain
pure previews regardless of this setting, so you can safely rehearse a config change there first.

**SL is always a real exit** once `live_trading_enabled` is `true` — it's never optional. `target_exit` names
one additional exit (`"exit1"`, `"exit2"`, `"exit3"`, `"exit4"`, or `null`) that also closes the trade for
real, so the position closes on whichever of SL or that target hits first; `null` (default) means SL is the
only real exit. BACKTEST and LIVE test mode both honor `target_exit` too, purely for previewing what LIVE
would actually do — so `describe_exit_outcomes()`'s "Best Case Exit" reporting and a real run's actual close
reason can be compared directly.

If placing the real entry order fails, the setup is dropped (no paper trade is recorded, no position was
opened). If placing the real *exit* order fails, the engine does **not** reset its state — it keeps retrying
the close on every subsequent check, since resetting while the real position is still open would mean losing
track of a position you're still holding.

### LIVE test mode (`--test_mode true --date YYYY-MM-DD`)

`executor.py --test_mode true --date YYYY-MM-DD` runs the live engine's actual code (threads, `execute()`,
order-placement gating, all of it) against a past date's candles on a simulated clock (starting at
`test_mode_start_time`, advancing `test_mode_step_seconds` per pass) instead of polling the broker in real
time — so it produces the same trades a `run_backtest.py` run would for that date, but by exercising the
exact live code path rather than a separate one. It's the fastest way to sanity-check a config change (or the
live code itself) without waiting for market hours, and it **never** places a real order regardless of
`live_trading_enabled`.

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
