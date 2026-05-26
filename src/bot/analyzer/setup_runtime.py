from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from bot.analyzer.entry_ltf import (
    finest_closed_ltf,
    invalidation_tf_for_setup,
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
    status: str  # "NO_MATCHING_LTF" | "LTF_NOT_CLOSED" | "WAITING_CONFIRM" | "CONFIRMED"
    used_tf: str | None = None
    ltf_df: pd.DataFrame | None = None
    row: Any | None = None
    choch: LtfChoCh | None = None
    wait_suffix: str | None = None


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
