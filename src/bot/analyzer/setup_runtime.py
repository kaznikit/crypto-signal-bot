from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from bot.analyzer.entry_advanced import AdvancedEntryUpdate, resolve_advanced_entry
from bot.analyzer.entry_ltf import (
    cascade_sequence_for_htf,
    detect_entry_structure_confirm,
    finest_closed_ltf,
    invalidation_tf_for_setup,
    prepare_since_open_ms,
    try_entry_confirm,
)
from bot.config import EntryConfig
from bot.market.pivots import LtfChoCh


@dataclass(slots=True, frozen=True)
class PriceInvalidationResult:
    status: str  # "NOT_TRIGGERED" | "TRIGGERED"
    inv_tf: str
    row: Any | None = None

    @property
    def invalidated(self) -> bool:
        return self.status == "TRIGGERED"


@dataclass(slots=True, frozen=True)
class LtfConfirmationResult:
    status: str  # NO_MATCHING_LTF | LTF_NOT_CLOSED | WAITING_CONFIRM | CASCADE_ADVANCED | CONFIRMED
    used_tf: str | None = None
    ltf_df: pd.DataFrame | None = None
    row: Any | None = None
    choch: LtfChoCh | None = None
    wait_suffix: str | None = None
    cascade_update: EntryCascadeUpdate | None = None
    advanced_update: AdvancedEntryUpdate | None = None
    recommended_stop: float | None = None
    recommended_stop_source: str | None = None
    target_price: float | None = None
    rr_to_target: float | None = None


@dataclass(slots=True, frozen=True)
class EntryCascadeUpdate:
    stage: int
    since_ms: int | None
    touch_ms: int | None
    retrace_level: float | None


def check_price_invalidation(
    *,
    setup: Any,
    series: dict[str, Any],
    entry: EntryConfig,
) -> PriceInvalidationResult:
    series_keys = set(series.keys())
    inv_tf = invalidation_tf_for_setup(
        str(setup.htf),
        str(setup.ltf_expected),
        entry,
        series_keys,
    )
    inv_df = series.get(inv_tf)
    if inv_df is None or inv_df.empty:
        return PriceInvalidationResult(status="NOT_TRIGGERED", inv_tf=inv_tf, row=None)

    row = inv_df.iloc[-1]
    if str(setup.direction) == "LONG" and float(row["low"]) <= float(setup.invalidation_price):
        return PriceInvalidationResult(status="TRIGGERED", inv_tf=inv_tf, row=row)
    if str(setup.direction) == "SHORT" and float(row["high"]) >= float(setup.invalidation_price):
        return PriceInvalidationResult(status="TRIGGERED", inv_tf=inv_tf, row=row)
    return PriceInvalidationResult(status="NOT_TRIGGERED", inv_tf=inv_tf, row=row)


def _setup_cascade_stage(setup: Any, sequence: list[str]) -> int:
    raw = int(getattr(setup, "entry_cascade_stage", 0) or 0)
    if raw < 0 or raw >= len(sequence):
        return 0
    return raw


def _cascade_initial_since_ms(setup: Any) -> int:
    last_entry_bar_ms = getattr(setup, "last_entry_bar_ms", None)
    if last_entry_bar_ms is not None:
        return int(last_entry_bar_ms)
    return prepare_since_open_ms(setup)


def _resolve_cascade_ltf_confirmation(
    *,
    setup: Any,
    series: dict[str, Any],
    closed_tfs: list[str],
    entry: EntryConfig,
    pivot_swing_by_tf: dict[str, int] | None,
    liberal_swing_override: dict[str, int] | None,
    use_close: bool,
    sequence: list[str],
) -> LtfConfirmationResult:
    series_keys = set(series.keys())
    stage = _setup_cascade_stage(setup, sequence)
    used_tf = sequence[stage]

    if used_tf not in series_keys:
        if not any(tf in series_keys for tf in sequence):
            return LtfConfirmationResult(status="NO_MATCHING_LTF")
        return LtfConfirmationResult(status="LTF_NOT_CLOSED", used_tf=used_tf)
    if used_tf not in set(closed_tfs):
        return LtfConfirmationResult(status="LTF_NOT_CLOSED", used_tf=used_tf)

    ltf_df = series.get(used_tf)
    if ltf_df is None or ltf_df.empty:
        return LtfConfirmationResult(status="LTF_NOT_CLOSED", used_tf=used_tf)

    row = ltf_df.iloc[-1]
    cascade_update: EntryCascadeUpdate | None = None
    since_open_ms = _cascade_initial_since_ms(setup)

    if stage > 0:
        previous_break_ms = getattr(setup, "entry_cascade_since_ms", None)
        if previous_break_ms is None:
            cascade_update = EntryCascadeUpdate(
                stage=0,
                since_ms=None,
                touch_ms=None,
                retrace_level=None,
            )
            return LtfConfirmationResult(
                status="WAITING_CONFIRM",
                used_tf=used_tf,
                ltf_df=ltf_df,
                row=row,
                wait_suffix="cascade_reset",
                cascade_update=cascade_update,
            )
        since_open_ms = int(previous_break_ms) + 1

    kinds = tuple(k.upper() for k in entry.cascade_confirm_structure_kinds) or ("BOS",)
    choch = detect_entry_structure_confirm(
        entry=entry,
        ltf_df=ltf_df,
        used_tf=used_tf,
        direction=str(setup.direction),
        since_open_ms=since_open_ms,
        pivot_swing_by_tf=pivot_swing_by_tf,
        liberal_swing_override=liberal_swing_override,
        is_liberal=bool(getattr(setup, "is_liberal", False)),
        use_close=use_close,
        structure_kinds=kinds,
        lookback_mode="since_prepare",
    )
    if choch is None:
        return LtfConfirmationResult(
            status="WAITING_CONFIRM",
            used_tf=used_tf,
            ltf_df=ltf_df,
            row=row,
            wait_suffix=f"cascade_{used_tf.lower()}",
            cascade_update=cascade_update,
        )

    if stage == len(sequence) - 1:
        return LtfConfirmationResult(
            status="CONFIRMED",
            used_tf=used_tf,
            ltf_df=ltf_df,
            row=row,
            choch=choch,
            cascade_update=cascade_update,
        )

    if choch.broken_open_ms is None:
        return LtfConfirmationResult(
            status="WAITING_CONFIRM",
            used_tf=used_tf,
            ltf_df=ltf_df,
            row=row,
            choch=choch,
            wait_suffix="cascade_break_time",
            cascade_update=cascade_update,
        )

    next_update = EntryCascadeUpdate(
        stage=stage + 1,
        since_ms=int(choch.broken_open_ms),
        touch_ms=None,
        retrace_level=None,
    )
    return LtfConfirmationResult(
        status="CASCADE_ADVANCED",
        used_tf=used_tf,
        ltf_df=ltf_df,
        row=row,
        choch=choch,
        cascade_update=next_update,
    )


def resolve_ltf_confirmation(
    *,
    setup: Any,
    series: dict[str, Any],
    closed_tfs: list[str],
    entry: EntryConfig,
    pivot_swing_by_tf: dict[str, int] | None,
    liberal_swing_override: dict[str, int] | None,
    use_close: bool,
) -> LtfConfirmationResult:
    setup_mode = str(getattr(setup, "entry_mode", None) or entry.mode).lower()
    if setup_mode in {"advanced", "sweep_reclaim"}:
        series_keys = set(series.keys())
        expected = [part.strip() for part in str(setup.ltf_expected).split("|") if part.strip()]
        used_tf = finest_closed_ltf(
            str(setup.ltf_expected),
            closed_tfs=closed_tfs,
            available=series_keys,
        )
        if used_tf is None:
            if not any(tf in series_keys for tf in expected):
                return LtfConfirmationResult(status="NO_MATCHING_LTF")
            return LtfConfirmationResult(status="LTF_NOT_CLOSED")
        ltf_df = series.get(used_tf)
        if ltf_df is None or ltf_df.empty:
            return LtfConfirmationResult(status="LTF_NOT_CLOSED")
        advanced = resolve_advanced_entry(
            setup=setup,
            ltf_df=ltf_df,
            used_tf=used_tf,
            entry=entry,
            pivot_swing_by_tf=pivot_swing_by_tf,
            liberal_swing_override=liberal_swing_override,
            use_close=use_close,
        )
        return LtfConfirmationResult(
            status=advanced.status,
            used_tf=used_tf,
            ltf_df=ltf_df,
            row=ltf_df.iloc[-1],
            choch=advanced.choch,
            wait_suffix=advanced.wait_suffix,
            advanced_update=advanced.update,
            recommended_stop=advanced.recommended_stop,
            recommended_stop_source=advanced.recommended_stop_source,
            target_price=advanced.target_price,
            rr_to_target=advanced.rr_to_target,
        )

    sequence = cascade_sequence_for_htf(str(setup.htf), entry)
    if sequence:
        return _resolve_cascade_ltf_confirmation(
            setup=setup,
            series=series,
            closed_tfs=closed_tfs,
            entry=entry,
            pivot_swing_by_tf=pivot_swing_by_tf,
            liberal_swing_override=liberal_swing_override,
            use_close=use_close,
            sequence=sequence,
        )

    series_keys = set(series.keys())
    expected = [part.strip() for part in str(setup.ltf_expected).split("|") if part.strip()]
    used_tf = finest_closed_ltf(
        str(setup.ltf_expected),
        closed_tfs=closed_tfs,
        available=series_keys,
    )
    if used_tf is None:
        if not any(tf in series_keys for tf in expected):
            return LtfConfirmationResult(status="NO_MATCHING_LTF")
        return LtfConfirmationResult(status="LTF_NOT_CLOSED")

    ltf_df = series.get(used_tf)
    if ltf_df is None or ltf_df.empty:
        return LtfConfirmationResult(status="LTF_NOT_CLOSED")

    ok, choch = try_entry_confirm(
        entry=entry,
        ltf_df=ltf_df,
        used_tf=used_tf,
        setup=setup,
        pivot_swing_by_tf=pivot_swing_by_tf,
        liberal_swing_override=liberal_swing_override,
        is_liberal=bool(getattr(setup, "is_liberal", False)),
        use_close=use_close,
    )
    if not ok or choch is None:
        wait_suffix = (
            "directional_close"
            if (entry.confirm_mode or "structure_break") == "directional_close"
            else "structure"
        )
        return LtfConfirmationResult(
            status="WAITING_CONFIRM",
            used_tf=used_tf,
            ltf_df=ltf_df,
            row=ltf_df.iloc[-1],
            wait_suffix=wait_suffix,
        )

    return LtfConfirmationResult(
        status="CONFIRMED",
        used_tf=used_tf,
        ltf_df=ltf_df,
        row=ltf_df.iloc[-1],
        choch=choch,
    )
