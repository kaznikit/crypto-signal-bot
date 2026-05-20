"""Fib-вычисления поверх Pine-импульсов (`bot.market.pivots.ImpulseLeg`).

После перехода на pivot-стек ноль legacy-зависимостей — никаких
``AnchoredImpulse`` / ``smartmoneyconcepts`` / walk-back. Если откуда-то
нужен 0.5 retrace — берём ``impulse.fib_half`` или вызываем ``fib_level``.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.market.pivots import ImpulseLeg


@dataclass(slots=True)
class OteZone:
    low: float
    high: float


def build_ote_zone(impulse: ImpulseLeg, fib_low: float, fib_high: float) -> OteZone:
    distance = abs(impulse.end_price - impulse.start_price)
    if impulse.direction == "LONG":
        level_a = impulse.end_price - distance * fib_low
        level_b = impulse.end_price - distance * fib_high
    else:
        level_a = impulse.end_price + distance * fib_low
        level_b = impulse.end_price + distance * fib_high
    return OteZone(low=min(level_a, level_b), high=max(level_a, level_b))


def is_price_in_zone(price_low: float, price_high: float, zone: OteZone) -> bool:
    return not (price_high < zone.low or price_low > zone.high)


def fib_level(impulse: ImpulseLeg, fib: float) -> float:
    """Цена ретрейса на заданном fib (0..1) от Pine-импульса.

    LONG (HL→HH): уровень = end_price - fib * (end_price - start_price).
    SHORT (LH→LL): уровень = end_price + fib * (start_price - end_price).
    """
    distance = abs(impulse.end_price - impulse.start_price)
    if impulse.direction == "LONG":
        return impulse.end_price - distance * fib
    return impulse.end_price + distance * fib
