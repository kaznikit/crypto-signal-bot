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
