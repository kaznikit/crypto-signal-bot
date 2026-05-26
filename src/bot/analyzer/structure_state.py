from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from bot.market.pivots import (
    ImpulseLeg,
    StructureBreak,
    find_first_touch_idx,
    impulse_invalidated,
    prepare_emission_bar_idx,
    prepare_emission_on_current_bar,
)


RejectCallback = Callable[[str], None] | None


@dataclass(slots=True, frozen=True)
class PrepareStructureState:
    """Состояние HTF-подготовки перед PREPARE.

    Явно хранит ключевые узлы цепочки:
    structure break -> импульс -> уровень 0.5 -> first touch -> бар эмиссии.
    """

    direction: str
    structure_break: StructureBreak
    impulse: ImpulseLeg
    level_50: float
    touch_idx: int
    since_touch_idx: int
    emission_bar_idx: int

    @property
    def retrace_label(self) -> str:
        return "HL" if self.direction == "LONG" else "LH"

    @property
    def retrace_price(self) -> float:
        return float(self.impulse.start_price)


def _fib_at(impulse: ImpulseLeg, fib: float) -> float:
    if impulse.direction == "LONG":
        return impulse.end_price - fib * (impulse.end_price - impulse.start_price)
    return impulse.end_price + fib * (impulse.start_price - impulse.end_price)


def resolve_prepare_structure_state(
    *,
    df: pd.DataFrame,
    legs: list[ImpulseLeg],
    structure_break: StructureBreak,
    structure_direction: str,
    fib_level: float,
    impulse_max_age_bars: int,
    swing_size: int,
    last_pos: int,
    on_reject: RejectCallback = None,
) -> PrepareStructureState | None:
    """Подбирает актуальное состояние PREPARE для текущего HTF-бара.

    Возвращает ``None``, если на текущем баре PREPARE эмитить нельзя.
    """
    for cand in legs:
        if cand.direction != structure_direction:
            if on_reject is not None:
                on_reject("leg_direction_misaligned")
            continue
        if cand.anchor_break_idx is None:
            if on_reject is not None:
                on_reject("leg_has_no_anchor_break")
            continue
        if cand.anchor_break_idx < structure_break.broken_idx:
            if on_reject is not None:
                on_reject("leg_not_confirmed_by_anchor_break")
            continue
        if last_pos - cand.end_idx > impulse_max_age_bars:
            if on_reject is not None:
                on_reject("leg_too_old")
            break
        if cand.end_idx >= last_pos:
            if on_reject is not None:
                on_reject("leg_peak_is_current_bar")
            continue
        if impulse_invalidated(
            df,
            direction=cand.direction,
            start_price=cand.start_price,
            after_idx=max(
                cand.end_idx,
                cand.anchor_break_idx
                if cand.anchor_break_idx is not None
                else cand.end_idx,
            ),
        ):
            if on_reject is not None:
                on_reject("leg_invalidated")
            continue

        trigger = cand.fib_half if fib_level == 0.5 else _fib_at(cand, fib_level)
        since_touch = max(cand.end_idx, cand.anchor_break_idx, structure_break.broken_idx)
        touch_idx = find_first_touch_idx(
            df,
            direction=cand.direction,
            level=trigger,
            since_idx=since_touch,
        )
        if touch_idx < 0:
            if on_reject is not None:
                on_reject("no_touch_yet")
            continue

        emission_bar = prepare_emission_bar_idx(
            leg_end_idx=cand.end_idx,
            anchor_break_idx=cand.anchor_break_idx,
            swing_size=swing_size,
            touch_idx=touch_idx,
        )
        if emission_bar != last_pos:
            if on_reject is not None:
                if emission_bar < last_pos:
                    on_reject("emission_bar_in_past")
                else:
                    on_reject("emission_bar_in_future")
            continue

        emission = prepare_emission_on_current_bar(
            df,
            leg_end_idx=cand.end_idx,
            swing_size=swing_size,
            touch_direction=cand.direction,
            level=trigger,
            since_idx=since_touch,
            anchor_break_idx=cand.anchor_break_idx,
        )
        if emission is None:
            continue
        _, touch_idx = emission
        return PrepareStructureState(
            direction=cand.direction,
            structure_break=structure_break,
            impulse=cand,
            level_50=float(trigger),
            touch_idx=int(touch_idx),
            since_touch_idx=int(since_touch),
            emission_bar_idx=int(last_pos),
        )
    return None
