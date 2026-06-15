from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any

from bot.market.fibo import OteZone, is_price_in_zone
from bot.storage.models import Setup, SetupState, SetupType
from bot.util.time import ensure_utc, utcnow


@dataclass(slots=True)
class SetupEvent:
    kind: str
    payload: dict[str, Any]


def make_setup_id(symbol: str, setup_type: SetupType, htf: str, close_time: int) -> str:
    raw = f"{symbol}:{setup_type}:{htf}:{close_time}"
    return sha256(raw.encode("ascii")).hexdigest()[:32]


def build_setup(
    setup_id: str,
    symbol: str,
    setup_type: SetupType,
    direction: str,
    htf: str,
    ltf_expected: str,
    origin_price: float,
    ote_low: float,
    ote_high: float,
    invalidation_price: float,
    ttl_hours: int,
    score: int = 0,
    phase: str = "WAIT_CHOCH",
    is_liberal: bool = False,
    prepare_since_ms: int | None = None,
    entry_count: int = 0,
    last_entry_bar_ms: int | None = None,
    last_entry_price: float | None = None,
    last_entry_swing_level: float | None = None,
    entry_cascade_stage: int = 0,
    entry_cascade_since_ms: int | None = None,
    entry_cascade_touch_ms: int | None = None,
    entry_cascade_retrace_level: float | None = None,
    entry_mode: str = "simple",
    entry_target_price: float | None = None,
    comparison_group_id: str | None = None,
) -> Setup:
    now = utcnow()
    return Setup(
        id=setup_id,
        symbol=symbol,
        type=setup_type.value,
        state=SetupState.ARMED.value,
        direction=direction,
        htf=htf,
        ltf_expected=ltf_expected,
        origin_price=origin_price,
        ote_low=ote_low,
        ote_high=ote_high,
        invalidation_price=invalidation_price,
        score=score,
        phase=phase,
        is_liberal=is_liberal,
        prepare_since_ms=prepare_since_ms,
        entry_count=entry_count,
        last_entry_bar_ms=last_entry_bar_ms,
        last_entry_price=last_entry_price,
        last_entry_swing_level=last_entry_swing_level,
        entry_cascade_stage=entry_cascade_stage,
        entry_cascade_since_ms=entry_cascade_since_ms,
        entry_cascade_touch_ms=entry_cascade_touch_ms,
        entry_cascade_retrace_level=entry_cascade_retrace_level,
        entry_mode=entry_mode,
        comparison_group_id=comparison_group_id,
        entry_advanced_stage="WAIT_SWEEP",
        entry_sweep_level=None,
        entry_sweep_extreme=None,
        entry_sweep_ms=None,
        entry_reclaim_ms=None,
        entry_confirm_level=None,
        entry_confirm_ms=None,
        entry_target_price=entry_target_price,
        fib_dca_plan_json=None,
        fib_dca_filled_json=None,
        fib_dca_average_entry=None,
        fib_dca_filled_weight_pct=0.0,
        fib_dca_last_fill_ms=None,
        active_trade_stop_price=None,
        active_trade_target_price=None,
        active_trade_tf=None,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )


def clone_setup_for_entry_mode(
    setup: Setup,
    *,
    setup_id: str,
    entry_mode: str,
    comparison_group_id: str,
) -> Setup:
    clone = build_setup(
        setup_id=setup_id,
        symbol=setup.symbol,
        setup_type=SetupType(setup.type),
        direction=setup.direction,
        htf=setup.htf,
        ltf_expected=setup.ltf_expected,
        origin_price=setup.origin_price,
        ote_low=setup.ote_low,
        ote_high=setup.ote_high,
        invalidation_price=setup.invalidation_price,
        ttl_hours=1,
        score=setup.score,
        phase=setup.phase,
        is_liberal=setup.is_liberal,
        prepare_since_ms=setup.prepare_since_ms,
        entry_mode=entry_mode,
        entry_target_price=setup.entry_target_price,
        comparison_group_id=comparison_group_id,
    )
    clone.created_at = setup.created_at
    clone.updated_at = setup.updated_at
    clone.expires_at = setup.expires_at
    return clone


def tick_setup(
    setup: Setup,
    price_low: float,
    price_high: float,
    choch_direction: str | None,
    *,
    check_invalidation: bool = True,
) -> tuple[str, SetupEvent | None, str | None]:
    """Возвращает (state, event, phase_update).

    phase_update — новое значение колонки phase (например WAIT_CHOCH после касания OTE), иначе None.
    """
    now = utcnow()
    if now >= ensure_utc(setup.expires_at):
        return (
            SetupState.EXPIRED.value,
            SetupEvent(kind="EXPIRED", payload={"setup_id": setup.id}),
            None,
        )
    if check_invalidation:
        if setup.direction == "LONG" and price_low <= setup.invalidation_price:
            return SetupState.INVALIDATED.value, SetupEvent(
                kind="INVALIDATED",
                payload={"setup_id": setup.id},
            ), None
        if setup.direction == "SHORT" and price_high >= setup.invalidation_price:
            return SetupState.INVALIDATED.value, SetupEvent(
                kind="INVALIDATED",
                payload={"setup_id": setup.id},
            ), None

    phase = setup.phase or "WAIT_CHOCH"
    phase_out: str | None = None
    effective = phase

    if phase == "WAIT_OTE":
        zone = OteZone(low=setup.ote_low, high=setup.ote_high)
        if not is_price_in_zone(price_low, price_high, zone):
            return setup.state, None, None
        effective = "WAIT_CHOCH"
        phase_out = "WAIT_CHOCH"

    if (
        effective == "WAIT_CHOCH"
        and choch_direction is not None
        and choch_direction == setup.direction
    ):
        return (
            SetupState.CONFIRMED.value,
            SetupEvent(
                kind="ENTRY",
                payload={"setup_id": setup.id, "direction": setup.direction},
            ),
            phase_out,
        )
    return setup.state, None, phase_out
