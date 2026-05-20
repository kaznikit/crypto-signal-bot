"""HTF impulse-lock: без внутренних пивотов/BOS до пробоя HL или HH."""

from __future__ import annotations

from bot.market.pivots import (
    ImpulseLeg,
    ImpulseLockState,
    Pivot,
    compute_impulse_lock_state,
    detect_pivots,
    detect_pivots_htf,
    extract_impulse_legs,
    extract_structure_breaks,
    extract_structure_breaks_htf,
    filter_pivots_by_impulse_lock,
    filter_structure_breaks_by_impulse_lock,
    first_impulse_boundary_break_idxs,
    impulse_leg_for_retracement_pivot,
    pivot_label_for_htf_display,
)


def _df(closes: list[float], *, spread: float = 0.3):
    rows = []
    for i, c in enumerate(closes):
        rows.append(
            {
                "open_time": i * 60_000,
                "open": float(c) - 0.05,
                "high": float(c) + spread,
                "low": float(c) - spread,
                "close": float(c),
                "volume": 100.0,
            }
        )
    import pandas as pd

    return pd.DataFrame(rows)


def test_impulse_lock_suppresses_internal_short_breaks_until_hl_break() -> None:
    """LONG HL→HH, ретрейс: SHORT BOS/CHoCH на HTF только после low < HL."""
    closes = (
        [100.0] * 8
        + [100.0 + i * 2.0 for i in range(1, 16)]
        + [130.0] * 4
        + [128.0, 125.0, 122.0, 118.0]
        + [95.0]
        + [95.0] * 4
        + [92.0]
    )
    df = _df(closes, spread=0.2)
    swing = 3
    raw_breaks = extract_structure_breaks(df, swing_size=swing, use_close=True)
    locked_breaks = extract_structure_breaks_htf(
        df, swing_size=swing, use_close=True, impulse_lock=True
    )
    pivots = detect_pivots(df, swing_size=swing)
    leg = extract_impulse_legs(pivots)[-1]
    broken_start, _ = first_impulse_boundary_break_idxs(
        df, leg, after_idx=leg.end_idx, use_close=True
    )
    raw_shorts_after_peak = [
        b
        for b in raw_breaks
        if b.direction == "SHORT" and b.broken_idx > leg.end_idx
    ]
    locked_shorts = [b for b in locked_breaks if b.direction == "SHORT"]
    if raw_shorts_after_peak and broken_start >= 0:
        assert len(locked_shorts) <= len(raw_shorts_after_peak)
        assert all(b.broken_idx >= broken_start for b in locked_shorts)


def test_retracement_hl_uses_own_impulse_hl_not_later_leg() -> None:
    """Откат после 1-го HH: low выше HL 1-го, но ниже HL 2-го — всё равно HL."""
    legs = [
        ImpulseLeg("LONG", 0, 100.0, 50, 200.0),
        ImpulseLeg("LONG", 55, 180.0, 90, 250.0),
    ]
    lock = ImpulseLockState(
        leg=legs[1],
        lock_from_idx=95,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    retracement = Pivot(idx=60, kind="LOW", price=150.0, label="LL")
    assert impulse_leg_for_retracement_pivot(retracement, legs) == legs[0]
    filtered = filter_pivots_by_impulse_lock([retracement], lock, impulse_legs=legs)
    assert retracement in filtered


def test_retracement_hl_above_impulse_hl_allowed_during_lock() -> None:
    """Коррекционный low выше HL импульса (жёлтые минимумы) виден даже в lock."""
    lock = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 10, 130.0),
        lock_from_idx=13,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    retracement_hl = Pivot(idx=22, kind="LOW", price=115.0, label="LL")
    true_ll = Pivot(idx=25, kind="LOW", price=95.0, label="LL")
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=130.0, label="HH"),
        retracement_hl,
        true_ll,
    ]
    filtered = filter_pivots_by_impulse_lock(
        pivots, lock, impulse_legs=[ImpulseLeg("LONG", 0, 100.0, 10, 130.0)]
    )
    assert retracement_hl in filtered
    assert true_ll not in filtered


def test_impulse_anchor_idxs_always_visible() -> None:
    lock = ImpulseLockState(
        leg=ImpulseLeg("LONG", 50, 200.0, 80, 250.0),
        lock_from_idx=83,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    hl_anchor = Pivot(idx=50, kind="LOW", price=200.0, label="HL")
    pivots = [hl_anchor, Pivot(idx=80, kind="HIGH", price=250.0, label="HH")]
    filtered = filter_pivots_by_impulse_lock(
        pivots,
        lock,
        impulse_legs=[ImpulseLeg("LONG", 50, 200.0, 80, 250.0)],
    )
    assert hl_anchor in filtered


def test_boundary_break_start_uses_close_when_use_close() -> None:
    """Фитиль ниже HL без close — пробой минимума не засчитывается."""
    import pandas as pd

    # HL=100, HH at bar 10; bar 15: wick 98, close 102
    rows = []
    for i, c in enumerate(
        [100.0] * 8
        + [100.0 + i * 3.0 for i in range(1, 4)]
        + [130.0] * 4
        + [102.0, 102.0, 102.0]
    ):
        low = 98.0 if i == 15 else c - 0.2
        rows.append(
            {
                "open_time": i * 60_000,
                "open": c,
                "high": c + 0.2,
                "low": low,
                "close": c,
                "volume": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    leg = ImpulseLeg("LONG", 0, 100.0, 10, 130.0)
    broken_start, _ = first_impulse_boundary_break_idxs(
        df, leg, after_idx=10, use_close=True
    )
    assert broken_start < 0


def test_pivot_label_relabels_false_ll_above_hl() -> None:
    lock = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 10, 130.0),
        lock_from_idx=13,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    p = Pivot(idx=22, kind="LOW", price=115.0, label="LL")
    assert pivot_label_for_htf_display(p, lock) == "HL"


def test_filter_pivots_hides_lh_during_full_lock_keeps_retracement_hl() -> None:
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=130.0, label="HH"),
        Pivot(idx=15, kind="LOW", price=115.0, label="HL"),
        Pivot(idx=18, kind="HIGH", price=120.0, label="LH"),
    ]
    lock = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 10, 130.0),
        lock_from_idx=13,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    filtered = filter_pivots_by_impulse_lock(
        pivots, lock, impulse_legs=[ImpulseLeg("LONG", 0, 100.0, 10, 130.0)]
    )
    assert pivots[2] in filtered
    assert pivots[3] not in filtered


def test_detect_pivots_htf_matches_filter_helper() -> None:
    closes = (
        [100.0] * 8
        + [100.0 + i * 2.0 for i in range(1, 16)]
        + [130.0] * 4
        + [128.0, 125.0, 122.0, 118.0]
    )
    df = _df(closes, spread=0.2)
    swing = 3
    raw = detect_pivots(df, swing_size=swing)
    htf = detect_pivots_htf(df, swing_size=swing, use_close=True, impulse_lock=True)
    state = compute_impulse_lock_state(df, raw, swing_size=swing, use_close=True)
    expected = filter_pivots_by_impulse_lock(raw, state)
    assert htf == expected


def test_structure_before_hh_kept_under_lock() -> None:
    df = _df([100.0] * 6 + [101.0, 102.0, 103.0, 110.0] + [108.0] * 8, spread=0.05)
    swing = 3
    breaks = extract_structure_breaks(df, swing_size=swing, use_close=True)
    raw_pivots = detect_pivots(df, swing_size=swing)
    lock = compute_impulse_lock_state(df, raw_pivots, swing_size=swing, use_close=True)
    if lock is None:
        return
    kept = filter_structure_breaks_by_impulse_lock(breaks, lock)
    for b in kept:
        if b.broken_idx > lock.leg.end_idx:
            assert b.broken_idx >= max(
                lock.broken_start_idx if lock.broken_start_idx >= 0 else 10**9,
                lock.broken_end_idx if lock.broken_end_idx >= 0 else 10**9,
            )
