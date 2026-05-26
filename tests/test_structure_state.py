from __future__ import annotations

import pandas as pd

from bot.analyzer.structure_state import resolve_prepare_structure_state
from bot.market.pivots import ImpulseLeg, StructureBreak


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        out.append(
            {
                "open_time": i * 60_000,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(out)


def test_resolve_prepare_structure_state_long_first_touch() -> None:
    # level_50 = (100 + 120) / 2 = 110; first touch на последнем баре.
    df = _df(
        [
            (100.0, 101.0, 99.0, 100.0),
            (101.0, 103.0, 100.0, 102.0),
            (102.0, 106.0, 101.0, 105.0),
            (105.0, 112.0, 104.0, 111.0),
            (111.0, 120.0, 110.5, 119.0),
            (119.0, 119.5, 109.5, 110.0),
        ]
    )
    br = StructureBreak(
        direction="LONG",
        kind="BOS",
        swing_idx=3,
        swing_price=112.0,
        broken_idx=4,
    )
    legs = [
        ImpulseLeg(
            direction="LONG",
            start_idx=1,
            start_price=100.0,
            end_idx=4,
            end_price=120.0,
            anchor_break_idx=4,
        )
    ]
    state = resolve_prepare_structure_state(
        df=df,
        legs=legs,
        structure_break=br,
        structure_direction="LONG",
        fib_level=0.5,
        impulse_max_age_bars=20,
        swing_size=3,
        last_pos=int(df.index[-1]),
    )
    assert state is not None
    assert state.direction == "LONG"
    assert state.level_50 == 110.0
    assert state.touch_idx == 5
    assert state.retrace_label == "HL"
    assert state.retrace_price == 100.0


def test_resolve_prepare_structure_state_returns_none_without_touch() -> None:
    df = _df(
        [
            (100.0, 101.0, 99.0, 100.0),
            (101.0, 103.0, 100.0, 102.0),
            (102.0, 106.0, 101.0, 105.0),
            (105.0, 112.0, 104.0, 111.0),
            (111.0, 120.0, 111.0, 119.0),
            (119.0, 121.0, 115.0, 120.0),
        ]
    )
    br = StructureBreak(
        direction="LONG",
        kind="BOS",
        swing_idx=3,
        swing_price=112.0,
        broken_idx=4,
    )
    legs = [
        ImpulseLeg(
            direction="LONG",
            start_idx=1,
            start_price=100.0,
            end_idx=4,
            end_price=120.0,
            anchor_break_idx=4,
        )
    ]
    state = resolve_prepare_structure_state(
        df=df,
        legs=legs,
        structure_break=br,
        structure_direction="LONG",
        fib_level=0.5,
        impulse_max_age_bars=20,
        swing_size=3,
        last_pos=int(df.index[-1]),
    )
    assert state is None
