from datetime import timedelta

from bot.analyzer.setup_machine import build_setup, tick_setup
from bot.storage.models import SetupState, SetupType
from bot.util.time import utcnow


def test_tick_confirms_entry() -> None:
    setup = build_setup(
        setup_id="abc",
        symbol="BTCUSDT",
        setup_type=SetupType.REVERSAL,
        direction="LONG",
        htf="4H",
        ltf_expected="1H",
        origin_price=100.0,
        ote_low=95.0,
        ote_high=97.0,
        invalidation_price=90.0,
        ttl_hours=12,
    )
    state, event, phase_new = tick_setup(
        setup=setup, price_low=99.0, price_high=101.0, choch_direction="LONG"
    )
    assert state == SetupState.CONFIRMED.value
    assert event is not None
    assert event.kind == "ENTRY"
    assert phase_new is None


def test_tick_wait_ote_then_choch() -> None:
    setup = build_setup(
        setup_id="abc2",
        symbol="BTCUSDT",
        setup_type=SetupType.CONTINUATION,
        direction="LONG",
        htf="1H",
        ltf_expected="15M",
        origin_price=100.0,
        ote_low=95.0,
        ote_high=97.0,
        invalidation_price=90.0,
        ttl_hours=12,
        phase="WAIT_OTE",
    )
    state, event, phase_new = tick_setup(
        setup=setup, price_low=95.2, price_high=96.0, choch_direction=None
    )
    assert state == SetupState.ARMED.value
    assert event is None
    assert phase_new == "WAIT_CHOCH"
    setup.phase = "WAIT_CHOCH"
    state, event, phase_new = tick_setup(
        setup=setup, price_low=99.0, price_high=101.0, choch_direction="LONG"
    )
    assert state == SetupState.CONFIRMED.value
    assert event is not None
    assert event.kind == "ENTRY"


def test_tick_expires_with_naive_expires_at() -> None:
    setup = build_setup(
        setup_id="naive-exp",
        symbol="BTCUSDT",
        setup_type=SetupType.REVERSAL,
        direction="LONG",
        htf="4H",
        ltf_expected="1H",
        origin_price=100.0,
        ote_low=95.0,
        ote_high=97.0,
        invalidation_price=90.0,
        ttl_hours=12,
    )
    setup.expires_at = (utcnow() - timedelta(hours=1)).replace(tzinfo=None)
    state, event, _ = tick_setup(
        setup=setup, price_low=99.0, price_high=101.0, choch_direction=None
    )
    assert state == SetupState.EXPIRED.value
    assert event is not None
    assert event.kind == "EXPIRED"
