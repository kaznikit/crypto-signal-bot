from pathlib import Path

import yaml

from bot.config import PREPARE_HTF_ORDER, BotConfig, EnvConfig, load_bot_config


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


def test_split_config_prepare_htfs() -> None:
    cfg = load_bot_config(Path("config"))
    assert cfg.prepare_htfs() == ("4H", "1H")
    assert cfg.entry.strategy == "structural"
    assert cfg.entry.ltf_by_htf["4H"] == "5M"
    assert cfg.filters.min_atr_pct == 0.3
    assert cfg.filters.min_rr == 1.5


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


def test_split_config_deep_merges_sections(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    parts = {
        "runtime.yaml": {
            "exchange": {"name": "bybit", "category": "linear"},
            "symbols": {},
            "timeframes": ["1H"],
            "paper_mode": {},
        },
        "setup.yaml": {
            "reversal": {},
            "continuation": {},
            "filters": {"min_atr_pct": 0.4},
        },
        "entry.yaml": {"entry": {}, "filters": {"min_rr": 2.0}},
        "risk.yaml": {"risk": {}},
        "research.yaml": {"research": {"max_expanded_bars_per_tf": 5_000}},
        "notifications.yaml": {"telegram": {}},
    }
    for filename, payload in parts.items():
        (config_dir / filename).write_text(yaml.safe_dump(payload), encoding="utf-8")

    cfg = load_bot_config(config_dir)

    assert cfg.filters.min_atr_pct == 0.4
    assert cfg.filters.min_rr == 2.0
    assert cfg.research.max_expanded_bars_per_tf == 5_000


def test_env_config_resolves_notification_channel_names() -> None:
    env = EnvConfig(
        TG_BOT_TOKEN="123456:TEST_TOKEN",
        TG_PREPARE_CHAT_ID="prepare",
        TG_ENTRY_CHAT_ID="entry",
    )

    assert env.telegram_chat_id("TG_PREPARE_CHAT_ID") == "prepare"
    assert env.telegram_chat_id("TG_ENTRY_CHAT_ID") == "entry"
    assert env.telegram_chat_id("UNKNOWN") is None
