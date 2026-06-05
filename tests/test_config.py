from pathlib import Path

from bot.config import PREPARE_HTF_ORDER, BotConfig, load_bot_config


def test_prepare_htfs_respects_config_order() -> None:
    cfg = BotConfig.model_validate(
        {
            "exchange": {"name": "bybit", "category": "linear"},
            "symbols": {},
            "timeframes": ["1H", "4H", "15M"],
            "reversal": {},
            "continuation": {},
            "filters": {},
            "risk": {},
            "telegram": {},
            "paper_mode": {},
        }
    )
    assert cfg.prepare_htfs() == PREPARE_HTF_ORDER


def test_prepare_htfs_filters_unknown_and_empty() -> None:
    cfg = BotConfig.model_validate(
        {
            "exchange": {"name": "bybit", "category": "linear"},
            "symbols": {},
            "timeframes": ["4H", "1H", "5M"],
            "reversal": {},
            "continuation": {},
            "filters": {},
            "risk": {},
            "telegram": {},
            "paper_mode": {},
        }
    )
    assert cfg.prepare_htfs() == ("4H", "1H")


def test_config_yaml_prepare_htfs() -> None:
    cfg = load_bot_config(Path("config.yaml"))
    assert cfg.prepare_htfs() == ("4H", "1H")


def test_config_yaml_entry_cascade_enabled_for_1h() -> None:
    cfg = load_bot_config(Path("config.yaml"))

    assert cfg.entry.cascade_enabled is True
    assert cfg.entry.cascade_by_htf["1H"] == "5M|1M"
    assert cfg.entry.cascade_confirm_structure_kinds == ["BOS", "CHOCH"]
    assert cfg.telegram.send_prepare_signals is True
    assert cfg.history_replay.max_expanded_bars_per_tf == 60_000
    assert cfg.entry_stats.check_interval_hours == 24


def test_entry_max_entries_per_setup_default() -> None:
    cfg = BotConfig.model_validate(
        {
            "exchange": {"name": "bybit", "category": "linear"},
            "symbols": {},
            "timeframes": ["1H"],
            "reversal": {},
            "continuation": {},
            "filters": {},
            "risk": {},
            "telegram": {},
            "paper_mode": {},
        }
    )
    assert cfg.entry.max_entries_per_setup == 2
    assert cfg.telegram.send_prepare_signals is True
    assert cfg.entry_stats.check_interval_hours == 24
