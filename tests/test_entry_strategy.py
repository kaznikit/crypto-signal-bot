from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from bot.config import EntryConfig
from bot.entry_engine import (
    EntryEvaluationContext,
    StructuralEntryStrategy,
    build_entry_strategy,
)
from bot.market.pivots import LtfChoCh


@dataclass(slots=True)
class _Setup:
    direction: str = "LONG"
    invalidation_price: float = 90.0
    entry_count: int = 0
    last_entry_bar_ms: int | None = None
    last_entry_price: float | None = None
    last_entry_swing_level: float | None = None
    entry_target_price: float | None = 120.0


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": 1_000,
                "open": 98.0,
                "high": 101.0,
                "low": 97.0,
                "close": 100.0,
                "volume": 1.0,
            }
        ]
    )


def test_structural_entry_accepts_primary_signal() -> None:
    df = _df()
    decision = StructuralEntryStrategy().evaluate(
        EntryEvaluationContext(
            setup=_Setup(),
            confirmation=LtfChoCh(direction="LONG", level=99.0, bars_ago=0, kind="CHOCH"),
            ltf_df=df,
            row=df.iloc[-1],
            entry=EntryConfig(compute_sl_tp=True, require_close_beyond_choch=True),
            min_rr=1.5,
            max_entries=2,
        )
    )

    assert decision.accepted is True
    assert decision.entry_price == 100.0
    assert decision.levels == {"sl": 90.0, "tp": 120.0, "tp1": 120.0}


def test_structural_entry_rejects_duplicate_bar() -> None:
    df = _df()
    decision = StructuralEntryStrategy().evaluate(
        EntryEvaluationContext(
            setup=_Setup(last_entry_bar_ms=1_000),
            confirmation=LtfChoCh(direction="LONG", level=99.0, bars_ago=0, kind="CHOCH"),
            ltf_df=df,
            row=df.iloc[-1],
            entry=EntryConfig(),
            min_rr=1.5,
            max_entries=2,
        )
    )

    assert decision.accepted is False
    assert decision.reason == "entry_skipped_duplicate_bar"


def test_build_entry_strategy_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown entry strategy"):
        build_entry_strategy("unknown")
