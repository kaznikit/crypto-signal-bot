from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from bot.storage.models import Base, BotState, Setup, Signal
from bot.util.time import ensure_utc


def _normalize_setup_times(setup: Setup) -> Setup:
    setup.created_at = ensure_utc(setup.created_at)
    setup.updated_at = ensure_utc(setup.updated_at)
    setup.expires_at = ensure_utc(setup.expires_at)
    return setup


def _sqlite_table_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {str(r[1]) for r in rows}


class Repository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_engine(db_url, future=True)
        self._session_factory = sessionmaker(
            bind=self._engine,
            class_=Session,
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        Base.metadata.create_all(self._engine)
        self._migrate_sqlite_columns()

    def _migrate_sqlite_columns(self) -> None:
        if not str(self._engine.url).startswith("sqlite"):
            return
        with self._engine.begin() as conn:
            cols = _sqlite_table_columns(conn, "setups")
            if "phase" not in cols:
                conn.execute(
                    text("ALTER TABLE setups ADD COLUMN phase VARCHAR(32) DEFAULT 'WAIT_CHOCH'")
                )
            if "is_liberal" not in cols:
                conn.execute(
                    text("ALTER TABLE setups ADD COLUMN is_liberal INTEGER NOT NULL DEFAULT 0")
                )
            if "prepare_since_ms" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN prepare_since_ms INTEGER"))
            if "entry_count" not in cols:
                conn.execute(
                    text("ALTER TABLE setups ADD COLUMN entry_count INTEGER NOT NULL DEFAULT 0")
                )
            if "last_entry_bar_ms" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN last_entry_bar_ms INTEGER"))
            if "last_entry_price" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN last_entry_price FLOAT"))
            if "last_entry_swing_level" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN last_entry_swing_level FLOAT"))
            if "entry_cascade_stage" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE setups ADD COLUMN "
                        "entry_cascade_stage INTEGER NOT NULL DEFAULT 0"
                    )
                )
            if "entry_cascade_since_ms" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN entry_cascade_since_ms INTEGER"))
            if "entry_cascade_touch_ms" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN entry_cascade_touch_ms INTEGER"))
            if "entry_cascade_retrace_level" not in cols:
                conn.execute(
                    text("ALTER TABLE setups ADD COLUMN entry_cascade_retrace_level FLOAT")
                )
            if "entry_mode" not in cols:
                conn.execute(
                    text("ALTER TABLE setups ADD COLUMN entry_mode VARCHAR(16) DEFAULT 'simple'")
                )
            if "entry_advanced_stage" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE setups ADD COLUMN "
                        "entry_advanced_stage VARCHAR(32) DEFAULT 'WAIT_SWEEP'"
                    )
                )
            for column in (
                "entry_sweep_level",
                "entry_sweep_extreme",
                "entry_confirm_level",
                "entry_target_price",
            ):
                if column not in cols:
                    conn.execute(text(f"ALTER TABLE setups ADD COLUMN {column} FLOAT"))
            for column in ("entry_sweep_ms", "entry_reclaim_ms", "entry_confirm_ms"):
                if column not in cols:
                    conn.execute(text(f"ALTER TABLE setups ADD COLUMN {column} INTEGER"))
            for column in ("fib_dca_plan_json", "fib_dca_filled_json"):
                if column not in cols:
                    conn.execute(text(f"ALTER TABLE setups ADD COLUMN {column} TEXT"))
            for column in ("fib_dca_average_entry", "fib_dca_filled_weight_pct"):
                if column not in cols:
                    default = " DEFAULT 0" if column == "fib_dca_filled_weight_pct" else ""
                    conn.execute(text(f"ALTER TABLE setups ADD COLUMN {column} FLOAT{default}"))
            if "fib_dca_last_fill_ms" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN fib_dca_last_fill_ms INTEGER"))
            for column in ("active_trade_stop_price", "active_trade_target_price"):
                if column not in cols:
                    conn.execute(text(f"ALTER TABLE setups ADD COLUMN {column} FLOAT"))
            if "active_trade_tf" not in cols:
                conn.execute(text("ALTER TABLE setups ADD COLUMN active_trade_tf VARCHAR(8)"))

    def upsert_setup(self, setup: Setup) -> None:
        with self._session_factory() as session:
            session.merge(setup)
            session.commit()

    def save_signal(self, signal: Signal) -> None:
        with self._session_factory() as session:
            session.merge(signal)
            session.commit()

    def load_active_setups(self) -> list[Setup]:
        with self._session_factory() as session:
            rows = session.scalars(select(Setup).where(Setup.state == "ARMED")).all()
            return [_normalize_setup_times(row) for row in rows]

    def update_setup_phase(self, setup_id: str, phase: str) -> None:
        with self._session_factory() as session:
            row = session.get(Setup, setup_id)
            if row is None:
                return
            row.phase = phase
            session.merge(row)
            session.commit()

    def set_state_value(self, key: str, value: dict[str, str]) -> None:
        with self._session_factory() as session:
            row = session.get(BotState, key)
            payload = json.dumps(value, ensure_ascii=True)
            if row is None:
                row = BotState(key=key, value=payload)
            else:
                row.value = payload
            session.merge(row)
            session.commit()

    def get_state_value(self, key: str) -> dict[str, str] | None:
        with self._session_factory() as session:
            row = session.get(BotState, key)
            return None if row is None else json.loads(row.value)

    def mark_setup_state(self, setup_id: str, new_state: str, at: datetime) -> None:
        with self._session_factory() as session:
            row = session.get(Setup, setup_id)
            if row is None:
                return
            row.state = new_state
            row.updated_at = at
            session.merge(row)
            session.commit()

    def load_signals_for_export(
        self,
        *,
        kinds: tuple[str, ...],
    ) -> list[Signal]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(Signal).where(Signal.kind.in_(kinds)).order_by(Signal.sent_at)
            ).all()
            return list(rows)

    def load_signals_by_kind(self, kinds: tuple[str, ...]) -> list[Signal]:
        return self.load_signals_for_export(kinds=kinds)
