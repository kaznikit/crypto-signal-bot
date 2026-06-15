from __future__ import annotations

import pandas as pd


def atr_percent(df: pd.DataFrame, period: int = 14) -> float:
    work = df.copy()
    work["prev_close"] = work["close"].shift(1)
    tr = pd.concat(
        [
            (work["high"] - work["low"]).abs(),
            (work["high"] - work["prev_close"]).abs(),
            (work["low"] - work["prev_close"]).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    close = work["close"].iloc[-1]
    return float((atr / close) * 100)


def close_beyond_level(close_price: float, level: float, direction: str) -> bool:
    if direction == "LONG":
        return close_price > level
    return close_price < level


def recommended_entry_stop(
    *,
    entry: float,
    direction: str,
    reset_level: float | None,
    invalidation_price: float,
) -> tuple[float, str]:
    if reset_level is not None:
        if direction == "LONG" and float(reset_level) < entry:
            return float(reset_level), "confirm_reset_level"
        if direction == "SHORT" and float(reset_level) > entry:
            return float(reset_level), "confirm_reset_level"
    return float(invalidation_price), "htf_invalidation"


def rr_ok(entry: float, sl: float, tp: float, min_rr: float) -> bool:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    if risk == 0:
        return False
    return (reward / risk) >= min_rr


def entry_sl_tp_levels(
    *,
    entry: float,
    direction: str,
    invalidation_price: float,
    tp_r_multiple: float = 2.0,
) -> tuple[float, float]:
    sl = invalidation_price
    distance = abs(entry - sl)
    if direction == "LONG":
        tp = entry + (distance * tp_r_multiple)
    else:
        tp = entry - (distance * tp_r_multiple)
    return sl, tp


def finalize_entry_levels(
    *,
    entry: float,
    direction: str,
    invalidation_price: float,
    compute_sl_tp: bool,
    min_rr: float,
    tp_r_multiple: float = 2.0,
) -> tuple[dict[str, float] | None, str | None]:
    """SL/TP для ENTRY-сигнала.

    Returns ``(payload_fields, reject_reason)``. При ``compute_sl_tp=False``
    поля не считаются и RR не проверяется. ``reject_reason``: ``zero_risk`` |
    ``rr_below_min``.
    """
    if not compute_sl_tp:
        return None, None

    sl, tp = entry_sl_tp_levels(
        entry=entry,
        direction=direction,
        invalidation_price=invalidation_price,
        tp_r_multiple=tp_r_multiple,
    )
    if abs(entry - sl) == 0:
        return None, "zero_risk"
    if not rr_ok(entry, sl, tp, min_rr):
        return None, "rr_below_min"
    return {"sl": sl, "tp": tp, "tp1": tp}, None
