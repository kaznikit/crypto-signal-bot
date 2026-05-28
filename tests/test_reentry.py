import pandas as pd

from bot.analyzer.reentry import (
    reentry_has_new_structure_break,
    reentry_price_improved,
    reentry_swing_reset_reached,
)


def _ltf_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"open_time": 1000, "low": 10.0, "high": 12.0},
            {"open_time": 2000, "low": 9.5, "high": 11.5},
            {"open_time": 3000, "low": 8.9, "high": 10.5},
            {"open_time": 4000, "low": 9.2, "high": 11.0},
        ]
    )


def test_reentry_swing_reset_long_true_after_break() -> None:
    df = _ltf_df()
    assert reentry_swing_reset_reached(
        ltf_df=df,
        direction="LONG",
        last_entry_bar_ms=2000,
        last_entry_swing_level=9.0,
    )


def test_reentry_swing_reset_long_false_without_break() -> None:
    df = _ltf_df()
    assert not reentry_swing_reset_reached(
        ltf_df=df,
        direction="LONG",
        last_entry_bar_ms=2000,
        last_entry_swing_level=8.8,
    )


def test_reentry_swing_reset_short_true_after_break() -> None:
    df = _ltf_df()
    assert reentry_swing_reset_reached(
        ltf_df=df,
        direction="SHORT",
        last_entry_bar_ms=2000,
        last_entry_swing_level=10.8,
    )


def test_reentry_swing_reset_requires_known_level() -> None:
    df = _ltf_df()
    assert not reentry_swing_reset_reached(
        ltf_df=df,
        direction="LONG",
        last_entry_bar_ms=2000,
        last_entry_swing_level=None,
    )


def test_reentry_requires_new_structure_break_after_last_entry() -> None:
    assert not reentry_has_new_structure_break(confirm_broken_open_ms=None, last_entry_bar_ms=2000)
    assert not reentry_has_new_structure_break(confirm_broken_open_ms=2000, last_entry_bar_ms=2000)
    assert not reentry_has_new_structure_break(confirm_broken_open_ms=1500, last_entry_bar_ms=2000)
    assert reentry_has_new_structure_break(confirm_broken_open_ms=2500, last_entry_bar_ms=2000)


def test_reentry_price_improved_rules() -> None:
    assert reentry_price_improved(direction="LONG", entry_price=99.0, last_entry_price=100.0)
    assert not reentry_price_improved(direction="LONG", entry_price=101.0, last_entry_price=100.0)
    assert reentry_price_improved(direction="SHORT", entry_price=101.0, last_entry_price=100.0)
    assert not reentry_price_improved(direction="SHORT", entry_price=99.0, last_entry_price=100.0)
