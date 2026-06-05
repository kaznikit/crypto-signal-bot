import json
from dataclasses import dataclass
from datetime import UTC, datetime

from bot.entry_stats import (
    EntryStatsResult,
    build_entry_stats_candidates,
    evaluate_entry_stats_candidate,
    format_entry_stats_message,
    format_entry_stats_messages,
    prepare_payloads_by_setup,
)
from bot.storage.models import Signal


@dataclass(frozen=True)
class Candle:
    open_time: int
    high: float
    low: float


def _signal(signal_id: str, setup_id: str, kind: str, payload: dict) -> Signal:
    return Signal(
        id=signal_id,
        setup_id=setup_id,
        kind=kind,
        payload_json=json.dumps(payload),
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_entry_stats_candidate_uses_prepare_impulse_target() -> None:
    prepare = _signal(
        "prepare-1",
        "setup-1",
        "PREPARE",
        {
            "setup_id": "setup-1",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "impulse_start_price": 95,
            "impulse_end_price": 110,
            "invalidation_price": 95,
        },
    )
    entry = _signal(
        "entry-1",
        "setup-1",
        "ENTRY",
        {
            "setup_id": "setup-1",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry": 100,
            "bar_open_ms": 1_000,
            "entry_ltf": "5M",
        },
    )

    candidates = build_entry_stats_candidates(
        [entry],
        prepare_payloads_by_setup([prepare, entry]),
        set(),
    )

    assert len(candidates) == 1
    assert candidates[0].target_price == 110
    assert candidates[0].invalidation_price == 95


def test_entry_stats_candidates_keep_latest_entry_per_setup() -> None:
    entries = [
        _signal(
            "entry-1",
            "setup-1",
            "ENTRY",
            {
                "setup_id": "setup-1",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "entry": 100,
                "bar_open_ms": 1_000,
                "impulse_end_price": 110,
                "invalidation_price": 95,
            },
        ),
        _signal(
            "entry-2",
            "setup-1",
            "ENTRY",
            {
                "setup_id": "setup-1",
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "entry": 98,
                "bar_open_ms": 2_000,
                "impulse_end_price": 110,
                "invalidation_price": 95,
            },
        ),
    ]

    candidates = build_entry_stats_candidates(entries, {}, set())

    assert len(candidates) == 1
    assert candidates[0].signal_id == "entry-2"
    assert candidates[0].entry_price == 98


def test_entry_stats_long_success_when_high_updates_impulse_max() -> None:
    candidate = build_entry_stats_candidates(
        [
            _signal(
                "entry-1",
                "setup-1",
                "ENTRY",
                {
                    "setup_id": "setup-1",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "entry": 100,
                    "bar_open_ms": 1_000,
                    "impulse_end_price": 110,
                    "invalidation_price": 95,
                },
            )
        ],
        {},
        set(),
    )[0]

    result = evaluate_entry_stats_candidate(
        candidate,
        [
            Candle(open_time=1_000, high=120, low=90),
            Candle(open_time=2_000, high=111, low=99),
        ],
    )

    assert result is not None
    assert result.status == "SUCCESS"
    assert result.extreme_price == 111


def test_entry_stats_short_fail_when_price_breaks_impulse_start() -> None:
    candidate = build_entry_stats_candidates(
        [
            _signal(
                "entry-2",
                "setup-2",
                "ENTRY",
                {
                    "setup_id": "setup-2",
                    "symbol": "ETHUSDT",
                    "direction": "SHORT",
                    "entry": 100,
                    "bar_open_ms": 1_000,
                    "impulse_end_price": 90,
                    "invalidation_price": 105,
                },
            )
        ],
        {},
        set(),
    )[0]

    result = evaluate_entry_stats_candidate(
        candidate,
        [Candle(open_time=2_000, high=106, low=99)],
    )

    assert result is not None
    assert result.status == "FAIL"
    assert result.extreme_price == 106


def test_entry_stats_message_contains_short_summary_only() -> None:
    candidate = build_entry_stats_candidates(
        [
            _signal(
                "entry-1",
                "setup-1",
                "ENTRY",
                {
                    "setup_id": "setup-1",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "entry": 100,
                    "bar_open_ms": 1_000,
                    "impulse_end_price": 110,
                    "invalidation_price": 95,
                },
            )
        ],
        {},
        set(),
    )[0]
    result = evaluate_entry_stats_candidate(
        candidate,
        [Candle(open_time=2_000, high=111, low=99)],
    )
    assert result is not None

    message = format_entry_stats_message([result])

    assert message == "✅ BTCUSDT 1970-01-01 00:00 LONG"
    assert "Trades:" not in message
    assert "Symbol" not in message


def test_entry_stats_messages_are_split_under_limit() -> None:
    results = [
        EntryStatsResult(
            signal_id=f"entry-{idx}",
            symbol=f"LONGSYMBOL{idx:03d}USDT",
            direction="LONG",
            status="SUCCESS",
            entry_price=100,
            target_price=110,
            invalidation_price=95,
            extreme_price=111,
            entry_open_ms=1_000,
            outcome_open_ms=2_000,
        )
        for idx in range(80)
    ]

    messages = format_entry_stats_messages(results, max_message_len=800)

    assert len(messages) > 1
    assert all(len(message) <= 800 for message in messages)
