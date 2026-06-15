from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import pandas as pd

from bot.analyzer.filters import close_beyond_level, finalize_entry_levels
from bot.analyzer.reentry import (
    reentry_has_new_structure_break,
    reentry_price_improved,
    reentry_swing_reset_reached,
)
from bot.config import EntryConfig
from bot.market.pivots import LtfChoCh


@dataclass(slots=True, frozen=True)
class EntryEvaluationContext:
    setup: Any
    confirmation: LtfChoCh
    ltf_df: pd.DataFrame
    row: Any
    entry: EntryConfig
    min_rr: float
    max_entries: int


@dataclass(slots=True, frozen=True)
class EntryDecision:
    accepted: bool
    reason: str
    entry_price: float | None = None
    levels: dict[str, float] | None = None


class EntryStrategy(Protocol):
    name: str

    def evaluate(self, context: EntryEvaluationContext) -> EntryDecision: ...


class StructuralEntryStrategy:
    """Текущая стратегия входа: LTF BOS/CHoCH и подтверждённый re-entry."""

    name = "structural"

    def evaluate(self, context: EntryEvaluationContext) -> EntryDecision:
        setup = context.setup
        confirmation = context.confirmation
        bar_open_ms = int(context.row["open_time"])
        entry_price = float(context.row["close"])
        entry_count = int(setup.entry_count or 0)

        if setup.last_entry_bar_ms is not None and int(setup.last_entry_bar_ms) == bar_open_ms:
            return EntryDecision(False, "entry_skipped_duplicate_bar")
        if entry_count >= context.max_entries:
            return EntryDecision(False, "entry_limit_reached")

        if entry_count > 0:
            if not reentry_has_new_structure_break(
                confirm_broken_open_ms=confirmation.broken_open_ms,
                last_entry_bar_ms=setup.last_entry_bar_ms,
            ):
                return EntryDecision(False, "entry_reentry_wait_new_structure_break")
            if not reentry_swing_reset_reached(
                ltf_df=context.ltf_df,
                direction=str(setup.direction),
                last_entry_bar_ms=setup.last_entry_bar_ms,
                last_entry_swing_level=setup.last_entry_swing_level,
            ):
                return EntryDecision(False, "entry_reentry_wait_reset_swing")

        if context.entry.require_close_beyond_choch and not close_beyond_level(
            entry_price,
            float(confirmation.level),
            str(setup.direction),
        ):
            return EntryDecision(False, "entry_rejected_close_not_beyond_level")

        if entry_count > 0 and not reentry_price_improved(
            direction=str(setup.direction),
            entry_price=entry_price,
            last_entry_price=setup.last_entry_price,
        ):
            return EntryDecision(False, "entry_reentry_wait_better_price")

        levels, reject = finalize_entry_levels(
            entry=entry_price,
            direction=str(setup.direction),
            invalidation_price=float(setup.invalidation_price),
            compute_sl_tp=context.entry.compute_sl_tp,
            min_rr=context.min_rr,
        )
        if reject == "zero_risk":
            return EntryDecision(False, "entry_rejected_zero_risk")
        if reject == "rr_below_min":
            return EntryDecision(False, "entry_rejected_rr_below_min")
        return EntryDecision(True, "entry_accepted", entry_price=entry_price, levels=levels)
