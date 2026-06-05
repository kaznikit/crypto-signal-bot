import pandas as pd

from bot.history_replay import (
    ClosedTrade,
    OpenTrade,
    _collapse_impulse_fanout_by_start,
    _emit_fresh_pivot_events,
    _dedupe_overlay_events,
    _filter_invalidated_impulses,
    _filter_stale_structure_events,
    _invalidate_armed_replay_setups_by_key,
    _keep_single_retrace_pivot_per_leg,
    _max_drawdown_r,
    _normalize_structure_sequence,
    _resolve_trade_exit,
    ReplaySetup,
    _expanded_limits_by_tf,
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


def test_expanded_limits_respect_custom_cap() -> None:
    limits = _expanded_limits_by_tf(
        needed_tfs=("1M", "5M", "15M", "1H"),
        limit=1000,
        max_bars_per_tf=60_000,
    )

    assert limits["1H"] == 1000
    assert limits["15M"] == 4000
    assert limits["5M"] == 12_000
    assert limits["1M"] == 60_000


def test_invalidate_armed_replay_setups_by_key() -> None:
    setups = [
        ReplaySetup(
            id="a",
            symbol="BTCUSDT",
            setup_type="CONTINUATION",
            direction="LONG",
            htf="1H",
            ltf_expected="5M",
            invalidation_price=90.0,
            state="ARMED",
            close_time=1,
            expires_time=10,
            ote_low=95.0,
            ote_high=95.0,
        ),
        ReplaySetup(
            id="b",
            symbol="BTCUSDT",
            setup_type="CONTINUATION",
            direction="SHORT",
            htf="1H",
            ltf_expected="5M",
            invalidation_price=110.0,
            state="ARMED",
            close_time=2,
            expires_time=10,
            ote_low=105.0,
            ote_high=105.0,
        ),
        ReplaySetup(
            id="c",
            symbol="BTCUSDT",
            setup_type="CONTINUATION",
            direction="LONG",
            htf="1H",
            ltf_expected="5M",
            invalidation_price=91.0,
            state="CONFIRMED",
            close_time=3,
            expires_time=10,
            ote_low=96.0,
            ote_high=96.0,
        ),
    ]
    changed = _invalidate_armed_replay_setups_by_key(
        setups,
        key=("BTCUSDT", "CONTINUATION", "1H", "LONG"),
    )
    assert changed == 1
    assert setups[0].state == "INVALIDATED"
    assert setups[1].state == "ARMED"
    assert setups[2].state == "CONFIRMED"


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


def test_filter_stale_structure_events_drops_early_probe(monkeypatch) -> None:
    df = pd.DataFrame(
        [
            {"open_time": 1000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            {"open_time": 2000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            {"open_time": 3000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        ]
    )
    events = [
        {
            "kind": "STRUCTURE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 1000,
            "swing_open_ms": 900,
            "direction": "SHORT",
            "subkind": "CHOCH",
        },
        {
            "kind": "STRUCTURE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 3000,
            "swing_open_ms": 2000,
            "direction": "SHORT",
            "subkind": "BOS",
        },
        {"kind": "PIVOT", "htf": "1H", "bar_open_ms": 3000, "label": "LH", "pivot_kind": "HIGH"},
    ]

    from bot.market.pivots import StructureBreak

    monkeypatch.setattr(
        "bot.history_replay.extract_structure_breaks_htf",
        lambda *args, **kwargs: [StructureBreak("SHORT", "CHOCH", 1, 0.0, 2)],
    )

    class _Pivots:
        swing_size_by_tf = {"1H": 4}
        bos_use_close = True

    class _Cfg:
        pivots = _Pivots()

    _filter_stale_structure_events(events, {"1H": df}, _Cfg())

    kinds = [e["kind"] for e in events]
    assert kinds.count("STRUCTURE") == 1
    assert events[0]["bar_open_ms"] == 3000
    assert events[0]["direction"] == "SHORT"


def test_emit_fresh_pivot_events_adds_swing_pivot_for_structure_anchor(monkeypatch) -> None:
    df = pd.DataFrame(
        [
            {"open_time": 1000, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1.0},
            {"open_time": 2000, "open": 1.0, "high": 1.1, "low": 0.8, "close": 0.9, "volume": 1.0},
            {"open_time": 3000, "open": 0.9, "high": 1.0, "low": 0.7, "close": 0.8, "volume": 1.0},
        ]
    )
    from bot.market.pivots import Pivot, StructureBreak

    monkeypatch.setattr(
        "bot.history_replay.detect_pivots",
        lambda *_args, **_kwargs: [Pivot(idx=1, kind="LOW", price=0.8, label="HL")],
    )
    monkeypatch.setattr(
        "bot.history_replay.detect_pivots_htf",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "bot.history_replay.extract_structure_breaks_htf",
        lambda *_args, **_kwargs: [
            StructureBreak("SHORT", "CHOCH", swing_idx=1, swing_price=0.8, broken_idx=2)
        ],
    )
    monkeypatch.setattr(
        "bot.history_replay.extract_impulse_legs_confirmed",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "bot.history_replay.compute_impulse_lock_state",
        lambda *_args, **_kwargs: None,
    )
    events: list[dict[str, object]] = []
    _emit_fresh_pivot_events(
        df=df,
        htf="1H",
        symbol="INJUSDT",
        swing_size=1,
        bos_use_close=True,
        events_out=events,
    )
    assert any(
        ev.get("kind") == "PIVOT" and int(ev.get("bar_open_ms") or 0) == 2000
        for ev in events
    )
    assert any(
        ev.get("kind") == "STRUCTURE" and int(ev.get("bar_open_ms") or 0) == 3000
        for ev in events
    )


def test_filter_stale_structure_events_drops_prepare_without_valid_structure(monkeypatch) -> None:
    df = pd.DataFrame(
        [
            {"open_time": 1000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            {"open_time": 2000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            {"open_time": 3000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        ]
    )
    events = [
        {
            "kind": "PREPARE",
            "setup_id": "s-old",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "SHORT",
            "structure_broken_open_ms": 1000,
            "structure_swing_open_ms": 900,
        },
        {
            "kind": "ENTRY",
            "setup_id": "s-old",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "SHORT",
            "bar_open_ms": 1500,
        },
        {
            "kind": "PREPARE",
            "setup_id": "s-new",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "SHORT",
            "structure_broken_open_ms": 3000,
            "structure_swing_open_ms": 2000,
        },
        {
            "kind": "ENTRY",
            "setup_id": "s-new",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "SHORT",
            "bar_open_ms": 3100,
        },
    ]
    from bot.market.pivots import StructureBreak

    monkeypatch.setattr(
        "bot.history_replay.extract_structure_breaks_htf",
        lambda *args, **kwargs: [StructureBreak("SHORT", "CHOCH", 1, 0.0, 2)],
    )

    class _Pivots:
        swing_size_by_tf = {"1H": 4}
        bos_use_close = True

    class _Cfg:
        pivots = _Pivots()

    _filter_stale_structure_events(events, {"1H": df}, _Cfg())

    kinds = [(e["kind"], e.get("setup_id")) for e in events]
    assert ("PREPARE", "s-old") not in kinds
    assert ("ENTRY", "s-old") not in kinds
    assert ("PREPARE", "s-new") in kinds
    assert ("ENTRY", "s-new") in kinds


def test_keep_single_retrace_pivot_keeps_only_expected_kind_between_structure() -> None:
    events = [
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 1,
            "label": "HH",
            "pivot_kind": "HIGH",
            "price": 110.0,
        },
        {
            "kind": "STRUCTURE",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 10,
            "subkind": "BOS",
            "direction": "LONG",
            "level": 108.0,
        },
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 11,
            "label": "LH",
            "pivot_kind": "HIGH",
            "price": 109.0,
        },
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 12,
            "label": "HL",
            "pivot_kind": "LOW",
            "price": 101.0,
        },
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 13,
            "label": "HL",
            "pivot_kind": "LOW",
            "price": 99.0,
        },
        {
            "kind": "STRUCTURE",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 20,
            "subkind": "CHOCH",
            "direction": "SHORT",
            "level": 100.0,
        },
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 21,
            "label": "LH",
            "pivot_kind": "HIGH",
            "price": 106.0,
        },
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 22,
            "label": "HL",
            "pivot_kind": "LOW",
            "price": 97.0,
        },
        {
            "kind": "PIVOT",
            "symbol": "BTCUSDT",
            "htf": "1H",
            "bar_open_ms": 23,
            "label": "LH",
            "pivot_kind": "HIGH",
            "price": 108.0,
        },
    ]

    _keep_single_retrace_pivot_per_leg(events)

    pivots = [e for e in events if e.get("kind") == "PIVOT"]
    assert {int(p["bar_open_ms"]) for p in pivots} == {1, 13, 23}


def test_keep_single_retrace_pivot_preserves_structure_anchor_pivot() -> None:
    events = [
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 1,
            "label": "HH",
            "pivot_kind": "HIGH",
            "price": 5.3,
        },
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 2,
            "label": "HL",
            "pivot_kind": "LOW",
            "price": 5.04,
        },
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 3,
            "label": "LH",
            "pivot_kind": "HIGH",
            "price": 5.2,
        },
        {
            "kind": "STRUCTURE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 10,
            "subkind": "CHOCH",
            "direction": "SHORT",
            "level": 5.04,
            "swing_open_ms": 2,
        },
    ]
    _keep_single_retrace_pivot_per_leg(events)
    pivots = [e for e in events if e.get("kind") == "PIVOT"]
    assert any(int(p["bar_open_ms"]) == 2 for p in pivots)


def test_keep_single_retrace_pivot_before_first_structure_keeps_first_hl() -> None:
    events = [
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 1000,
            "label": "HL",
            "pivot_kind": "LOW",
            "price": 4.8,
        },
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 2000,
            "label": "HH",
            "pivot_kind": "HIGH",
            "price": 5.1,
        },
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 3000,
            "label": "HL",
            "pivot_kind": "LOW",
            "price": 4.9,
        },
        {
            "kind": "STRUCTURE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 4000,
            "subkind": "CHOCH",
            "direction": "SHORT",
            "level": 4.9,
            "swing_open_ms": 3000,
        },
    ]
    _keep_single_retrace_pivot_per_leg(events)
    pivots = [e for e in events if e.get("kind") == "PIVOT"]
    assert any(int(p["bar_open_ms"]) == 1000 for p in pivots)
    # Pivot, на который опирается первое STRUCTURE-событие, должен остаться.
    assert any(int(p["bar_open_ms"]) == 3000 for p in pivots)


def test_keep_single_retrace_anchor_keeps_structure_swing_even_with_duplicate_hl() -> None:
    events = [
        {"kind": "STRUCTURE", "symbol": "INJUSDT", "htf": "1H", "bar_open_ms": 1000, "subkind": "BOS", "direction": "LONG", "level": 10.0, "swing_open_ms": 900},
        {"kind": "PIVOT", "symbol": "INJUSDT", "htf": "1H", "bar_open_ms": 2000, "label": "HL", "pivot_kind": "LOW", "price": 9.0},
        {"kind": "PIVOT", "symbol": "INJUSDT", "htf": "1H", "bar_open_ms": 3000, "label": "HL", "pivot_kind": "LOW", "price": 9.2},
        {"kind": "STRUCTURE", "symbol": "INJUSDT", "htf": "1H", "bar_open_ms": 4000, "subkind": "CHOCH", "direction": "SHORT", "level": 9.2, "swing_open_ms": 3000},
    ]
    _keep_single_retrace_pivot_per_leg(events)
    piv_ms = [int(e["bar_open_ms"]) for e in events if e.get("kind") == "PIVOT"]
    assert 2000 in piv_ms
    # Swing-anchor STRUCTURE должен оставаться, даже если label дублируется.
    assert 3000 in piv_ms


def test_collapse_impulse_fanout_by_start_prefers_prepare_referenced_leg() -> None:
    events = [
        {
            "kind": "IMPULSE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "LONG",
            "start_open_ms": 1000,
            "end_open_ms": 2000,
        },
        {
            "kind": "IMPULSE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "LONG",
            "start_open_ms": 1000,
            "end_open_ms": 3000,
        },
        {
            "kind": "PREPARE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "LONG",
            "impulse_leg_start_open_ms": 1000,
            "impulse_leg_end_open_ms": 2000,
        },
    ]
    _collapse_impulse_fanout_by_start(events)
    impulses = [e for e in events if e.get("kind") == "IMPULSE"]
    assert len(impulses) == 1
    assert int(impulses[0]["end_open_ms"]) == 2000


def test_keep_single_retrace_preserves_prepare_leg_end_pivot_without_impulse_anchor() -> None:
    events = [
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 1000,
            "label": "HL",
            "pivot_kind": "LOW",
            "price": 3.7,
        },
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 2000,
            "label": "HH",
            "pivot_kind": "HIGH",
            "price": 3.9,
        },
        {
            "kind": "PIVOT",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 3000,
            "label": "HH",
            "pivot_kind": "HIGH",
            "price": 4.0,
        },
        {
            "kind": "STRUCTURE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "bar_open_ms": 4000,
            "subkind": "BOS",
            "direction": "LONG",
            "level": 3.9,
            "swing_open_ms": 2000,
        },
        {
            "kind": "PREPARE",
            "symbol": "INJUSDT",
            "htf": "1H",
            "direction": "LONG",
            "bar_open_ms": 4500,
            "impulse_leg_start_open_ms": 1000,
            "impulse_leg_end_open_ms": 3000,
        },
    ]
    _keep_single_retrace_pivot_per_leg(events)
    piv_ms = [int(e["bar_open_ms"]) for e in events if e.get("kind") == "PIVOT"]
    assert 3000 in piv_ms
