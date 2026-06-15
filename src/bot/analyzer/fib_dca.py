from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from bot.config import FibDcaConfig


@dataclass(frozen=True, slots=True)
class FibDcaLevel:
    fib: float
    weight_pct: float
    price: float


def fib_retrace_price(
    *,
    direction: str,
    impulse_start_price: float,
    impulse_end_price: float,
    fib: float,
) -> float:
    distance = abs(float(impulse_end_price) - float(impulse_start_price))
    if direction == "LONG":
        return float(impulse_end_price) - distance * float(fib)
    return float(impulse_end_price) + distance * float(fib)


def build_fib_dca_plan(
    *,
    direction: str,
    impulse_start_price: float,
    impulse_end_price: float,
    trigger_fib: float,
    config: FibDcaConfig,
) -> list[FibDcaLevel]:
    return [
        FibDcaLevel(
            fib=float(level.fib),
            weight_pct=float(level.weight_pct),
            price=fib_retrace_price(
                direction=direction,
                impulse_start_price=impulse_start_price,
                impulse_end_price=impulse_end_price,
                fib=float(level.fib),
            ),
        )
        for level in config.levels
        if float(level.fib) + 1e-9 >= float(trigger_fib)
    ]


def new_fib_dca_fills(
    *,
    direction: str,
    plan: Iterable[FibDcaLevel],
    filled_fibs: set[float],
    price_low: float,
    price_high: float,
) -> list[FibDcaLevel]:
    if direction == "LONG":
        return [
            level
            for level in plan
            if level.fib not in filled_fibs and float(price_low) <= level.price
        ]
    return [
        level
        for level in plan
        if level.fib not in filled_fibs and float(price_high) >= level.price
    ]


def initial_trigger_fills(
    *,
    plan: Iterable[FibDcaLevel],
    filled_fibs: set[float],
    trigger_price: float,
) -> list[FibDcaLevel]:
    tolerance = max(abs(float(trigger_price)) * 1e-9, 1e-12)
    return [
        level
        for level in plan
        if level.fib not in filled_fibs and abs(level.price - float(trigger_price)) <= tolerance
    ]


def weighted_average_entry(
    plan: Iterable[FibDcaLevel],
    filled_fibs: set[float],
) -> float | None:
    filled = [level for level in plan if level.fib in filled_fibs]
    total_weight = sum(level.weight_pct for level in filled)
    if total_weight <= 0:
        return None
    return sum(level.price * level.weight_pct for level in filled) / total_weight


def filled_weight_pct(plan: Iterable[FibDcaLevel], filled_fibs: set[float]) -> float:
    return sum(level.weight_pct for level in plan if level.fib in filled_fibs)


def planned_risk(
    plan: Iterable[FibDcaLevel],
    *,
    invalidation_price: float,
) -> float:
    return sum(
        (level.weight_pct / 100.0) * abs(level.price - float(invalidation_price))
        for level in plan
    )


def position_pnl(
    plan: Iterable[FibDcaLevel],
    *,
    filled_fibs: set[float],
    direction: str,
    exit_price: float,
) -> float:
    sign = 1.0 if direction == "LONG" else -1.0
    return sum(
        (level.weight_pct / 100.0) * sign * (float(exit_price) - level.price)
        for level in plan
        if level.fib in filled_fibs
    )


def target_reached(
    *,
    direction: str,
    target_price: float,
    price_low: float,
    price_high: float,
) -> bool:
    if direction == "LONG":
        return float(price_high) >= float(target_price)
    return float(price_low) <= float(target_price)


def serialize_plan(plan: Iterable[FibDcaLevel]) -> str:
    return json.dumps([asdict(level) for level in plan], ensure_ascii=True)


def deserialize_plan(raw: str | None) -> list[FibDcaLevel]:
    if not raw:
        return []
    values = json.loads(raw)
    return [
        FibDcaLevel(
            fib=float(value["fib"]),
            weight_pct=float(value["weight_pct"]),
            price=float(value["price"]),
        )
        for value in values
    ]


def serialize_filled_fibs(filled_fibs: set[float]) -> str:
    return json.dumps(sorted(float(fib) for fib in filled_fibs), ensure_ascii=True)


def deserialize_filled_fibs(raw: str | None) -> set[float]:
    if not raw:
        return set()
    return {float(value) for value in json.loads(raw)}


def initialize_fib_dca_setup(
    *,
    setup: Any,
    prepare_payload: dict[str, Any],
    config: FibDcaConfig,
) -> None:
    if str(getattr(setup, "entry_mode", "simple")).lower() != "fib_dca":
        return
    plan = build_fib_dca_plan(
        direction=str(setup.direction),
        impulse_start_price=float(prepare_payload["impulse_start_price"]),
        impulse_end_price=float(prepare_payload["impulse_end_price"]),
        trigger_fib=float(prepare_payload.get("prepare_trigger_fib", 0.5)),
        config=config,
    )
    setup.fib_dca_plan_json = serialize_plan(plan)
    setup.fib_dca_filled_json = serialize_filled_fibs(set())
    setup.fib_dca_average_entry = None
    setup.fib_dca_filled_weight_pct = 0.0
    setup.fib_dca_last_fill_ms = None
    prepare_payload["entry_mode"] = "fib_dca"
    prepare_payload["fib_dca_levels"] = [asdict(level) for level in plan]
    prepare_payload["target_price"] = float(prepare_payload["impulse_end_price"])
