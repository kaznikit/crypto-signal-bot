import sqlite3

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
