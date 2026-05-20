import pandas as pd

from bot.market.fibo import fib_level
from bot.market.pivots import ImpulseLeg, first_touch_of_level_since


def test_fib_level_long() -> None:
    imp = ImpulseLeg(
        direction="LONG", start_idx=0, start_price=100.0, end_idx=1, end_price=200.0
    )
    assert fib_level(imp, 0.5) == 150.0
    assert fib_level(imp, 0.0) == 200.0
    assert fib_level(imp, 1.0) == 100.0


def test_fib_level_short() -> None:
    imp = ImpulseLeg(
        direction="SHORT", start_idx=0, start_price=200.0, end_idx=1, end_price=100.0
    )
    assert fib_level(imp, 0.5) == 150.0
    assert fib_level(imp, 0.0) == 100.0
    assert fib_level(imp, 1.0) == 200.0


def test_impulse_leg_fib_half_matches_pine() -> None:
    """Pine рисует 0.5-линию как ``(prevLow + pivHi) / 2`` для LONG-leg'а.
    ``ImpulseLeg.fib_half`` обязан давать ровно ту же цифру."""
    long_leg = ImpulseLeg(
        direction="LONG", start_idx=0, start_price=100.0, end_idx=10, end_price=180.0
    )
    short_leg = ImpulseLeg(
        direction="SHORT", start_idx=0, start_price=300.0, end_idx=10, end_price=200.0
    )
    assert long_leg.fib_half == 140.0
    assert short_leg.fib_half == 250.0


def _df_from_ohlc(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": i * 60_000,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 100.0,
            }
            for i, (o, h, l, c) in enumerate(rows)
        ]
    )


def test_first_touch_long_fires_after_clean_retrace() -> None:
    df = _df_from_ohlc(
        [
            (100.0, 110.0, 100.0, 105.0),
            (105.0, 200.0, 105.0, 195.0),  # peak idx=1
            (195.0, 196.0, 180.0, 185.0),  # low 180 > 150
            (185.0, 190.0, 170.0, 175.0),  # low 170 > 150
            (175.0, 180.0, 149.0, 152.0),  # FIRST touch (low=149)
        ]
    )
    assert first_touch_of_level_since(
        df, direction="LONG", level=150.0, since_idx=1
    )


def test_first_touch_long_no_fire_if_visited_before() -> None:
    df = _df_from_ohlc(
        [
            (100.0, 110.0, 100.0, 105.0),
            (105.0, 200.0, 105.0, 195.0),  # peak idx=1
            (195.0, 196.0, 145.0, 160.0),  # already below 150
            (160.0, 170.0, 155.0, 165.0),
            (165.0, 175.0, 148.0, 155.0),  # re-touch — must NOT fire
        ]
    )
    assert not first_touch_of_level_since(
        df, direction="LONG", level=150.0, since_idx=1
    )


def test_first_touch_long_no_fire_at_peak_bar() -> None:
    df = _df_from_ohlc(
        [
            (100.0, 110.0, 100.0, 105.0),
            (105.0, 150.0, 105.0, 145.0),
            (145.0, 200.0, 130.0, 195.0),  # peak = last bar → still in impulse
        ]
    )
    assert not first_touch_of_level_since(
        df, direction="LONG", level=150.0, since_idx=2
    )


def test_first_touch_short_fires_on_retrace_up() -> None:
    df = _df_from_ohlc(
        [
            (200.0, 200.0, 190.0, 195.0),
            (195.0, 195.0, 100.0, 105.0),  # peak idx=1
            (105.0, 120.0, 105.0, 115.0),
            (115.0, 140.0, 110.0, 135.0),
            (135.0, 151.0, 130.0, 148.0),  # FIRST touch high=151 of 150
        ]
    )
    assert first_touch_of_level_since(
        df, direction="SHORT", level=150.0, since_idx=1
    )


def test_first_touch_long_single_retrace_bar() -> None:
    df = _df_from_ohlc(
        [
            (100.0, 110.0, 100.0, 105.0),
            (105.0, 200.0, 105.0, 195.0),  # peak idx=1
            (195.0, 196.0, 149.0, 152.0),  # single retrace bar pierces 150
        ]
    )
    assert first_touch_of_level_since(
        df, direction="LONG", level=150.0, since_idx=1
    )
