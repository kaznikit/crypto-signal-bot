from types import SimpleNamespace

from bot.analyzer.fib_dca import (
    build_fib_dca_plan,
    filled_weight_pct,
    initial_trigger_fills,
    initialize_fib_dca_setup,
    new_fib_dca_fills,
    planned_risk,
    position_pnl,
    weighted_average_entry,
)
from bot.config import FibDcaConfig


def test_build_long_fib_dca_plan() -> None:
    plan = build_fib_dca_plan(
        direction="LONG",
        impulse_start_price=90,
        impulse_end_price=110,
        trigger_fib=0.5,
        config=FibDcaConfig(),
    )

    assert [level.fib for level in plan] == [0.5, 0.618, 0.705, 0.786]
    assert plan[0].price == 100
    assert plan[-1].price == 94.28


def test_build_short_fib_dca_plan() -> None:
    plan = build_fib_dca_plan(
        direction="SHORT",
        impulse_start_price=110,
        impulse_end_price=90,
        trigger_fib=0.5,
        config=FibDcaConfig(),
    )

    assert plan[0].price == 100
    assert plan[-1].price == 105.72


def test_fills_all_crossed_long_levels_once() -> None:
    plan = build_fib_dca_plan(
        direction="LONG",
        impulse_start_price=90,
        impulse_end_price=110,
        trigger_fib=0.5,
        config=FibDcaConfig(),
    )

    fills = new_fib_dca_fills(
        direction="LONG",
        plan=plan,
        filled_fibs={0.5},
        price_low=95,
        price_high=101,
    )

    assert [level.fib for level in fills] == [0.618, 0.705]


def test_initial_trigger_fill_does_not_depend_on_monitor_candle() -> None:
    plan = build_fib_dca_plan(
        direction="LONG",
        impulse_start_price=90,
        impulse_end_price=110,
        trigger_fib=0.5,
        config=FibDcaConfig(),
    )

    fills = initial_trigger_fills(plan=plan, filled_fibs=set(), trigger_price=100)

    assert [level.fib for level in fills] == [0.5]


def test_initialize_fib_dca_setup_freezes_plan_in_setup_and_payload() -> None:
    setup = SimpleNamespace(entry_mode="fib_dca", direction="LONG")
    payload = {
        "impulse_start_price": 90,
        "impulse_end_price": 110,
        "prepare_trigger_fib": 0.5,
    }

    initialize_fib_dca_setup(setup=setup, prepare_payload=payload, config=FibDcaConfig())

    assert setup.fib_dca_plan_json
    assert setup.fib_dca_filled_json == "[]"
    assert payload["entry_mode"] == "fib_dca"
    assert [level["fib"] for level in payload["fib_dca_levels"]] == [
        0.5,
        0.618,
        0.705,
        0.786,
    ]


def test_aggregate_position_risk_and_pnl() -> None:
    plan = build_fib_dca_plan(
        direction="LONG",
        impulse_start_price=90,
        impulse_end_price=110,
        trigger_fib=0.5,
        config=FibDcaConfig(),
    )
    filled = {level.fib for level in plan}
    risk = planned_risk(plan, invalidation_price=90)

    assert filled_weight_pct(plan, filled) == 100
    assert weighted_average_entry(plan, filled) is not None
    assert position_pnl(plan, filled_fibs=filled, direction="LONG", exit_price=90) == -risk
