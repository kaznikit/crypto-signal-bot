"""HTF impulse-lock: без внутренних пивотов/BOS до пробоя HL или HH."""

from __future__ import annotations

from bot.market.pivots import (
    ImpulseLeg,
    ImpulseLockState,
    Pivot,
    StructureBreak,
    compute_impulse_lock_state,
    detect_pivots,
    detect_pivots_htf,
    extract_impulse_legs,
    extract_structure_breaks,
    extract_structure_breaks_htf,
    filter_pivots_by_impulse_lock,
    filter_structure_breaks_by_impulse_lock,
    first_correction_pivot_confirm_idx,
    first_impulse_boundary_break_idxs,
    impulse_leg_for_retracement_pivot,
    pivot_label_for_htf_display,
    apply_impulse_invalidation_choch,
    prepare_suppressed_during_impulse_lock,
    reclassify_structure_break_kinds,
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


def test_impulse_invalidation_forces_choch_at_hl() -> None:
    state = ImpulseLockState(
        leg=ImpulseLeg("LONG", 5, 100.0, 50, 200.0),
        lock_from_idx=55,
        broken_start_idx=80,
        broken_end_idx=-1,
    )
    breaks = [
        StructureBreak("LONG", "BOS", 50, 200.0, 40),
        StructureBreak("SHORT", "BOS", 70, 110.0, 80),
    ]
    out = apply_impulse_invalidation_choch(breaks, state)
    inv = [b for b in out if b.broken_idx == 80 and b.direction == "SHORT"]
    assert len(inv) == 1
    assert inv[0].kind == "CHOCH"
    assert inv[0].swing_idx == 5
    assert inv[0].swing_price == 100.0


def test_reversal_short_waits_for_lh_not_old_impulse_fibo() -> None:
    """Reversal: P SHORT не на 0.5 старого HL→HH, а после подтверждения LH."""
    import pandas as pd

    from bot.analyzer.reversal import detect_reversal_prepare

    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []
    pattern += [100.0] * 10
    pattern += [100.0 + i * (50.0 / 12) for i in range(1, 13)]
    pattern += [150.0] * 4
    pattern += [150.0 - i * (30.0 / 8) for i in range(1, 9)]
    pattern += [120.0] * 4
    pattern += [120.0 - i * (35.0 / 8) for i in range(1, 9)]
    pattern += [90.0, 95.0, 100.0, 110.0, 115.0, 118.0]
    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) - 0.05,
                "high": float(c) + 0.3,
                "low": float(c) - 0.3,
                "close": float(c),
                "volume": 100.0,
            }
        )
    df = pd.DataFrame(bars)
    early_p_short = False
    for n in range(25, len(df)):
        df_slice = df.iloc[: n + 1].reset_index(drop=True)
        setup, _ = detect_reversal_prepare(
            symbol="T",
            htf_df=df_slice,
            close_time=int(df_slice.iloc[-1]["open_time"]),
            swing_size=3,
            max_bars_ago_choch=len(df_slice),
            impulse_max_age_bars=len(df_slice),
            ttl_hours=24,
        )
        if setup is not None and setup.direction == "SHORT":
            raw = detect_pivots(df_slice, swing_size=3)
            lh_confirmed = any(
                p.kind == "HIGH" and p.idx + 3 <= n for p in raw
            )
            if not lh_confirmed:
                early_p_short = True
                break
    assert not early_p_short


def test_continuation_short_does_not_fire_before_lh_ll_impulse() -> None:
    """После CHOCH SHORT (слом HL) P SHORT не должен появляться, пока нет LH→LL."""
    import pandas as pd

    from bot.analyzer.continuation import (
        ContinuationPrepareState,
        detect_continuation_prepare,
    )

    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []
    pattern += [100.0] * 10                                           # ранняя база
    pattern += [100.0 + i * (50.0 / 12) for i in range(1, 13)]        # 100→150 (HH импульса)
    pattern += [150.0] * 4
    pattern += [150.0 - i * (30.0 / 8) for i in range(1, 9)]          # 150→120 (откат)
    pattern += [120.0] * 4
    pattern += [120.0 - i * (35.0 / 8) for i in range(1, 9)]          # 120→85 (слом HL=100)
    pattern += [90.0, 95.0, 100.0, 110.0, 115.0, 118.0]               # отскок к 0.5 = (150+85)/2≈117
    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) - 0.05,
                "high": float(c) + 0.3,
                "low": float(c) - 0.3,
                "close": float(c),
                "volume": 100.0,
            }
        )
    df = pd.DataFrame(bars)
    state = ContinuationPrepareState()
    saw_p_short_before_lh_ll = False
    for n in range(20, len(df)):
        df_slice = df.iloc[: n + 1].reset_index(drop=True)
        setup, event = detect_continuation_prepare(
            symbol="T",
            htf="1H",
            htf_df=df_slice,
            close_time=int(df_slice.iloc[-1]["open_time"]),
            swing_size=3,
            fib_level=0.5,
            impulse_max_age_bars=len(df_slice),
            structure_max_bars_ago=len(df_slice),
            prepare_state=state,
        )
        if setup is None or event is None:
            continue
        if setup.direction != "SHORT":
            continue
        legs = extract_impulse_legs(detect_pivots(df_slice, swing_size=3))
        has_short_impulse = any(leg.direction == "SHORT" for leg in legs)
        if not has_short_impulse:
            saw_p_short_before_lh_ll = True
            break
    assert not saw_p_short_before_lh_ll, "P SHORT не должен появляться без LH→LL импульса"


def test_prepare_suppressed_opposite_during_lock() -> None:
    import pandas as pd

    rows = []
    for i in range(30):
        c = 100.0 + (i * 2.0 if i < 15 else -(i - 14) * 1.5)
        rows.append(
            {
                "open_time": i * 60_000,
                "open": c,
                "high": c + 0.5,
                "low": c - 0.5,
                "close": c,
                "volume": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    pivots = detect_pivots(df, swing_size=3)
    state = compute_impulse_lock_state(df, pivots, swing_size=3, use_close=True)
    if state is None:
        return
    assert prepare_suppressed_during_impulse_lock(
        df, pivots, swing_size=3, use_close=True, setup_direction="SHORT"
    )
    assert not prepare_suppressed_during_impulse_lock(
        df, pivots, swing_size=3, use_close=True, setup_direction="LONG"
    )


def test_reclassify_makes_first_visible_short_choch_after_long() -> None:
    breaks = [
        StructureBreak("LONG", "BOS", 10, 110.0, 50),
        StructureBreak("SHORT", "BOS", 30, 95.0, 80),
        StructureBreak("SHORT", "CHOCH", 40, 90.0, 100),
    ]
    out = reclassify_structure_break_kinds(breaks)
    assert out[1].kind == "CHOCH"
    assert out[2].kind == "BOS"


def test_lh_visible_after_hl_break_on_long_impulse() -> None:
    lock = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 10, 130.0),
        lock_from_idx=13,
        broken_start_idx=20,
        broken_end_idx=-1,
    )
    lh = Pivot(idx=25, kind="HIGH", price=120.0, label="LH")
    legs = [ImpulseLeg("LONG", 0, 100.0, 10, 130.0)]
    assert lh in filter_pivots_by_impulse_lock([lh], lock, impulse_legs=legs)


def test_first_correction_pivot_confirm_after_choch_short() -> None:
    pivots = [
        Pivot(idx=5, kind="LOW", price=90.0, label="HL"),
        Pivot(idx=60, kind="HIGH", price=115.0, label="HH"),
    ]
    assert (
        first_correction_pivot_confirm_idx(
            pivots,
            invalidation_idx=50,
            new_trend_direction="SHORT",
            swing_size=3,
        )
        == 63
    )


def test_apply_impulse_invalidation_choch_not_downgraded_by_reclassify() -> None:
    state = ImpulseLockState(
        leg=ImpulseLeg("LONG", 5, 100.0, 50, 200.0),
        lock_from_idx=55,
        broken_start_idx=80,
        broken_end_idx=-1,
    )
    breaks = [
        StructureBreak("LONG", "BOS", 50, 200.0, 40),
        StructureBreak("SHORT", "BOS", 70, 110.0, 75),
        StructureBreak("SHORT", "BOS", 70, 110.0, 80),
    ]
    out = apply_impulse_invalidation_choch(breaks, state)
    at_inv = [b for b in out if b.broken_idx == 80 and b.direction == "SHORT"]
    assert len(at_inv) == 1
    assert at_inv[0].kind == "CHOCH"
    assert at_inv[0].swing_price == 100.0


def test_break_of_hl_is_choch_even_before_lock_confirmed() -> None:
    """Если слом HL пришёл раньше чем end+swing, на самом баре должен быть CHOCH."""
    closes = [
        100.13255711869463,
        99.89575188177145,
        99.81650119455826,
        100.02674778641101,
        100.05128445303761,
        100.12789371907678,
        100.08222958863742,
        100.16207831219002,
        100.17797342901362,
        99.99775193459683,
        99.99981204191077,
        99.86299298702369,
        101.71982888162918,
        103.6324464399412,
        105.23209309918252,
        107.27519359954447,
        108.86545523148268,
        110.77727534960734,
        112.78792510450299,
        114.23586446338606,
        116.0159772361835,
        116.19177828827523,
        114.19177828827523,
        111.19177828827523,
        107.19177828827523,
        102.19177828827523,
        96.19177828827523,
        90.19177828827523,
        88.19177828827523,
        86.19177828827523,
        85.19177828827523,
        83.95503523204573,
        86.08357947783163,
        83.20298756852831,
        86.55507070254528,
        86.61308951278167,
        86.3394553376525,
        84.8935556017211,
        84.32480527773505,
        85.83827860916573,
        85.25026608239963,
        84.87661058973168,
        84.5464526294388,
        84.94655193513053,
        85.85619495776342,
        86.49606605497866,
        86.80777574353692,
        83.84963732695074,
        84.37473957351449,
        84.96440071872387,
        85.44527191544381,
        84.58418825568327,
        83.97344174986617,
        83.53194561826376,
        84.48655694871508,
        85.03367823247262,
    ]
    swing = 4
    df = _df(closes, spread=0.25)
    state = compute_impulse_lock_state(df, detect_pivots(df, swing_size=swing), swing_size=swing)
    assert state is not None
    assert state.broken_start_idx == 47
    assert state.lock_from_idx > state.broken_start_idx

    on_break = df.iloc[: state.broken_start_idx + 1].reset_index(drop=True)
    breaks = extract_structure_breaks_htf(
        on_break, swing_size=swing, use_close=True, impulse_lock=True
    )
    short_breaks = [b for b in breaks if b.direction == "SHORT" and b.broken_idx == 47]
    assert short_breaks
    assert short_breaks[-1].kind == "CHOCH"


def test_pivot_label_relabels_false_ll_above_hl() -> None:
    lock = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 10, 130.0),
        lock_from_idx=13,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    p = Pivot(idx=22, kind="LOW", price=115.0, label="LL")
    assert pivot_label_for_htf_display(p, lock) == "HL"


def test_pivot_label_marks_ll_after_short_bos_when_new_low_breaks_ll() -> None:
    lock = ImpulseLockState(
        leg=ImpulseLeg("SHORT", 884, 5.208, 887, 4.739),
        lock_from_idx=892,
        broken_start_idx=-1,
        broken_end_idx=903,
    )
    p = Pivot(idx=923, kind="LOW", price=4.618, label="HL")
    assert pivot_label_for_htf_display(p, lock) == "LL"


def test_pivot_label_keeps_short_leg_end_as_ll() -> None:
    lock = ImpulseLockState(
        leg=ImpulseLeg("SHORT", 884, 5.208, 887, 4.739),
        lock_from_idx=892,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    p = Pivot(idx=887, kind="LOW", price=4.739, label="HL")
    assert pivot_label_for_htf_display(p, lock) == "LL"


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
