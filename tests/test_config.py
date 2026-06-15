from pathlib import Path

import pytest
from pydantic import ValidationError

from bot.config import PREPARE_HTF_ORDER, BotConfig, EnvConfig, load_bot_config

CONFIG_EXAMPLE = Path("config.example.yaml")
SPLIT_CONFIG = Path("config")


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


def test_config_example_prepare_htfs() -> None:
    cfg = load_bot_config(CONFIG_EXAMPLE)
    assert cfg.prepare_htfs() == ("4H", "1H")


def test_split_config_matches_current_runtime_settings() -> None:
    cfg = load_bot_config(SPLIT_CONFIG)

    assert cfg.prepare_htfs() == ("4H", "1H")
    assert cfg.entry.mode == "simple"
    assert cfg.entry.active_modes() == ("simple", "sweep_reclaim", "advanced")
    assert cfg.entry.comparison_enabled() is True
    assert cfg.entry.compute_sl_tp is True
    assert cfg.entry.max_entries_per_setup == 2
    assert cfg.entry.risk_fractions == [0.6, 0.4]
    assert cfg.history_replay.max_expanded_bars_per_tf == 60_000
    assert cfg.history_replay.intrabar_policy == "conservative"
    assert cfg.strategy_features.quality_score_filter_enabled is False
    assert cfg.telegram.prepare_chat_id_env == "TG_PREPARE_CHAT_ID"
    assert cfg.telegram.entry_chat_id_env == "TG_ENTRY_CHAT_ID"
    assert cfg.telegram.route_paper_mode_to_paper_chat is False


def test_config_example_entry_cascade_settings_for_1h() -> None:
    cfg = load_bot_config(CONFIG_EXAMPLE)

    assert cfg.entry.cascade_enabled is False
    assert cfg.entry.cascade_by_htf["1H"] == "5M|1M"
    assert cfg.entry.cascade_confirm_structure_kinds == ["BOS", "CHOCH"]
    assert cfg.telegram.send_prepare_signals is True
    assert cfg.history_replay.max_expanded_bars_per_tf == 60_000
    assert cfg.entry_stats.check_interval_hours == 24
    assert cfg.entry_stats.max_candidates_per_run == 25
    assert cfg.prepare_stats.evaluation_tf_by_htf["1H"] == "5M"
    assert [level.weight_pct for level in cfg.entry.fib_dca.levels] == [40, 30, 20, 10]
    assert cfg.prepare_stats.check_interval_hours == 24
    assert cfg.entry.fib_dca.levels[0].fib == 0.5


def test_fib_dca_weights_must_sum_to_100() -> None:
    with pytest.raises(ValidationError):
        BotConfig.model_validate(
            {
                "exchange": {"name": "bybit", "category": "linear"},
                "symbols": {},
                "timeframes": ["1H"],
                "reversal": {},
                "continuation": {},
                "entry": {
                    "mode": "fib_dca",
                    "fib_dca": {
                        "levels": [
                            {"fib": 0.5, "weight_pct": 40},
                            {"fib": 0.618, "weight_pct": 40},
                        ]
                    },
                },
                "filters": {},
                "risk": {},
                "telegram": {},
                "paper_mode": {},
            }
        )


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
    assert cfg.entry.max_entries_per_setup == 1
    assert cfg.entry.mode == "simple"
    assert cfg.entry.active_modes() == ("simple",)
    assert cfg.entry.comparison_enabled() is False
    assert cfg.entry.ltf_by_htf == {"4H": "5M", "1H": "5M", "15M": "5M"}
    assert cfg.entry.fib_dca.monitoring_tf_by_htf == {
        "4H": "5M",
        "1H": "5M",
        "15M": "5M",
    }
    assert cfg.entry.advanced.confirm_structure_kinds == ["CHOCH"]
    assert cfg.telegram.send_prepare_signals is True
    assert cfg.entry_stats.check_interval_hours == 24
    assert cfg.entry_stats.max_candidates_per_run == 25


def test_env_config_resolves_notification_channel_names() -> None:
    env = EnvConfig(
        TG_BOT_TOKEN="123456:TEST_TOKEN",
        TG_PREPARE_CHAT_ID="prepare",
        TG_ENTRY_CHAT_ID="entry",
    )

    assert env.telegram_chat_id("TG_PREPARE_CHAT_ID") == "prepare"
    assert env.telegram_chat_id("TG_ENTRY_CHAT_ID") == "entry"
    assert env.telegram_chat_id("UNKNOWN") is None
