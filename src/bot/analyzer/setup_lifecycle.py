from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.market.pivots import (
    StructureBreak,
    opposite_structure_break_since_open_ms,
    structure_break_since_open_ms,
)


@dataclass(slots=True, frozen=True)
class SetupStructureDecision:
    """Решение по активному setup на основе новых HTF структурных событий."""

    action: str  # "KEEP" | "INVALIDATE_OPPOSITE" | "RESET_SAME_DIRECTION"
    trigger: StructureBreak | None = None

    @property
    def should_invalidate(self) -> bool:
        return self.action != "KEEP"


def decide_setup_structure_transition(
    *,
    breaks: list[StructureBreak],
    df: pd.DataFrame,
    setup_direction: str,
    since_open_ms: int,
) -> SetupStructureDecision:
    """Единый lifecycle-решатель для live/replay.

    Приоритет:
    1) противоположная структура после PREPARE -> INVALIDATE_OPPOSITE;
    2) новая структура в том же направлении после PREPARE -> RESET_SAME_DIRECTION;
    3) иначе KEEP.
    """
    opposite = opposite_structure_break_since_open_ms(
        breaks,
        df,
        setup_direction=setup_direction,
        since_open_ms=since_open_ms,
    )
    if opposite is not None:
        return SetupStructureDecision(action="INVALIDATE_OPPOSITE", trigger=opposite)

    same_dir_new = structure_break_since_open_ms(
        breaks,
        df,
        since_open_ms=since_open_ms,
        direction=setup_direction,
        kinds=("BOS", "CHOCH"),
        strict_after=True,
    )
    if same_dir_new is not None:
        return SetupStructureDecision(action="RESET_SAME_DIRECTION", trigger=same_dir_new)

    return SetupStructureDecision(action="KEEP", trigger=None)
