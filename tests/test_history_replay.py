import pandas as pd

from bot.history_replay import (
    ClosedTrade,
    OpenTrade,
    _dedupe_overlay_events,
    _filter_invalidated_impulses,
    _max_drawdown_r,
    _normalize_structure_sequence,
    _resolve_trade_exit,
    _summarize,
)


def test_resolve_trade_exit_long_tp_hit() -> None:
    trade = OpenTrade(
        setup_id="s1",
        symbol="BTCUSDT",
        setup_type="REVERSAL",
        direction="LONG",
        tf="15M",
        entry_time=1,
        entry=100.0,
        sl=99.0,
        tp=102.0,
        risk=1.0,
    )
    hit, exit_price, r_mult, reason = _resolve_trade_exit(trade=trade, high=102.5, low=100.1)
    assert hit is True
    assert exit_price == 102.0
    assert r_mult == 2.0
    assert reason == "tp"


def test_resolve_trade_exit_short_sl_hit() -> None:
    trade = OpenTrade(
        setup_id="s2",
        symbol="ETHUSDT",
        setup_type="CONTINUATION",
        direction="SHORT",
        tf="5M",
        entry_time=1,
        entry=100.0,
        sl=101.0,
        tp=98.0,
        risk=1.0,
    )
    hit, exit_price, r_mult, reason = _resolve_trade_exit(trade=trade, high=101.2, low=99.9)
    assert hit is True
    assert exit_price == 101.0
    assert r_mult == -1.0
    assert reason == "sl"


def test_max_drawdown_r() -> None:
    trades = [
        ClosedTrade("a", "BTCUSDT", "REVERSAL", "LONG", "15M", 1, 2, 100.0, 102.0, 2.0, "tp"),
        ClosedTrade("b", "BTCUSDT", "REVERSAL", "LONG", "15M", 3, 4, 100.0, 99.0, -1.0, "sl"),
        ClosedTrade("c", "BTCUSDT", "REVERSAL", "LONG", "15M", 5, 6, 100.0, 99.0, -1.0, "sl"),
        ClosedTrade("d", "BTCUSDT", "REVERSAL", "LONG", "15M", 7, 8, 100.0, 102.0, 2.0, "tp"),
    ]
    assert _max_drawdown_r(trades) == 2.0


def test_summarize_basic_metrics() -> None:
    trades = [
        ClosedTrade("a", "BTCUSDT", "REVERSAL", "LONG", "15M", 1, 2, 100.0, 102.0, 2.0, "tp"),
        ClosedTrade("b", "BTCUSDT", "REVERSAL", "LONG", "15M", 3, 4, 100.0, 99.0, -1.0, "sl"),
    ]
    summary = _summarize(symbol="BTCUSDT", mode="both", closed_trades=trades)
    assert summary.trades == 2
    assert summary.wins == 1
    assert summary.losses == 1
    assert summary.winrate_pct == 50.0
    assert summary.total_r == 1.0


def test_filter_invalidated_impulses_keeps_prepare_referenced_leg() -> None:
    df = pd.DataFrame(
        [
            {"open_time": 0, "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 1.0},
            {"open_time": 60_000, "open": 104.0, "high": 110.0, "low": 103.0, "close": 108.0, "volume": 1.0},
            {"open_time": 120_000, "open": 108.0, "high": 109.0, "low": 100.0, "close": 101.0, "volume": 1.0},
            {"open_time": 180_000, "open": 101.0, "high": 112.0, "low": 100.0, "close": 111.0, "volume": 1.0},
        ]
    )
    events = [
        {
            "kind": "PREPARE",
            "htf": "1H",
            "direction": "SHORT",
            "impulse_leg_start_open_ms": 60_000,
            "impulse_leg_end_open_ms": 120_000,
        },
        {
            "kind": "IMPULSE",
            "htf": "1H",
            "direction": "SHORT",
            "start_open_ms": 60_000,
            "end_open_ms": 120_000,
            "start_price": 110.0,
        },
        {
            "kind": "IMPULSE",
            "htf": "1H",
            "direction": "SHORT",
            "start_open_ms": 0,
            "end_open_ms": 60_000,
            "start_price": 105.0,
        },
    ]

    _filter_invalidated_impulses(events, {"1H": df})

    impulses = [e for e in events if e.get("kind") == "IMPULSE"]
    assert len(impulses) == 1
    assert int(impulses[0]["start_open_ms"]) == 60_000


def test_normalize_structure_sequence_relabels_same_direction_as_bos() -> None:
    events = [
        {"kind": "STRUCTURE", "htf": "1H", "direction": "SHORT", "subkind": "CHOCH"},
        {"kind": "STRUCTURE", "htf": "1H", "direction": "SHORT", "subkind": "CHOCH"},
        {"kind": "STRUCTURE", "htf": "1H", "direction": "LONG", "subkind": "BOS"},
        {"kind": "STRUCTURE", "htf": "1H", "direction": "LONG", "subkind": "CHOCH"},
        {"kind": "STRUCTURE", "htf": "4H", "direction": "LONG", "subkind": "BOS"},
    ]

    _normalize_structure_sequence(events)

    assert events[0]["subkind"] == "CHOCH"
    assert events[1]["subkind"] == "BOS"
    assert events[2]["subkind"] == "CHOCH"
    assert events[3]["subkind"] == "BOS"
    # Другой TF имеет независимую последовательность.
    assert events[4]["subkind"] == "CHOCH"


def test_dedupe_overlay_events_removes_duplicate_pivot() -> None:
    events = [
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 1,
            "label": "LH",
            "pivot_kind": "HIGH",
        },
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 1,
            "label": "LH",
            "pivot_kind": "HIGH",
        },
        {
            "kind": "STRUCTURE",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 2,
            "subkind": "CHOCH",
            "direction": "SHORT",
            "swing_open_ms": 1,
        },
    ]
    _dedupe_overlay_events(events)
    assert len(events) == 2
    assert events[0]["kind"] == "PIVOT"
    assert events[1]["kind"] == "STRUCTURE"
