from __future__ import annotations

import pandas as pd

from bot.analyzer.structure_state import resolve_prepare_structure_state
from bot.market.pivots import ImpulseLeg, StructureBreak


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    out = []
    for i, (o, h, low, c) in enumerate(rows):
        out.append(
            {
                "open_time": i * 60_000,
                "open": o,
                "high": h,
                "low": low,
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


def test_resolve_prepare_structure_state_allows_leg_if_end_matches_structure_swing() -> None:
    # LONG: текущий break пробивает swing на idx=5, а leg.anchor старее.
    # Нога остаётся валидной, если её end >= swing_idx (здесь end == swing_idx).
    df = _df(
        [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 102.0, 99.5, 101.0),
            (101.0, 103.0, 100.0, 102.0),
            (102.0, 104.0, 101.0, 103.0),
            (103.0, 105.0, 102.0, 104.5),
            (104.5, 107.0, 104.0, 106.5),  # swing HIGH
            (106.5, 108.0, 105.5, 107.5),  # break бар
            (107.5, 108.0, 103.5, 105.5),  # touch 0.5
        ]
    )
    br = StructureBreak(
        direction="LONG",
        kind="BOS",
        swing_idx=5,
        swing_price=107.0,
        broken_idx=6,
    )
    legs = [
        ImpulseLeg(
            direction="LONG",
            start_idx=3,
            start_price=101.0,
            end_idx=5,
            end_price=107.0,
            anchor_break_idx=2,  # старее текущего break
        )
    ]
    state = resolve_prepare_structure_state(
        df=df,
        legs=legs,
        structure_break=br,
        structure_direction="LONG",
        fib_level=0.5,
        impulse_max_age_bars=20,
        swing_size=1,
        last_pos=int(df.index[-1]),
    )
    assert state is not None
    assert state.touch_idx == 7
    assert state.impulse.end_idx == 5
