from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bot.entry_identity import entry_point_name, entry_variant
from bot.storage.models import Signal

GREEN_CHECK_SIGN = "✅"
RED_CANCEL_SIGN = "❌"
TELEGRAM_SAFE_MESSAGE_LEN = 3500


@dataclass(frozen=True, slots=True)
class EntryStatsCandidate:
    signal_id: str
    setup_id: str
    symbol: str
    direction: str
    entry_price: float
    target_price: float
    invalidation_price: float
    entry_open_ms: int
    timeframe: str
    entry_variant: str = "simple"
    entry_point: str = "SIMPLE"
    stop_source: str = "htf_invalidation"


@dataclass(frozen=True, slots=True)
class EntryStatsResult:
    signal_id: str
    symbol: str
    direction: str
    status: str
    entry_price: float
    target_price: float
    invalidation_price: float
    extreme_price: float
    entry_open_ms: int
    outcome_open_ms: int
    entry_variant: str = "simple"
    entry_point: str = "SIMPLE"
    stop_source: str = "htf_invalidation"


def _payload(signal: Signal) -> dict[str, Any]:
    return json.loads(signal.payload_json)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def prepare_payloads_by_setup(signals: list[Signal]) -> dict[str, dict[str, Any]]:
    prepares: dict[str, dict[str, Any]] = {}
    for signal in signals:
        if signal.kind != "PREPARE":
            continue
        payload = _payload(signal)
        setup_id = str(payload.get("setup_id") or signal.setup_id)
        prepares[setup_id] = payload
    return prepares


def build_entry_stats_candidates(
    entry_signals: list[Signal],
    prepare_payloads: dict[str, dict[str, Any]],
    processed_signal_ids: set[str],
) -> list[EntryStatsCandidate]:
    candidates_by_setup_variant: dict[tuple[str, str], EntryStatsCandidate] = {}
    for signal in entry_signals:
        if signal.id in processed_signal_ids:
            continue
        payload = _payload(signal)
        setup_id = str(payload.get("setup_id") or signal.setup_id)
        prepare_setup_id = str(payload.get("prepare_setup_id") or setup_id)
        prepare_payload = prepare_payloads.get(prepare_setup_id, {})
        direction = str(payload.get("direction") or prepare_payload.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            continue

        entry_price = _float_or_none(payload.get("entry") or payload.get("origin_price"))
        variant = entry_variant(payload)
        target_price = _float_or_none(
            payload.get("target_price")
            or payload.get("impulse_end_price")
            or prepare_payload.get("impulse_end_price")
        )
        invalidation_price = _float_or_none(payload.get("recommended_stop"))
        if invalidation_price is None:
            invalidation_price = _float_or_none(
                payload.get("impulse_start_price")
                or payload.get("invalidation_price")
                or prepare_payload.get("impulse_start_price")
                or prepare_payload.get("invalidation_price")
            )
        entry_open_ms = _int_or_none(payload.get("bar_open_ms"))
        symbol = str(payload.get("symbol") or prepare_payload.get("symbol") or "")
        if (
            not symbol
            or entry_price is None
            or target_price is None
            or invalidation_price is None
            or entry_open_ms is None
        ):
            continue
        candidate = EntryStatsCandidate(
            signal_id=signal.id,
            setup_id=setup_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            target_price=target_price,
            invalidation_price=invalidation_price,
            entry_open_ms=entry_open_ms,
            timeframe=str(payload.get("entry_ltf") or payload.get("htf") or "5M"),
            entry_variant=variant,
            entry_point=entry_point_name(payload),
            stop_source=str(payload.get("recommended_stop_source") or "htf_invalidation"),
        )
        key = (prepare_setup_id, variant)
        previous = candidates_by_setup_variant.get(key)
        if previous is None or candidate.entry_open_ms >= previous.entry_open_ms:
            candidates_by_setup_variant[key] = candidate
    return list(candidates_by_setup_variant.values())


def _row_value(row: Any, field: str) -> Any:
    if isinstance(row, dict):
        return row[field]
    return getattr(row, field)


def evaluate_entry_stats_candidate(
    candidate: EntryStatsCandidate,
    candles: list[Any],
) -> EntryStatsResult | None:
    if candidate.direction == "LONG":
        extreme = candidate.entry_price
        for candle in candles:
            open_time = int(_row_value(candle, "open_time"))
            if open_time <= candidate.entry_open_ms:
                continue
            low = float(_row_value(candle, "low"))
            high = float(_row_value(candle, "high"))
            extreme = max(extreme, high)
            if low <= candidate.invalidation_price:
                return EntryStatsResult(
                    signal_id=candidate.signal_id,
                    symbol=candidate.symbol,
                    direction=candidate.direction,
                    status="FAIL",
                    entry_price=candidate.entry_price,
                    target_price=candidate.target_price,
                    invalidation_price=candidate.invalidation_price,
                    extreme_price=low,
                    entry_open_ms=candidate.entry_open_ms,
                    outcome_open_ms=open_time,
                    entry_variant=candidate.entry_variant,
                    entry_point=candidate.entry_point,
                    stop_source=candidate.stop_source,
                )
            if high >= candidate.target_price:
                return EntryStatsResult(
                    signal_id=candidate.signal_id,
                    symbol=candidate.symbol,
                    direction=candidate.direction,
                    status="SUCCESS",
                    entry_price=candidate.entry_price,
                    target_price=candidate.target_price,
                    invalidation_price=candidate.invalidation_price,
                    extreme_price=extreme,
                    entry_open_ms=candidate.entry_open_ms,
                    outcome_open_ms=open_time,
                    entry_variant=candidate.entry_variant,
                    entry_point=candidate.entry_point,
                    stop_source=candidate.stop_source,
                )
        return None

    extreme = candidate.entry_price
    for candle in candles:
        open_time = int(_row_value(candle, "open_time"))
        if open_time <= candidate.entry_open_ms:
            continue
        low = float(_row_value(candle, "low"))
        high = float(_row_value(candle, "high"))
        extreme = min(extreme, low)
        if high >= candidate.invalidation_price:
            return EntryStatsResult(
                signal_id=candidate.signal_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                status="FAIL",
                entry_price=candidate.entry_price,
                target_price=candidate.target_price,
                invalidation_price=candidate.invalidation_price,
                extreme_price=high,
                entry_open_ms=candidate.entry_open_ms,
                outcome_open_ms=open_time,
                entry_variant=candidate.entry_variant,
                entry_point=candidate.entry_point,
                stop_source=candidate.stop_source,
            )
        if low <= candidate.target_price:
            return EntryStatsResult(
                signal_id=candidate.signal_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                status="SUCCESS",
                entry_price=candidate.entry_price,
                target_price=candidate.target_price,
                invalidation_price=candidate.invalidation_price,
                extreme_price=extreme,
                entry_open_ms=candidate.entry_open_ms,
                outcome_open_ms=open_time,
                entry_variant=candidate.entry_variant,
                entry_point=candidate.entry_point,
                stop_source=candidate.stop_source,
            )
    return None


def format_entry_stats_message(results: list[EntryStatsResult]) -> str:
    return "\n\n".join(format_entry_stats_messages(results))


def format_entry_stats_messages(
    results: list[EntryStatsResult],
    max_message_len: int = TELEGRAM_SAFE_MESSAGE_LEN,
) -> list[str]:
    if not results:
        return []
    outcomes_by_variant: dict[str, list[str]] = {}
    for result in results:
        outcomes_by_variant.setdefault(result.entry_variant.upper(), []).append(result.status)

    comparison_lines = ["ENTRY STATS"]
    for variant, statuses in sorted(outcomes_by_variant.items()):
        success = statuses.count("SUCCESS")
        fail = statuses.count("FAIL")
        win_rate = (success / len(statuses)) * 100
        comparison_lines.append(
            f"{variant}: success {success} | fail {fail} | winrate {win_rate:.1f}%"
        )

    summary_lines = []
    for result in results:
        sign = GREEN_CHECK_SIGN if result.status == "SUCCESS" else RED_CANCEL_SIGN
        summary_lines.append(
            f"{sign} {result.symbol} {_format_ms(result.entry_open_ms)} {result.direction} "
            f"| {result.entry_point} | stop {result.invalidation_price:g} [{result.stop_source}]"
        )

    chunks: list[str] = []
    current: list[str] = [*comparison_lines, ""]
    for line in summary_lines:
        candidate = "\n".join([*current, line]) if current else line
        if current and len(candidate) > max_message_len:
            chunks.append("\n".join(current).rstrip())
            current = ["ENTRY STATS (cont.)", line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
