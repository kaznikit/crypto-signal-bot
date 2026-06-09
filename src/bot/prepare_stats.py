from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bot.analyzer.fib_dca import fib_retrace_price
from bot.storage.models import Signal

TELEGRAM_SAFE_MESSAGE_LEN = 3500


@dataclass(frozen=True, slots=True)
class PrepareStatsCandidate:
    signal_id: str
    setup_id: str
    symbol: str
    direction: str
    htf: str
    timeframe: str
    prepare_open_ms: int
    target_price: float
    invalidation_price: float
    fib_prices: tuple[tuple[float, float], ...]
    initially_touched_fibs: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class PrepareStatsResult:
    signal_id: str
    symbol: str
    direction: str
    status: str
    prepare_open_ms: int
    outcome_open_ms: int
    touched_fibs: tuple[float, ...]
    deepest_fib: float | None


def _payload(signal: Signal) -> dict[str, Any]:
    return json.loads(signal.payload_json)


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def build_prepare_stats_candidates(
    prepare_signals: list[Signal],
    *,
    processed_signal_ids: set[str],
    fib_levels: list[float],
    evaluation_tf_by_htf: dict[str, str],
) -> list[PrepareStatsCandidate]:
    candidates: list[PrepareStatsCandidate] = []
    for signal in prepare_signals:
        if signal.kind != "PREPARE" or signal.id in processed_signal_ids:
            continue
        payload = _payload(signal)
        direction = str(payload.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            continue
        start = _float_or_none(payload.get("impulse_start_price"))
        end = _float_or_none(payload.get("impulse_end_price"))
        invalidation = _float_or_none(payload.get("invalidation_price"))
        prepare_open_ms = _int_or_none(
            payload.get("bar_open_ms") or payload.get("touch_open_ms")
        )
        symbol = str(payload.get("symbol") or "")
        htf = str(payload.get("htf") or "")
        if (
            start is None
            or end is None
            or invalidation is None
            or prepare_open_ms is None
            or not symbol
            or not htf
        ):
            continue
        trigger_fib = float(payload.get("prepare_trigger_fib") or 0.5)
        fib_prices = tuple(
            (
                float(fib),
                fib_retrace_price(
                    direction=direction,
                    impulse_start_price=start,
                    impulse_end_price=end,
                    fib=float(fib),
                ),
            )
            for fib in fib_levels
        )
        candidates.append(
            PrepareStatsCandidate(
                signal_id=signal.id,
                setup_id=str(payload.get("setup_id") or signal.setup_id),
                symbol=symbol,
                direction=direction,
                htf=htf,
                timeframe=str(evaluation_tf_by_htf.get(htf, htf)),
                prepare_open_ms=prepare_open_ms,
                target_price=end,
                invalidation_price=invalidation,
                fib_prices=fib_prices,
                initially_touched_fibs=tuple(
                    fib for fib, _ in fib_prices if fib <= trigger_fib + 1e-9
                ),
            )
        )
    return candidates


def _row_value(row: Any, field: str) -> Any:
    if isinstance(row, dict):
        return row[field]
    return getattr(row, field)


def evaluate_prepare_stats_candidate(
    candidate: PrepareStatsCandidate,
    candles: list[Any],
) -> PrepareStatsResult | None:
    touched = set(candidate.initially_touched_fibs)
    for candle in candles:
        open_time = int(_row_value(candle, "open_time"))
        if open_time <= candidate.prepare_open_ms:
            continue
        low = float(_row_value(candle, "low"))
        high = float(_row_value(candle, "high"))
        for fib, price in candidate.fib_prices:
            if candidate.direction == "LONG" and low <= price:
                touched.add(fib)
            if candidate.direction == "SHORT" and high >= price:
                touched.add(fib)

        invalidated = (
            low <= candidate.invalidation_price
            if candidate.direction == "LONG"
            else high >= candidate.invalidation_price
        )
        target_reached = (
            high >= candidate.target_price
            if candidate.direction == "LONG"
            else low <= candidate.target_price
        )
        if invalidated or target_reached:
            ordered = tuple(sorted(touched))
            return PrepareStatsResult(
                signal_id=candidate.signal_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                status="FAIL" if invalidated else "SUCCESS",
                prepare_open_ms=candidate.prepare_open_ms,
                outcome_open_ms=open_time,
                touched_fibs=ordered,
                deepest_fib=max(ordered) if ordered else None,
            )
    return None


def format_prepare_stats_messages(
    results: list[PrepareStatsResult],
    *,
    fib_levels: list[float],
    max_message_len: int = TELEGRAM_SAFE_MESSAGE_LEN,
) -> list[str]:
    if not results:
        return []
    successes = sum(result.status == "SUCCESS" for result in results)
    lines = [
        "PREPARE STATS",
        (
            f"Resolved {len(results)} | Success {successes} | Fail {len(results) - successes} "
            f"| Winrate {(successes / len(results)) * 100:.1f}%"
        ),
        "",
        "Fib reached / success after touch:",
    ]
    for fib in fib_levels:
        reached = [result for result in results if fib in result.touched_fibs]
        wins = sum(result.status == "SUCCESS" for result in reached)
        rate = (wins / len(reached) * 100.0) if reached else 0.0
        lines.append(f"{fib:g}: reached {len(reached)}, success {rate:.1f}%")
    lines.append("")
    for result in results:
        sign = "+" if result.status == "SUCCESS" else "-"
        deepest = "none" if result.deepest_fib is None else f"{result.deepest_fib:g}"
        lines.append(
            f"{sign} {result.symbol} {_format_ms(result.prepare_open_ms)} "
            f"{result.direction} deepest {deepest}"
        )

    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        candidate = "\n".join([*current, line]) if current else line
        if current and len(candidate) > max_message_len:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
