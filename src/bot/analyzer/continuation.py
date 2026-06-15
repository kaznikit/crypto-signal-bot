from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from bot.analyzer.setup_machine import SetupEvent, build_setup, make_setup_id
from bot.analyzer.structure_state import resolve_prepare_structure_state
from bot.market.fibo import build_ote_zone
from bot.market.pivots import (
    continuation_anchor_break,
    detect_pivots,
    extract_impulse_legs_confirmed,
    extract_structure_breaks_htf,
    filter_causal_structure_breaks,
    latest_structure_break,
    prepare_suppressed_after_trend_flip,
    prepare_suppressed_during_impulse_lock,
    structure_break_key,
)
from bot.storage.models import Setup, SetupType


@dataclass
class ContinuationPrepareState:
    """Состояние walk-forward replay: lock направления + дедуп по ноге."""

    direction_lock_by_htf: dict[str, str] = field(default_factory=dict)
    prepared_leg_keys: set[tuple[str, int, int, str, str, int, int]] = field(
        default_factory=set
    )

def _funnel_inc(funnel: Counter[str] | None, key: str) -> None:
    if funnel is not None:
        funnel[key] += 1


def detect_continuation_prepare(
    *,
    symbol: str,
    htf: str,
    htf_df,
    close_time: int,
    swing_size: int,
    fib_level: float = 0.5,
    fib_zone_high: float = 0.786,
    impulse_max_age_bars: int = 60,
    bos_use_close: bool = True,
    ttl_hours: int = 24,
    funnel: Counter[str] | None = None,
    structure_max_bars_ago: int = 30,
    prepare_state: ContinuationPrepareState | None = None,
    ltf_expected: str = "5M",
    entry_mode: str = "simple",
    setup_type: SetupType = SetupType.CONTINUATION,
    anchor_kinds: tuple[str, ...] = ("BOS", "CHOCH"),
) -> tuple[Setup | None, SetupEvent | None]:
    """PREPARE-continuation: одно касание 0.5 на событие BOS/CHoCH.

    1. **Lock направления** (``prepare_state``): сбрасывается при
       противоположном BOS/CHoCH.
    2. Якорь = ``continuation_anchor_break`` — последний BOS/CHoCH в lock-
       направлении **строго после** последнего противоположного пробоя.
    3. Нога с ``end_idx >= broken_idx``; первая по времени нога с эмиссией
       на текущем баре.
    4. Не более одного PREPARE на ключ ``structure_break_key``.
    """
    if len(htf_df) < 2:
        return None, None

    last_pos = int(htf_df.index[-1])
    choch_only_mode = set(anchor_kinds) == {"CHOCH"}

    breaks_all = extract_structure_breaks_htf(
        htf_df, swing_size=swing_size, use_close=bos_use_close, impulse_lock=True
    )
    breaks = filter_causal_structure_breaks(
        breaks_all,
        htf_df,
        swing_size=swing_size,
        use_close=bos_use_close,
        impulse_lock=True,
        max_bars_ago=structure_max_bars_ago,
        last_idx=last_pos,
    )
    last_any = latest_structure_break(
        breaks,
        kinds=anchor_kinds,
        max_bars_ago=structure_max_bars_ago,
        last_idx=last_pos,
    )
    if last_any is None:
        _funnel_inc(funnel, "no_recent_structure_break")
        return None, None

    structure_direction = last_any.direction
    if prepare_state is not None:
        lock = prepare_state.direction_lock_by_htf.get(htf)
        if lock is None:
            prepare_state.direction_lock_by_htf[htf] = structure_direction
            lock = structure_direction
        elif last_any.direction != lock:
            anchor_new = continuation_anchor_break(
                breaks,
                direction=last_any.direction,
                last_idx=last_pos,
                max_bars_ago=structure_max_bars_ago,
                kinds=anchor_kinds,
            )
            if last_any.kind == "BOS" or anchor_new is not None:
                prepare_state.direction_lock_by_htf[htf] = last_any.direction
                lock = last_any.direction
                prepare_state.prepared_leg_keys = {
                    k for k in prepare_state.prepared_leg_keys if k[0] != htf
                }
            else:
                _funnel_inc(funnel, "opposite_structure_no_anchor_yet")
        structure_direction = lock

    last_break = continuation_anchor_break(
        breaks,
        direction=structure_direction,
        last_idx=last_pos,
        max_bars_ago=structure_max_bars_ago,
        kinds=anchor_kinds,
    )
    if last_break is None:
        _funnel_inc(funnel, "no_anchor_break_after_opposite_structure")
        return None, None

    raw_pivots = detect_pivots(htf_df, swing_size=swing_size)
    if not choch_only_mode:
        if prepare_suppressed_during_impulse_lock(
            htf_df,
            raw_pivots,
            swing_size=swing_size,
            use_close=bos_use_close,
            setup_direction=structure_direction,
        ):
            _funnel_inc(funnel, "prepare_suppressed_impulse_lock_retracement")
            return None, None
        if prepare_suppressed_after_trend_flip(
            df=htf_df,
            raw_pivots=raw_pivots,
            swing_size=swing_size,
            use_close=bos_use_close,
            setup_direction=structure_direction,
            last_pos=last_pos,
        ):
            _funnel_inc(funnel, "prepare_wait_correction_pivot_after_choch")
            return None, None

    br_key = structure_break_key(htf, last_break, htf_df)

    if not raw_pivots:
        return None, None

    # Только настоящие импульсные ноги HL→HH (LONG) / LH→LL (SHORT). Pine-эталон
    # рисует P/0.5-линию ровно для них. Любые HIGH↔LOW (например HH→первый
    # новый low после CHOCH) дают ложный P SHORT до подтверждения LH.
    # Используем полный список break'ов (без causal-фильтра) — он нужен для
    # корректного chaining min_idx между подтверждёнными ногами одного тренда.
    # Continuation эмиссия всё равно работает только по causal ``last_break``.
    legs = extract_impulse_legs_confirmed(
        raw_pivots, breaks_all, swing_size=swing_size, df=htf_df
    )
    if not legs:
        return None, None

    state = resolve_prepare_structure_state(
        df=htf_df,
        legs=legs,
        structure_break=last_break,
        structure_direction=structure_direction,
        fib_level=fib_level,
        impulse_max_age_bars=impulse_max_age_bars,
        swing_size=swing_size,
        last_pos=last_pos,
        on_reject=(lambda reason: _funnel_inc(funnel, reason)),
    )
    if state is None:
        return None, None

    leg_key = (
        htf,
        br_key[1],
        br_key[2],
        br_key[3],
        br_key[4],
        int(state.impulse.start_idx),
        int(state.impulse.end_idx),
    )
    if prepare_state is not None:
        if leg_key in prepare_state.prepared_leg_keys:
            _funnel_inc(funnel, "prepare_already_emitted_for_leg")
            return None, None
        prepare_state.prepared_leg_keys.add(leg_key)

    setup_id = make_setup_id(symbol, setup_type, htf, close_time)
    start_open_ms = int(htf_df.iloc[state.impulse.start_idx]["open_time"])
    end_open_ms = int(htf_df.iloc[state.impulse.end_idx]["open_time"])
    touch_open_ms = int(htf_df.iloc[state.touch_idx]["open_time"])
    ote_zone = build_ote_zone(state.impulse, fib_level, fib_zone_high)
    setup = build_setup(
        setup_id=setup_id,
        symbol=symbol,
        setup_type=setup_type,
        direction=state.direction,
        htf=htf,
        ltf_expected=ltf_expected,
        origin_price=state.level_50,
        ote_low=ote_zone.low,
        ote_high=ote_zone.high,
        invalidation_price=state.impulse.start_price,
        ttl_hours=ttl_hours,
        phase="WAIT_CHOCH",
        prepare_since_ms=touch_open_ms,
        entry_mode=entry_mode,
        entry_target_price=state.impulse.end_price,
    )
    event = SetupEvent(
        kind="PREPARE",
        payload={
            "setup_id": setup.id,
            "symbol": symbol,
            "type": setup_type.value,
            "direction": state.direction,
            "htf": htf,
            "origin_price": state.level_50,
            "ote_low": ote_zone.low,
            "ote_high": ote_zone.high,
            "prepare_trigger_level": state.level_50,
            "prepare_trigger_fib": float(fib_level),
            "fib_zone_low": float(fib_level),
            "fib_zone_high": float(fib_zone_high),
            "touched_0_5": True,
            "touched_0_618": False,
            "touched_0_705": False,
            "touched_0_786": False,
            "max_fib_depth": float(fib_level),
            "impulse_start_price": state.impulse.start_price,
            "impulse_end_price": state.impulse.end_price,
            "invalidation_price": state.impulse.start_price,
            "wait_for_ote_touch": False,
            "retrace_label": state.retrace_label,
            "retrace_price": state.retrace_price,
            "structure_kind": last_break.kind,
            "structure_level": last_break.swing_price,
            "structure_swing_open_ms": int(
                htf_df.iloc[last_break.swing_idx]["open_time"]
            ),
            "structure_broken_open_ms": int(
                htf_df.iloc[last_break.broken_idx]["open_time"]
            ),
            "impulse_leg_start_open_ms": start_open_ms,
            "impulse_leg_end_open_ms": end_open_ms,
            "touch_open_ms": touch_open_ms,
            "emission_bar_open_ms": int(
                htf_df.iloc[state.emission_bar_idx]["open_time"]
            ),
            "structure_break_key": br_key,
        },
    )
    return setup, event
