import importlib.util
import json
from pathlib import Path


_LOADER_PATH = Path(__file__).parents[1] / "Config" / "config_loader.py"
_SPEC = importlib.util.spec_from_file_location("vwappiercing_options_config_loader", _LOADER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load config loader from {_LOADER_PATH}")
_CONFIG_LOADER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CONFIG_LOADER)


def test_order_config_overrides_legacy_order_settings(tmp_path):
    strategy_path = tmp_path / "strategy_config.json"
    order_path = tmp_path / "order_config.json"
    strategy_path.write_text(json.dumps({
        "live_trading_enabled": False,
        "target_exit": None,
        "order": {"order_type": "LIMIT", "lot_count": 4},
    }))
    order_path.write_text(json.dumps({
        "place_order": True,
        "target_exit": "exit3",
        "entry_order_type": "SL",
        "exit_order_type": "SL-M",
        "lot_count": 2,
        "tick_size": 0.1,
    }))

    config = _CONFIG_LOADER.load_config(strategy_path, order_path)

    assert config["live_trading_enabled"] is True
    assert config["target_exit"] == "exit3"
    assert config["order"]["entry_order_type"] == "SL"
    assert config["order"]["exit_order_type"] == "SL-M"
    assert config["order"]["lot_count"] == 2
    assert config["order"]["tick_size"] == 0.1
    assert "place_order" not in config["order"]


def test_legacy_order_type_applies_to_entry_and_exit(tmp_path):
    strategy_path = tmp_path / "strategy_config.json"
    strategy_path.write_text(json.dumps({"order": {"order_type": "LIMIT"}}))

    config = _CONFIG_LOADER.load_config(strategy_path, tmp_path / "missing_order_config.json")

    assert config["order"]["entry_order_type"] == "LIMIT"
    assert config["order"]["exit_order_type"] == "LIMIT"
