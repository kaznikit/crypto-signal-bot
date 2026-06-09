import json
from dataclasses import dataclass
from datetime import UTC, datetime

from bot.prepare_stats import (
    build_prepare_stats_candidates,
    evaluate_prepare_stats_candidate,
    format_prepare_stats_messages,
)
from bot.storage.models import Signal


@dataclass(frozen=True)
class Candle:
    open_time: int
    high: float
    low: float


def _prepare(direction: str = "LONG") -> Signal:
    return Signal(
        id="prepare-1",
        setup_id="setup-1",
        kind="PREPARE",
        payload_json=json.dumps(
            {
                "setup_id": "setup-1",
                "symbol": "BTCUSDT",
                "direction": direction,
                "htf": "1H",
                "bar_open_ms": 1_000,
                "prepare_trigger_fib": 0.5,
                "impulse_start_price": 90 if direction == "LONG" else 110,
                "impulse_end_price": 110 if direction == "LONG" else 90,
                "invalidation_price": 90 if direction == "LONG" else 110,
            }
        ),
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_prepare_stats_tracks_deepest_fib_before_success() -> None:
    candidate = build_prepare_stats_candidates(
        [_prepare()],
        processed_signal_ids=set(),
        fib_levels=[0.5, 0.618, 0.705, 0.786],
        evaluation_tf_by_htf={"1H": "5M"},
    )[0]

    result = evaluate_prepare_stats_candidate(
        candidate,
        [
            Candle(open_time=2_000, high=102, low=95.5),
            Candle(open_time=3_000, high=111, low=97),
        ],
    )

    assert result is not None
    assert result.status == "SUCCESS"
    assert result.deepest_fib == 0.705
    assert result.touched_fibs == (0.5, 0.618, 0.705)


def test_prepare_stats_is_conservative_when_target_and_invalidation_share_bar() -> None:
    candidate = build_prepare_stats_candidates(
        [_prepare()],
        processed_signal_ids=set(),
        fib_levels=[0.5, 0.786],
        evaluation_tf_by_htf={"1H": "5M"},
    )[0]

    result = evaluate_prepare_stats_candidate(
        candidate,
        [Candle(open_time=2_000, high=111, low=89)],
    )

    assert result is not None
    assert result.status == "FAIL"
    assert result.deepest_fib == 0.786


def test_prepare_stats_message_contains_fib_summary() -> None:
    candidate = build_prepare_stats_candidates(
        [_prepare()],
        processed_signal_ids=set(),
        fib_levels=[0.5, 0.618],
        evaluation_tf_by_htf={"1H": "5M"},
    )[0]
    result = evaluate_prepare_stats_candidate(
        candidate,
        [Candle(open_time=2_000, high=111, low=99)],
    )
    assert result is not None

    message = format_prepare_stats_messages([result], fib_levels=[0.5, 0.618])[0]

    assert "PREPARE STATS" in message
    assert "0.5: reached 1" in message
    assert "deepest 0.5" in message
