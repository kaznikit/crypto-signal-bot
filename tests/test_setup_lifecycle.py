from __future__ import annotations

import pandas as pd

from bot.analyzer.setup_lifecycle import decide_setup_structure_transition
from bot.market.pivots import StructureBreak


def _df(n: int = 30) -> pd.DataFrame:
    rows = []
    for i in range(n):
        c = 100.0 + i
        rows.append(
            {
                "open_time": i * 60_000,
                "open": c - 0.1,
                "high": c + 0.2,
                "low": c - 0.2,
                "close": c,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_decide_setup_structure_transition_opposite_first() -> None:
    df = _df()
    breaks = [
        StructureBreak("LONG", "BOS", 2, 102.0, 4),
        StructureBreak("SHORT", "CHOCH", 5, 99.0, 8),
        StructureBreak("LONG", "BOS", 9, 105.0, 12),
    ]
    since = int(df.iloc[6]["open_time"])
    decision = decide_setup_structure_transition(
        breaks=breaks,
        df=df,
        setup_direction="LONG",
        since_open_ms=since,
    )
    assert decision.action == "INVALIDATE_OPPOSITE"
    assert decision.trigger is not None
    assert decision.trigger.direction == "SHORT"


def test_decide_setup_structure_transition_same_direction_reset() -> None:
    df = _df()
    breaks = [
        StructureBreak("LONG", "BOS", 2, 102.0, 4),
        StructureBreak("LONG", "BOS", 9, 105.0, 12),
    ]
    since = int(df.iloc[6]["open_time"])
    decision = decide_setup_structure_transition(
        breaks=breaks,
        df=df,
        setup_direction="LONG",
        since_open_ms=since,
    )
    assert decision.action == "RESET_SAME_DIRECTION"
    assert decision.trigger is not None
    assert decision.trigger.direction == "LONG"
    assert decision.trigger.broken_idx == 12


def test_decide_setup_structure_transition_keep() -> None:
    df = _df()
    breaks = [
        StructureBreak("LONG", "BOS", 2, 102.0, 4),
        StructureBreak("SHORT", "CHOCH", 5, 99.0, 8),
    ]
    since = int(df.iloc[12]["open_time"])
    decision = decide_setup_structure_transition(
        breaks=breaks,
        df=df,
        setup_direction="LONG",
        since_open_ms=since,
    )
    assert decision.action == "KEEP"
    assert decision.trigger is None
