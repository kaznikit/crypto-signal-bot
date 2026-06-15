import asyncio
from collections import Counter
from types import SimpleNamespace

import pandas as pd

from bot.__main__ import SignalBotApp
from bot.analyzer.fib_dca import (
    deserialize_filled_fibs,
    deserialize_plan,
    initialize_fib_dca_setup,
    serialize_filled_fibs,
)
from bot.analyzer.setup_machine import build_setup
from bot.config import EntryConfig
from bot.storage.models import SetupType


class _Repo:
    def __init__(self) -> None:
        self.saved = []
        self.states = []
        self.active = []

    def load_active_setups(self):
        return self.active

    def save_signal(self, signal) -> None:
        self.saved.append(signal)

    def upsert_setup(self, _setup) -> None:
        return None

    def mark_setup_state(self, setup_id: str, state: str, _at) -> None:
        self.states.append((setup_id, state))


class _Notifier:
    def __init__(self) -> None:
        self.payloads = []

    async def send_event(self, **kwargs):
        self.payloads.append(kwargs["payload"])
        return SimpleNamespace(id=f"signal-{len(self.payloads)}")


def _app() -> SignalBotApp:
    app = object.__new__(SignalBotApp)
    app._cfg = SimpleNamespace(
        entry=EntryConfig(mode="fib_dca"),
        paper_mode=SimpleNamespace(enabled=True, liberal=SimpleNamespace(enabled=False)),
        pivots=SimpleNamespace(swing_size_by_tf={}, bos_use_close=True),
    )
    app._repo = _Repo()
    app._notifier = _Notifier()
    return app


def _setup():
    setup = build_setup(
        setup_id="setup-1",
        symbol="BTCUSDT",
        setup_type=SetupType.CONTINUATION,
        direction="LONG",
        htf="1H",
        ltf_expected="5M",
        origin_price=100,
        ote_low=100,
        ote_high=100,
        invalidation_price=90,
        ttl_hours=24,
        prepare_since_ms=1_000,
        entry_mode="fib_dca",
        entry_target_price=110,
    )
    initialize_fib_dca_setup(
        setup=setup,
        prepare_payload={
            "impulse_start_price": 90,
            "impulse_end_price": 110,
            "prepare_trigger_fib": 0.5,
        },
        config=EntryConfig().fib_dca,
    )
    return setup


def _df(*, low: float, high: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": 1_000,
                "open": 100,
                "high": high,
                "low": low,
                "close": 100,
                "volume": 1,
            }
        ]
    )


def test_materialize_comparison_modes_creates_independent_setups() -> None:
    app = object.__new__(SignalBotApp)
    app._cfg = SimpleNamespace(
        entry=EntryConfig(comparison_modes=["simple", "sweep_reclaim", "advanced"])
    )
    setup = build_setup(
        setup_id="prepare-1",
        symbol="BTCUSDT",
        setup_type=SetupType.CONTINUATION,
        direction="LONG",
        htf="1H",
        ltf_expected="5M",
        origin_price=100,
        ote_low=100,
        ote_high=100,
        invalidation_price=90,
        ttl_hours=24,
        prepare_since_ms=1_000,
        entry_target_price=110,
    )
    payload = {
        "setup_id": "prepare-1",
        "impulse_start_price": 90,
        "impulse_end_price": 110,
        "prepare_trigger_fib": 0.5,
    }

    variants = app._materialize_entry_mode_setups(setup=setup, prepare_payload=payload)

    assert [variant.entry_mode for variant in variants] == [
        "simple",
        "sweep_reclaim",
        "advanced",
    ]
    assert {variant.comparison_group_id for variant in variants} == {"prepare-1"}
    assert len({variant.id for variant in variants}) == 3
    assert payload["entry_modes"] == ["simple", "sweep_reclaim", "advanced"]


def test_live_fib_dca_sends_initial_trigger_fill_without_ltf_confirmation() -> None:
    app = _app()
    setup = _setup()

    asyncio.run(
        app._advance_fib_dca_setup(
            setup=setup,
            symbol="BTCUSDT",
            series={"5M": _df(low=101, high=102)},
            closed_tfs=["5M"],
            funnel=Counter(),
        )
    )

    assert app._notifier.payloads[0]["fib"] == 0.5
    assert deserialize_filled_fibs(setup.fib_dca_filled_json) == {0.5}


def test_live_fib_dca_stop_first_prevents_new_fills() -> None:
    app = _app()
    setup = _setup()

    asyncio.run(
        app._advance_fib_dca_setup(
            setup=setup,
            symbol="BTCUSDT",
            series={"5M": _df(low=89, high=101)},
            closed_tfs=["5M"],
            funnel=Counter(),
        )
    )

    assert len(app._notifier.payloads) == 1
    assert app._notifier.payloads[0]["mark_price"] == 90
    assert app._repo.states == [("setup-1", "INVALIDATED")]


def test_live_open_trade_waits_then_emits_invalidated_on_stop() -> None:
    app = _app()
    setup = _setup()
    setup.active_trade_stop_price = 95
    setup.active_trade_target_price = 110
    setup.active_trade_tf = "5M"

    asyncio.run(
        app._advance_open_trade(
            setup=setup,
            symbol="BTCUSDT",
            series={"5M": _df(low=94, high=101)},
            closed_tfs=["5M"],
            funnel=Counter(),
        )
    )

    assert app._notifier.payloads[0]["invalidation_price"] == 90
    assert app._notifier.payloads[0]["mark_price"] == 95
    assert app._notifier.payloads[0]["after_entry"] is True
    assert app._repo.states == [("setup-1", "INVALIDATED")]


def test_live_filled_fib_position_blocks_other_setup_and_ignores_structure_transition(
    monkeypatch,
) -> None:
    app = _app()
    current = _setup()
    current.fib_dca_filled_json = serialize_filled_fibs(
        {level.fib for level in deserialize_plan(current.fib_dca_plan_json)}
    )
    other = _setup()
    other.id = "setup-2"
    app._repo.active = [current, other]

    def fail_if_called(**_kwargs):
        raise AssertionError("open Fib DCA position must not be closed by a structure transition")

    monkeypatch.setattr("bot.__main__.decide_setup_structure_transition", fail_if_called)
    funnel = Counter()

    asyncio.run(
        app._advance_active_setups(
            symbol="BTCUSDT",
            series={"5M": _df(low=101, high=102), "1H": _df(low=101, high=102)},
            closed_tfs=["5M", "1H"],
            funnel=funnel,
        )
    )

    assert funnel["entry_skipped_open_position"] == 1
    assert app._repo.states == []
