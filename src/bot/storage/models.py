from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SetupType(StrEnum):
    REVERSAL = "REVERSAL"
    CONTINUATION = "CONTINUATION"


class SetupState(StrEnum):
    ARMED = "ARMED"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class SignalKind(StrEnum):
    PREPARE = "PREPARE"
    ENTRY = "ENTRY"
    INVALIDATED = "INVALIDATED"
    HEARTBEAT = "HEARTBEAT"


class Setup(Base):
    __tablename__ = "setups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8), index=True)
    htf: Mapped[str] = mapped_column(String(8))
    ltf_expected: Mapped[str] = mapped_column(String(16))
    origin_price: Mapped[float] = mapped_column(Float)
    ote_low: Mapped[float] = mapped_column(Float)
    ote_high: Mapped[float] = mapped_column(Float)
    invalidation_price: Mapped[float] = mapped_column(Float)
    score: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[str] = mapped_column(String(32), default="WAIT_CHOCH")
    is_liberal: Mapped[bool] = mapped_column(Boolean, default=False)
    # open_time (ms) бара PREPARE на HTF — якорь для since_prepare на LTF
    prepare_since_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Сколько ENTRY уже отправлено по этому setup.
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    # open_time (ms) бара, на котором отправляли последний ENTRY.
    last_entry_bar_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Цена последнего отправленного ENTRY.
    last_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Reset-swing уровень (LTF) для re-entry: LONG -> прошлый LOW, SHORT -> прошлый HIGH.
    last_entry_swing_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Прогресс каскадного ENTRY: stage указывает текущий TF в цепочке.
    entry_cascade_stage: Mapped[int] = mapped_column(Integer, default=0)
    # open_time предыдущего BOS в каскаде, после которого ждём retrace 0.5.
    entry_cascade_since_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # open_time бара касания 0.5 перед текущим TF-подтверждением.
    entry_cascade_touch_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Уровень 0.5 предыдущего TF-импульса.
    entry_cascade_retrace_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    setup_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class KlineCache(Base):
    __tablename__ = "klines_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    tf: Mapped[str] = mapped_column(String(8), index=True)
    open_time: Mapped[int] = mapped_column(Integer, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class BotState(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
