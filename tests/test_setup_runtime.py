from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.analyzer.setup_runtime import check_price_invalidation, resolve_ltf_confirmation
from bot.config import EntryConfig
from bot.market.pivots import LtfChoCh


@dataclass(slots=True)
class _Setup:
    htf: str
    ltf_expected: str
    direction: str
    invalidation_price: float
    is_liberal: bool = False
    prepare_since_ms: int | None = None
    entry_cascade_stage: int = 0
    entry_cascade_since_ms: int | None = None
    entry_cascade_touch_ms: int | None = None
    entry_cascade_retrace_level: float | None = None
    last_entry_bar_ms: int | None = None


def _df(
    *,
    open_time: int = 1_000,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": open_time,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1.0,
            }
        ]
    )


def test_check_price_invalidation_triggers_for_long() -> None:
    setup = _Setup(htf="1H", ltf_expected="5M|15M", direction="LONG", invalidation_price=95.0)
    series = {"1H": _df(low=94.5, high=101.0, close=96.0)}

    result = check_price_invalidation(setup=setup, series=series, entry=EntryConfig())

    assert result.invalidated is True
    assert result.status == "TRIGGERED"
    assert result.inv_tf == "1H"


def test_check_price_invalidation_triggers_for_short() -> None:
    setup = _Setup(htf="1H", ltf_expected="5M|15M", direction="SHORT", invalidation_price=105.0)
    series = {"1H": _df(low=99.0, high=105.2, close=104.8)}

    result = check_price_invalidation(setup=setup, series=series, entry=EntryConfig())

    assert result.invalidated is True
    assert result.status == "TRIGGERED"
    assert result.inv_tf == "1H"


def test_resolve_ltf_confirmation_no_matching_ltf() -> None:
    setup = _Setup(htf="1H", ltf_expected="5M|15M", direction="LONG", invalidation_price=95.0)
    series = {"1H": _df()}

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=["5M", "15M"],
        entry=EntryConfig(),
        pivot_swing_by_tf={"5M": 5, "15M": 8},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "NO_MATCHING_LTF"
    assert result.used_tf is None
    assert result.choch is None


def test_resolve_ltf_confirmation_ltf_not_closed() -> None:
    setup = _Setup(htf="1H", ltf_expected="5M|15M", direction="LONG", invalidation_price=95.0)
    series = {"15M": _df()}

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=[],
        entry=EntryConfig(),
        pivot_swing_by_tf={"15M": 8},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "LTF_NOT_CLOSED"
    assert result.used_tf is None
    assert result.choch is None


def test_resolve_ltf_confirmation_waiting_confirm(monkeypatch) -> None:
    setup = _Setup(htf="1H", ltf_expected="15M", direction="LONG", invalidation_price=95.0)
    series = {"15M": _df()}

    monkeypatch.setattr(
        "bot.analyzer.setup_runtime.try_entry_confirm",
        lambda **kwargs: (False, None),
    )

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=["15M"],
        entry=EntryConfig(),
        pivot_swing_by_tf={"15M": 8},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "WAITING_CONFIRM"
    assert result.used_tf == "15M"
    assert result.row is not None
    assert result.wait_suffix == "structure"


def test_resolve_ltf_confirmation_confirmed(monkeypatch) -> None:
    setup = _Setup(htf="1H", ltf_expected="15M", direction="SHORT", invalidation_price=105.0)
    series = {"15M": _df(open_=100.0, high=101.0, low=98.0, close=98.5)}
    choch = LtfChoCh(direction="SHORT", level=99.0, bars_ago=0, kind="BOS")

    monkeypatch.setattr(
        "bot.analyzer.setup_runtime.try_entry_confirm",
        lambda **kwargs: (True, choch),
    )

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=["15M"],
        entry=EntryConfig(),
        pivot_swing_by_tf={"15M": 8},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "CONFIRMED"
    assert result.used_tf == "15M"
    assert result.choch is choch
    assert result.row is not None


def test_cascade_advances_after_first_tf_bos(monkeypatch) -> None:
    setup = _Setup(
        htf="1H",
        ltf_expected="15M|5M|1M",
        direction="LONG",
        invalidation_price=95.0,
        prepare_since_ms=500,
    )
    series = {"15M": _df(open_time=1_000)}
    entry = EntryConfig(cascade_enabled=True, cascade_by_htf={"1H": "15M|5M|1M"})
    choch = LtfChoCh(
        direction="LONG",
        level=101.0,
        bars_ago=0,
        kind="BOS",
        broken_open_ms=1_000,
    )

    monkeypatch.setattr(
        "bot.analyzer.setup_runtime.detect_entry_structure_confirm",
        lambda **kwargs: choch,
    )
    monkeypatch.setattr(
        "bot.analyzer.setup_runtime.cascade_retrace_level_for_confirm",
        lambda **kwargs: 99.5,
    )

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=["15M"],
        entry=entry,
        pivot_swing_by_tf={"15M": 8},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "CASCADE_ADVANCED"
    assert result.used_tf == "15M"
    assert result.cascade_update is not None
    assert result.cascade_update.stage == 1
    assert result.cascade_update.since_ms == 1_000
    assert result.cascade_update.touch_ms is None
    assert result.cascade_update.retrace_level == 99.5


def test_cascade_waits_retrace_before_next_tf_bos() -> None:
    setup = _Setup(
        htf="1H",
        ltf_expected="15M|5M|1M",
        direction="LONG",
        invalidation_price=95.0,
        entry_cascade_stage=1,
        entry_cascade_since_ms=1_000,
        entry_cascade_retrace_level=99.0,
    )
    series = {"5M": _df(open_time=2_000, low=100.0)}
    entry = EntryConfig(cascade_enabled=True, cascade_by_htf={"1H": "15M|5M|1M"})

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=["5M"],
        entry=entry,
        pivot_swing_by_tf={"5M": 5},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "WAITING_RETRACE"
    assert result.used_tf == "5M"
    assert result.cascade_update is None


def test_cascade_records_retrace_touch_before_confirm(monkeypatch) -> None:
    setup = _Setup(
        htf="1H",
        ltf_expected="15M|5M|1M",
        direction="LONG",
        invalidation_price=95.0,
        entry_cascade_stage=1,
        entry_cascade_since_ms=1_000,
        entry_cascade_retrace_level=99.0,
    )
    series = {"5M": _df(open_time=2_000, low=98.5)}
    entry = EntryConfig(cascade_enabled=True, cascade_by_htf={"1H": "15M|5M|1M"})

    monkeypatch.setattr(
        "bot.analyzer.setup_runtime.detect_entry_structure_confirm",
        lambda **kwargs: None,
    )

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=["5M"],
        entry=entry,
        pivot_swing_by_tf={"5M": 5},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "WAITING_CONFIRM"
    assert result.cascade_update is not None
    assert result.cascade_update.stage == 1
    assert result.cascade_update.touch_ms == 2_000


def test_cascade_confirms_only_on_final_tf(monkeypatch) -> None:
    setup = _Setup(
        htf="1H",
        ltf_expected="15M|5M|1M",
        direction="SHORT",
        invalidation_price=105.0,
        entry_cascade_stage=2,
        entry_cascade_since_ms=3_000,
        entry_cascade_touch_ms=4_000,
        entry_cascade_retrace_level=101.0,
    )
    series = {"1M": _df(open_time=5_000, open_=100.0, high=101.0, low=98.0, close=98.5)}
    entry = EntryConfig(cascade_enabled=True, cascade_by_htf={"1H": "15M|5M|1M"})
    choch = LtfChoCh(
        direction="SHORT",
        level=99.0,
        bars_ago=0,
        kind="BOS",
        broken_open_ms=5_000,
    )
    calls: list[dict] = []

    def _fake_confirm(**kwargs):
        calls.append(kwargs)
        return choch

    monkeypatch.setattr(
        "bot.analyzer.setup_runtime.detect_entry_structure_confirm",
        _fake_confirm,
    )

    result = resolve_ltf_confirmation(
        setup=setup,
        series=series,
        closed_tfs=["1M"],
        entry=entry,
        pivot_swing_by_tf={"1M": 4},
        liberal_swing_override=None,
        use_close=True,
    )

    assert result.status == "CONFIRMED"
    assert result.used_tf == "1M"
    assert result.choch is choch
    assert calls[0]["since_open_ms"] == 4_001
