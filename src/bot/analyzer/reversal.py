from __future__ import annotations

from collections import Counter

from bot.analyzer.setup_machine import SetupEvent, build_setup, make_setup_id
from bot.market.pivots import (
    detect_pivots,
    detect_pivots_htf,
    extract_impulse_legs,
    extract_structure_breaks_htf,
    find_first_touch_idx,
    impulse_invalidated,
    latest_choch_break,
    prepare_emission_on_current_bar,
    prepare_suppressed_after_trend_flip,
    prepare_suppressed_during_impulse_lock,
)
from bot.storage.models import Setup, SetupType

REVERSAL_TRIGGER_FIB: float = 0.5


def _funnel_inc(funnel: Counter[str] | None, key: str) -> None:
    if funnel is not None:
        funnel[key] += 1


def detect_reversal_prepare(
    *,
    symbol: str,
    htf_df,
    close_time: int,
    swing_size: int,
    max_bars_ago_choch: int,
    impulse_max_age_bars: int = 60,
    bos_use_close: bool = True,
    ttl_hours: int,
    funnel: Counter[str] | None = None,
    ltf_expected: str = "5M|15M|1H",
) -> tuple[Setup | None, SetupEvent | None]:
    """PREPARE-reversal на основе Pine-стека (`bot.market.pivots`).

    Алгоритм:

    1. Пивоты + BOS/CHoCH; последний CHoCH в окне ``max_bars_ago_choch``.
    2. Реверсируемый импульс = последний HL→HH или LH→LL против CHoCH.
    3. ``emission_bar = max(end_idx + swing_size, touch_idx)`` — ловим касания
       0.5 во время подтверждения пивота (как у continuation).
    4. Invalidation = ``end_price`` реверсируемого импульса.
    """
    if len(htf_df) < 2:
        return None, None

    pivots = detect_pivots_htf(
        htf_df, swing_size=swing_size, use_close=bos_use_close, impulse_lock=True
    )
    if not pivots:
        return None, None

    breaks = extract_structure_breaks_htf(
        htf_df, swing_size=swing_size, use_close=bos_use_close, impulse_lock=True
    )
    if not breaks:
        _funnel_inc(funnel, "no_structure_breaks")
        return None, None

    last_pos = int(htf_df.index[-1])
    raw_pivots = detect_pivots(htf_df, swing_size=swing_size)
    choch_break = latest_choch_break(
        breaks,
        htf_df,
        raw_pivots,
        swing_size=swing_size,
        use_close=bos_use_close,
        max_bars_ago=max_bars_ago_choch,
        last_idx=last_pos,
    )
    if choch_break is None:
        _funnel_inc(funnel, "no_recent_choch")
        return None, None

    setup_direction = choch_break.direction
    if prepare_suppressed_during_impulse_lock(
        htf_df,
        raw_pivots,
        swing_size=swing_size,
        use_close=bos_use_close,
        setup_direction=setup_direction,
    ):
        _funnel_inc(funnel, "prepare_suppressed_impulse_lock_retracement")
        return None, None
    if prepare_suppressed_after_trend_flip(
        df=htf_df,
        raw_pivots=raw_pivots,
        swing_size=swing_size,
        use_close=bos_use_close,
        setup_direction=setup_direction,
        last_pos=last_pos,
    ):
        _funnel_inc(funnel, "prepare_wait_correction_pivot_after_choch")
        return None, None
    reversed_impulse_direction = "SHORT" if setup_direction == "LONG" else "LONG"

    legs = [
        leg
        for leg in extract_impulse_legs(pivots)
        if leg.direction == reversed_impulse_direction and leg.end_idx <= choch_break.broken_idx
    ]
    if not legs:
        _funnel_inc(funnel, "no_reversed_impulse_leg")
        return None, None
    impulse = legs[-1]

    if last_pos - impulse.end_idx > impulse_max_age_bars:
        _funnel_inc(funnel, "impulse_too_old")
        return None, None
    if impulse.end_idx >= last_pos:
        _funnel_inc(funnel, "impulse_peak_is_current_bar")
        return None, None

    invalidation = impulse.end_price
    if impulse_invalidated(
        htf_df,
        direction=setup_direction,
        start_price=invalidation,
        after_idx=impulse.end_idx,
    ):
        _funnel_inc(funnel, "impulse_invalidated")
        return None, None

    trigger_level = impulse.fib_half
    touch_direction = "SHORT" if setup_direction == "LONG" else "LONG"

    since_touch = max(impulse.end_idx, choch_break.broken_idx)
    touch_idx = find_first_touch_idx(
        htf_df,
        direction=touch_direction,
        level=trigger_level,
        since_idx=since_touch,
    )
    if touch_idx < 0:
        _funnel_inc(funnel, "no_touch_yet")
        return None, None

    emission_bar = max(impulse.end_idx + swing_size, touch_idx)
    if emission_bar != last_pos:
        if emission_bar < last_pos:
            _funnel_inc(funnel, "emission_bar_in_past")
        else:
            _funnel_inc(funnel, "emission_bar_in_future")
        return None, None

    emission = prepare_emission_on_current_bar(
        htf_df,
        leg_end_idx=impulse.end_idx,
        swing_size=swing_size,
        touch_direction=touch_direction,
        level=trigger_level,
        since_idx=since_touch,
    )
    if emission is None:
        return None, None
    _, touch_idx = emission

    setup_id = make_setup_id(symbol, SetupType.REVERSAL, "4H", close_time)
    structure_swing_open_ms = int(htf_df.iloc[choch_break.swing_idx]["open_time"])
    structure_broken_open_ms = int(htf_df.iloc[choch_break.broken_idx]["open_time"])
    touch_open_ms = int(htf_df.iloc[touch_idx]["open_time"])
    setup = build_setup(
        setup_id=setup_id,
        symbol=symbol,
        setup_type=SetupType.REVERSAL,
        direction=setup_direction,
        htf="4H",
        ltf_expected=ltf_expected,
        origin_price=trigger_level,
        ote_low=trigger_level,
        ote_high=trigger_level,
        invalidation_price=invalidation,
        ttl_hours=ttl_hours,
        prepare_since_ms=touch_open_ms,
    )
    event = SetupEvent(
        kind="PREPARE",
        payload={
            "setup_id": setup.id,
            "symbol": symbol,
            "type": SetupType.REVERSAL.value,
            "direction": setup_direction,
            "htf": "4H",
            "origin_price": trigger_level,
            "ote_low": trigger_level,
            "ote_high": trigger_level,
            "prepare_trigger_level": trigger_level,
            "prepare_trigger_fib": float(REVERSAL_TRIGGER_FIB),
            "impulse_start_price": impulse.start_price,
            "impulse_end_price": impulse.end_price,
            "invalidation_price": invalidation,
            "wait_for_ote_touch": False,
            "structure_kind": choch_break.kind,
            "structure_level": choch_break.swing_price,
            "structure_swing_open_ms": structure_swing_open_ms,
            "structure_broken_open_ms": structure_broken_open_ms,
            "touch_open_ms": touch_open_ms,
            "emission_bar_open_ms": int(htf_df.iloc[last_pos]["open_time"]),
        },
    )
    return setup, event
