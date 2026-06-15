import sqlite3
from datetime import UTC, datetime

import pytest

from bot.analyzer.setup_machine import build_setup
from bot.storage.models import SetupType
from bot.storage.repo import Repository


def test_create_schema_adds_fib_dca_setup_columns(tmp_path) -> None:
    db_path = tmp_path / "bot.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE setups (id VARCHAR(64) PRIMARY KEY)")
    repo = Repository(f"sqlite:///{db_path}")

    repo.create_schema()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(setups)").fetchall()}
    assert {
        "fib_dca_plan_json",
        "fib_dca_filled_json",
        "fib_dca_average_entry",
        "fib_dca_filled_weight_pct",
        "fib_dca_last_fill_ms",
        "active_trade_stop_price",
        "active_trade_target_price",
        "active_trade_tf",
        "comparison_group_id",
    } <= columns


def test_repository_persists_trade_lifecycle(tmp_path) -> None:
    repo = Repository(f"sqlite:///{tmp_path / 'bot.db'}")
    repo.create_schema()
    setup = build_setup(
        setup_id="setup-1",
        symbol="BTCUSDT",
        setup_type=SetupType.CONTINUATION,
        direction="LONG",
        htf="1H",
        ltf_expected="5M",
        origin_price=100.0,
        ote_low=95.0,
        ote_high=100.0,
        invalidation_price=90.0,
        ttl_hours=1,
        entry_target_price=120.0,
    )
    now = datetime.now(UTC)

    repo.upsert_trade_entry(
        setup=setup,
        payload={"entry": 100.0, "sl": 90.0, "tp": 120.0, "risk_fraction": 0.6},
        entry_time=1,
        at=now,
    )
    repo.upsert_trade_entry(
        setup=setup,
        payload={"entry": 95.0, "sl": 90.0, "tp": 120.0, "risk_fraction": 0.4},
        entry_time=2,
        at=now,
    )
    assert repo.load_open_trades()[0].risk_r == 1.0
    repo.close_trade(
        setup_id=setup.id,
        exit_time=2,
        exit_price=90.0,
        exit_reason="SL",
        at=now,
    )

    trade = repo.load_trade(setup.id)
    assert trade is not None
    assert trade.status == "CLOSED"
    assert trade.realized_r == pytest.approx(-1.0)
