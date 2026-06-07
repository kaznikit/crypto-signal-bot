from dataclasses import dataclass

import pandas as pd

from bot.analyzer.entry_advanced import resolve_advanced_entry
from bot.config import EntryConfig
from bot.market.pivots import LtfChoCh


@dataclass
class _Setup:
    direction: str = "LONG"
    is_liberal: bool = False
    prepare_since_ms: int | None = 0
    entry_advanced_stage: str = "WAIT_SWEEP"
    entry_sweep_level: float | None = None
    entry_sweep_extreme: float | None = None
    entry_sweep_ms: int | None = None
    entry_reclaim_ms: int | None = None
    entry_confirm_level: float | None = None
    entry_confirm_ms: int | None = None
    entry_target_price: float | None = 115.0
    entry_mode: str | None = None


def _df(rows: list[tuple[int, float, float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": open_time,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
            for open_time, open_, high, low, close, volume in rows
        ]
    )


def _apply(setup: _Setup, update) -> None:
    setup.entry_advanced_stage = update.stage
    setup.entry_sweep_level = update.sweep_level
    setup.entry_sweep_extreme = update.sweep_extreme
    setup.entry_sweep_ms = update.sweep_ms
    setup.entry_reclaim_ms = update.reclaim_ms
    setup.entry_confirm_level = update.confirm_level
    setup.entry_confirm_ms = update.confirm_ms


def test_advanced_entry_sweep_reclaim_choch_retest(monkeypatch) -> None:
    setup = _Setup()
    entry = EntryConfig(
        mode="advanced",
        ltf_swing_length={"5M": 1},
        advanced={
            "require_displacement": False,
            "max_stop_atr": 10.0,
            "min_rr_to_htf_target": 2.0,
        },
    )
    rows = [
        (1_000, 100.0, 101.0, 99.5, 100.0, 10.0),
        (2_000, 100.0, 101.0, 99.7, 100.2, 10.0),
        (3_000, 100.2, 101.0, 98.0, 100.5, 12.0),
    ]
    monkeypatch.setattr("bot.analyzer.entry_advanced._last_sweep_level", lambda *args, **kwargs: 99.5)

    swept = resolve_advanced_entry(
        setup=setup,
        ltf_df=_df(rows),
        used_tf="5M",
        entry=entry,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )
    assert swept.status == "WAITING_CONFIRM"
    assert swept.wait_suffix == "advanced_choch"
    assert swept.update is not None
    assert swept.update.stage == "WAIT_CHOCH"
    assert swept.update.sweep_extreme == 98.0
    _apply(setup, swept.update)

    rows.append((4_000, 100.5, 103.0, 100.0, 102.5, 20.0))
    choch = LtfChoCh(
        direction="LONG",
        level=101.0,
        bars_ago=0,
        kind="CHOCH",
        broken_open_ms=4_000,
        reset_level=98.0,
    )
    monkeypatch.setattr(
        "bot.analyzer.entry_advanced.detect_entry_structure_confirm",
        lambda **kwargs: choch,
    )
    confirmed_structure = resolve_advanced_entry(
        setup=setup,
        ltf_df=_df(rows),
        used_tf="5M",
        entry=entry,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )
    assert confirmed_structure.wait_suffix == "advanced_retest"
    assert confirmed_structure.update is not None
    assert confirmed_structure.update.stage == "WAIT_RETEST"
    _apply(setup, confirmed_structure.update)

    rows.append((5_000, 102.5, 102.8, 100.9, 101.8, 15.0))
    retest = resolve_advanced_entry(
        setup=setup,
        ltf_df=_df(rows),
        used_tf="5M",
        entry=entry,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )
    assert retest.status == "CONFIRMED"
    assert retest.recommended_stop is not None
    assert 98.0 < retest.recommended_stop < 100.9
    assert retest.recommended_stop_source == "retest_extreme"
    assert retest.target_price == 115.0
    assert retest.rr_to_target is not None
    assert retest.rr_to_target >= 2.0


def test_advanced_entry_resets_when_sweep_extreme_breaks() -> None:
    setup = _Setup(
        entry_advanced_stage="WAIT_CHOCH",
        entry_sweep_level=99.5,
        entry_sweep_extreme=98.0,
        entry_sweep_ms=2_000,
        entry_reclaim_ms=2_000,
    )
    entry = EntryConfig(mode="advanced")
    result = resolve_advanced_entry(
        setup=setup,
        ltf_df=_df(
            [
                (1_000, 100.0, 101.0, 99.0, 100.0, 10.0),
                (2_000, 100.0, 101.0, 98.0, 100.5, 10.0),
                (3_000, 100.5, 101.0, 97.5, 98.5, 10.0),
            ]
        ),
        used_tf="5M",
        entry=entry,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )
    assert result.wait_suffix == "advanced_sweep"
    assert result.update is not None
    assert result.update.stage == "WAIT_SWEEP"
    assert result.update.sweep_extreme is None


def test_sweep_reclaim_confirms_on_same_candle(monkeypatch) -> None:
    setup = _Setup(entry_mode="sweep_reclaim")
    entry = EntryConfig(
        mode="sweep_reclaim",
        ltf_swing_length={"5M": 1},
        advanced={
            "require_displacement": False,
            "max_stop_atr": 10.0,
            "min_rr_to_htf_target": 2.0,
        },
    )
    monkeypatch.setattr("bot.analyzer.entry_advanced._last_sweep_level", lambda *args, **kwargs: 99.5)

    result = resolve_advanced_entry(
        setup=setup,
        ltf_df=_df(
            [
                (1_000, 100.0, 101.0, 99.5, 100.0, 10.0),
                (2_000, 100.0, 101.0, 99.7, 100.2, 10.0),
                (3_000, 100.2, 101.0, 98.8, 100.4, 12.0),
            ]
        ),
        used_tf="5M",
        entry=entry,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "CONFIRMED"
    assert result.choch is not None
    assert result.choch.kind == "RECLAIM"
    assert result.recommended_stop is not None
    assert result.recommended_stop < 98.8
    assert result.recommended_stop_source == "sweep_extreme"


def test_sweep_reclaim_confirms_after_later_reclaim() -> None:
    setup = _Setup(
        entry_mode="sweep_reclaim",
        entry_advanced_stage="WAIT_RECLAIM",
        entry_sweep_level=99.5,
        entry_sweep_extreme=98.8,
        entry_sweep_ms=2_000,
    )
    entry = EntryConfig(
        mode="sweep_reclaim",
        advanced={
            "require_displacement": False,
            "max_stop_atr": 10.0,
            "min_rr_to_htf_target": 2.0,
        },
    )
    result = resolve_advanced_entry(
        setup=setup,
        ltf_df=_df(
            [
                (1_000, 100.0, 101.0, 99.0, 100.0, 10.0),
                (2_000, 100.0, 100.2, 98.8, 99.0, 10.0),
                (3_000, 99.0, 100.5, 98.9, 100.0, 12.0),
            ]
        ),
        used_tf="5M",
        entry=entry,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "CONFIRMED"
    assert result.choch is not None
    assert result.choch.kind == "RECLAIM"


def test_sweep_reclaim_directional_body_filter_is_optional(monkeypatch) -> None:
    setup = _Setup(entry_mode="sweep_reclaim")
    monkeypatch.setattr("bot.analyzer.entry_advanced._last_sweep_level", lambda *args, **kwargs: 99.5)
    candles = _df(
        [
            (1_000, 100.0, 101.0, 99.5, 100.0, 10.0),
            (2_000, 100.0, 101.0, 99.7, 100.2, 10.0),
            (3_000, 101.0, 101.2, 98.8, 100.0, 12.0),
        ]
    )
    loose = EntryConfig(
        mode="sweep_reclaim",
        ltf_swing_length={"5M": 1},
        advanced={
            "require_displacement": True,
            "require_directional_reclaim": False,
            "displacement_body_atr_min": 0.1,
            "max_stop_atr": 10.0,
            "min_rr_to_htf_target": 2.0,
        },
    )
    strict = EntryConfig(
        mode="sweep_reclaim",
        ltf_swing_length={"5M": 1},
        advanced={
            "require_displacement": True,
            "require_directional_reclaim": True,
            "displacement_body_atr_min": 0.1,
            "max_stop_atr": 10.0,
            "min_rr_to_htf_target": 2.0,
        },
    )

    loose_result = resolve_advanced_entry(
        setup=setup,
        ltf_df=candles,
        used_tf="5M",
        entry=loose,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )
    strict_result = resolve_advanced_entry(
        setup=setup,
        ltf_df=candles,
        used_tf="5M",
        entry=strict,
        pivot_swing_by_tf=None,
        liberal_swing_override=None,
        use_close=True,
    )

    assert loose_result.status == "CONFIRMED"
    assert strict_result.wait_suffix == "sweep_reclaim_displacement"
