"""HTF impulse-lock: без внутренних пивотов/BOS до пробоя HL или HH."""

from __future__ import annotations

from bot.market.pivots import (
    ImpulseLeg,
    ImpulseLockState,
    Pivot,
    StructureBreak,
    prepare_suppressed_after_trend_flip,
    compute_impulse_lock_state,
    detect_pivots,
    detect_pivots_htf,
    extract_impulse_legs,
    extract_impulse_legs_confirmed,
    extract_structure_breaks,
    extract_structure_breaks_htf,
    filter_pivots_by_impulse_lock,
    filter_structure_breaks_by_impulse_lock,
    first_correction_pivot_confirm_idx,
    first_impulse_boundary_break_idxs,
    impulse_leg_for_retracement_pivot,
    pivot_label_for_htf_display,
    apply_impulse_invalidation_choch,
    _build_leg_from_break,
    prepare_suppressed_during_impulse_lock,
    reanchor_choch_to_structural_swing,
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
        raw_pivots = detect_pivots(df_slice, swing_size=3)
        breaks_htf = extract_structure_breaks_htf(
            df_slice, swing_size=3, use_close=True, impulse_lock=True
        )
        legs = extract_impulse_legs_confirmed(
            raw_pivots, breaks_htf, swing_size=3, df=df_slice
        )
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


def test_apply_impulse_invalidation_removes_inner_break_at_same_bar() -> None:
    """На баре inv остаётся только synthetic CHOCH от HL/LH импульса."""
    state = ImpulseLockState(
        leg=ImpulseLeg("LONG", 5, 100.0, 50, 200.0),
        lock_from_idx=90,
        broken_start_idx=80,
        broken_end_idx=-1,
    )
    breaks = [
        StructureBreak("LONG", "BOS", 50, 200.0, 40),
        # Внутренний low на том же баре слома HL: не должен оставаться в выдаче.
        StructureBreak("SHORT", "BOS", 70, 110.0, 80),
    ]
    out = apply_impulse_invalidation_choch(breaks, state)
    at_inv = [b for b in out if b.direction == "SHORT" and b.broken_idx == 80]
    assert len(at_inv) == 1
    assert at_inv[0].kind == "CHOCH"
    assert at_inv[0].swing_idx == 5
    assert at_inv[0].swing_price == 100.0


def test_prepare_suppressed_after_trend_flip_allows_confirm_bar(monkeypatch) -> None:
    """На баре подтверждения первого LH/HL suppression должен сниматься."""
    import pandas as pd

    state = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 8, 130.0),
        lock_from_idx=12,
        broken_start_idx=9,
        broken_end_idx=-1,
    )
    raw_pivots = [Pivot(idx=10, kind="HIGH", price=120.0, label="LH")]
    df = _df([100.0] * 16, spread=0.1)

    monkeypatch.setattr(
        "bot.market.pivots.extract_structure_breaks",
        lambda df, swing_size, use_close=True: [],
    )
    monkeypatch.setattr(
        "bot.market.pivots.compute_impulse_lock_state",
        lambda df, pivots, swing_size, use_close=True, breaks=None: state,
    )

    suppressed = prepare_suppressed_after_trend_flip(
        df=df,
        raw_pivots=raw_pivots,
        swing_size=3,
        use_close=True,
        setup_direction="SHORT",
        last_pos=13,  # confirm = idx(10) + swing(3)
    )
    assert not suppressed


def test_extract_structure_breaks_htf_reclassifies_double_choch(monkeypatch) -> None:
    """После impulse-lock второй подряд CHOCH того же направления становится BOS."""
    raw = [
        StructureBreak("LONG", "BOS", 1, 110.0, 10),
        StructureBreak("SHORT", "CHOCH", 2, 95.0, 20),
        StructureBreak("SHORT", "CHOCH", 3, 90.0, 30),
    ]
    dummy_state = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 5, 130.0),
        lock_from_idx=8,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )

    monkeypatch.setattr(
        "bot.market.pivots.extract_structure_breaks",
        lambda df, swing_size, use_close=True: raw,
    )
    monkeypatch.setattr(
        "bot.market.pivots.detect_pivots",
        lambda df, swing_size: [Pivot(0, "LOW", 100.0, "HL")],
    )
    monkeypatch.setattr(
        "bot.market.pivots.compute_impulse_lock_state",
        lambda df, pivots, swing_size, use_close=True, breaks=None: dummy_state,
    )
    monkeypatch.setattr(
        "bot.market.pivots.filter_structure_breaks_by_impulse_lock",
        lambda breaks, state: breaks,
    )
    monkeypatch.setattr(
        "bot.market.pivots.apply_impulse_invalidation_choch",
        lambda breaks, state: breaks,
    )

    out = extract_structure_breaks_htf(
        _df([100.0] * 40, spread=0.1),
        swing_size=3,
        use_close=True,
        impulse_lock=True,
    )
    assert len(out) == 3
    assert out[1].direction == "SHORT" and out[1].kind == "CHOCH"
    assert out[2].direction == "SHORT" and out[2].kind == "BOS"


def test_extract_structure_breaks_htf_keeps_invalidation_choch_anchor(monkeypatch) -> None:
    """Даже после reclassify на баре inv должен остаться CHOCH от HL/LH."""
    dummy_state = ImpulseLockState(
        leg=ImpulseLeg("LONG", 5, 100.0, 50, 200.0),
        lock_from_idx=55,
        broken_start_idx=80,
        broken_end_idx=-1,
    )
    raw = [StructureBreak("LONG", "BOS", 50, 200.0, 40)]

    monkeypatch.setattr(
        "bot.market.pivots.extract_structure_breaks",
        lambda df, swing_size, use_close=True: raw,
    )
    monkeypatch.setattr(
        "bot.market.pivots.detect_pivots",
        lambda df, swing_size: [Pivot(5, "LOW", 100.0, "HL"), Pivot(50, "HIGH", 200.0, "HH")],
    )
    monkeypatch.setattr(
        "bot.market.pivots.compute_impulse_lock_state",
        lambda df, pivots, swing_size, use_close=True, breaks=None: dummy_state,
    )
    monkeypatch.setattr(
        "bot.market.pivots.filter_structure_breaks_by_impulse_lock",
        lambda breaks, state: breaks,
    )
    # Подмешиваем synthetic CHOCH, который потом "ломает" reclassify.
    monkeypatch.setattr(
        "bot.market.pivots.apply_impulse_invalidation_choch",
        lambda breaks, state: breaks
        + [StructureBreak("SHORT", "CHOCH", 5, 100.0, 80)],
    )
    monkeypatch.setattr(
        "bot.market.pivots.reclassify_structure_break_kinds",
        lambda breaks: [StructureBreak("LONG", "BOS", 50, 200.0, 40), StructureBreak("SHORT", "BOS", 70, 110.0, 80)],
    )

    out = extract_structure_breaks_htf(
        _df([100.0] * 120, spread=0.1),
        swing_size=3,
        use_close=True,
        impulse_lock=True,
    )
    inv = [b for b in out if b.direction == "SHORT" and b.broken_idx == 80]
    assert len(inv) == 1
    assert inv[0].kind == "CHOCH"
    assert inv[0].swing_idx == 5
    assert inv[0].swing_price == 100.0


def test_extract_structure_breaks_htf_collapses_early_flip_probe(monkeypatch) -> None:
    """Ранний CHOCH перед быстрым BOS той же стороны схлопывается в один CHOCH."""
    raw = [
        StructureBreak("LONG", "BOS", 10, 110.0, 20),
        StructureBreak("SHORT", "CHOCH", 30, 99.0, 40),
        StructureBreak("SHORT", "BOS", 41, 95.0, 48),
        StructureBreak("SHORT", "BOS", 50, 90.0, 70),
    ]

    monkeypatch.setattr(
        "bot.market.pivots.extract_structure_breaks",
        lambda df, swing_size, use_close=True: raw,
    )
    monkeypatch.setattr(
        "bot.market.pivots.detect_pivots",
        lambda df, swing_size: [Pivot(0, "LOW", 100.0, "HL"), Pivot(10, "HIGH", 130.0, "HH")],
    )
    monkeypatch.setattr(
        "bot.market.pivots.compute_impulse_lock_state",
        lambda df, pivots, swing_size, use_close=True, breaks=None: None,
    )

    out = extract_structure_breaks_htf(
        _df([100.0] * 120, spread=0.1),
        swing_size=4,
        use_close=True,
        impulse_lock=True,
    )
    shorts = [b for b in out if b.direction == "SHORT"]
    assert len(shorts) == 2
    assert shorts[0].broken_idx == 48
    assert shorts[0].kind == "CHOCH"
    assert shorts[0].swing_idx == 30
    assert shorts[0].swing_price == 99.0
    assert shorts[1].broken_idx == 70
    assert shorts[1].kind == "BOS"


def test_extract_structure_breaks_htf_collapsed_flip_keeps_probe_swing(monkeypatch) -> None:
    """Core structure: collapsed CHOCH сохраняет swing исходного probe-break."""
    raw = [
        StructureBreak("LONG", "BOS", 10, 110.0, 20),
        StructureBreak("SHORT", "CHOCH", 30, 99.0, 40),  # ранний probe
        StructureBreak("SHORT", "BOS", 41, 95.0, 48),    # подтверждение flip
    ]
    pivs = [
        Pivot(25, "LOW", 90.0, "HL"),   # ожидаемый HL-уровень flip
        Pivot(30, "LOW", 99.0, "HL"),   # локальный probe-level (должен быть отброшен)
        Pivot(41, "HIGH", 105.0, "LH"),
    ]
    monkeypatch.setattr(
        "bot.market.pivots.extract_structure_breaks",
        lambda df, swing_size, use_close=True: raw,
    )
    monkeypatch.setattr(
        "bot.market.pivots.detect_pivots",
        lambda df, swing_size: pivs,
    )
    monkeypatch.setattr(
        "bot.market.pivots.compute_impulse_lock_state",
        lambda df, pivots, swing_size, use_close=True, breaks=None: None,
    )

    out = extract_structure_breaks_htf(
        _df([100.0] * 80, spread=0.1),
        swing_size=4,
        use_close=True,
        impulse_lock=True,
    )
    flip = [b for b in out if b.direction == "SHORT"][0]
    assert flip.kind == "CHOCH"
    assert flip.broken_idx == 48
    assert flip.swing_idx == 30
    assert flip.swing_price == 99.0


def test_short_choch_after_long_bos_anchors_on_structural_hh() -> None:
    """SHORT-импульс после LONG BOS стартует от структурного HH (пика LONG-тренда),
    а не от ближайшего корректирующего LH.

    Воспроизводит INJUSDT 1H 19-21.05.26: после LONG BOS (anchor=22) HH 5.434
    зафиксирован как пик LONG-импульса. Затем серия HL/LH ниже структурного
    пика, и наконец CHOCH SHORT по HL. Импульсная нога должна анкорить start
    на HH 5.434 (структурный пик), а не на последнем LH перед LL.
    """
    swing = 4
    pivots = [
        Pivot(idx=14, kind="LOW", price=4.503, label="HL"),
        Pivot(idx=15, kind="HIGH", price=4.718, label="HH"),
        Pivot(idx=28, kind="HIGH", price=5.434, label="HH"),  # структурный пик
        Pivot(idx=41, kind="LOW", price=4.825, label="HL"),
        Pivot(idx=43, kind="HIGH", price=5.090, label="LH"),
        Pivot(idx=52, kind="LOW", price=4.877, label="HL"),
        Pivot(idx=57, kind="HIGH", price=5.097, label="HH"),
        Pivot(idx=59, kind="LOW", price=4.907, label="HL"),
        Pivot(idx=66, kind="HIGH", price=5.265, label="HH"),
        Pivot(idx=74, kind="LOW", price=4.745, label="LL"),  # LL после CHOCH
    ]
    breaks = [
        StructureBreak("LONG", "BOS", swing_idx=15, swing_price=4.718, broken_idx=22),
        StructureBreak("SHORT", "CHOCH", swing_idx=59, swing_price=4.907, broken_idx=73),
    ]
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing)
    shorts = [leg for leg in legs if leg.direction == "SHORT"]
    assert shorts, "ожидаем SHORT-ногу от CHOCH"
    short_leg = shorts[-1]
    assert short_leg.start_idx == 28, (
        f"SHORT start должен быть структурным HH (idx 28), а не ближайшим LH; "
        f"got {short_leg.start_idx}"
    )
    assert short_leg.start_price == 5.434
    assert short_leg.end_idx == 74
    assert short_leg.end_price == 4.745
    assert short_leg.anchor_break_idx == 73


def test_long_choch_after_short_bos_anchors_on_structural_ll() -> None:
    """Зеркальный кейс: LONG-импульс после SHORT BOS стартует от структурного LL.

    Симметрично INJUSDT: после SHORT BOS дно SHORT-импульса (структурный LL)
    задаёт start нового LONG-импульса, а не ближайший корректирующий HL.
    """
    swing = 4
    pivots = [
        Pivot(idx=10, kind="HIGH", price=5.0, label="HH"),
        Pivot(idx=20, kind="LOW", price=4.0, label="LL"),    # структурное дно
        Pivot(idx=30, kind="HIGH", price=4.4, label="LH"),
        Pivot(idx=40, kind="LOW", price=4.2, label="HL"),
        Pivot(idx=50, kind="HIGH", price=4.5, label="HH"),
        Pivot(idx=60, kind="LOW", price=4.3, label="HL"),
        Pivot(idx=70, kind="HIGH", price=4.8, label="HH"),   # HH после CHOCH
    ]
    breaks = [
        StructureBreak("SHORT", "BOS", swing_idx=5, swing_price=5.0, broken_idx=15),
        StructureBreak("LONG", "CHOCH", swing_idx=50, swing_price=4.5, broken_idx=65),
    ]
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing)
    longs = [leg for leg in legs if leg.direction == "LONG"]
    assert longs, "ожидаем LONG-ногу от CHOCH"
    long_leg = longs[-1]
    assert long_leg.start_idx == 20, (
        f"LONG start должен быть структурным LL (idx 20), а не ближайшим HL; "
        f"got {long_leg.start_idx}"
    )
    assert long_leg.start_price == 4.0
    assert long_leg.end_idx == 70
    assert long_leg.end_price == 4.8


def test_confirmed_short_leg_after_choch_uses_recent_segment_low() -> None:
    """После CHOCH SHORT нога заканчивается на свежем LL и стартует от структурного пика.

    End (LL) — это новый LL ПОСЛЕ swing (idx 766 < swing_price=4.936),
    не старый swing-low (idx 758). Start (структурный) — самый высокий
    HIGH-пивот между предыдущим LONG break (idx 703) и end-LL: idx 744 LH 5.325
    (см. также INJUSDT 1H 19-21.05.26: красная диагональ SHORT идёт от пика
    LONG-тренда, а не от ближайшего LH).
    """
    swing = 4
    pivots = [
        Pivot(idx=730, kind="LOW", price=4.906, label="HL"),
        Pivot(idx=742, kind="LOW", price=5.036, label="HL"),
        Pivot(idx=744, kind="HIGH", price=5.325, label="LH"),
        Pivot(idx=747, kind="LOW", price=5.041, label="HL"),
        Pivot(idx=749, kind="HIGH", price=5.272, label="LH"),
        Pivot(idx=754, kind="HIGH", price=5.284, label="HH"),
        Pivot(idx=758, kind="LOW", price=4.936, label="LL"),
        Pivot(idx=763, kind="HIGH", price=5.208, label="LH"),
        Pivot(idx=766, kind="LOW", price=4.739, label="LL"),
    ]
    breaks = [
        StructureBreak("LONG", "BOS", swing_idx=692, swing_price=4.95, broken_idx=703),
        StructureBreak("SHORT", "CHOCH", swing_idx=758, swing_price=4.936, broken_idx=765),
    ]
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing)
    shorts = [leg for leg in legs if leg.direction == "SHORT"]
    assert shorts
    last = shorts[-1]
    assert last.start_idx == 744
    assert last.end_idx == 766
    assert last.anchor_break_idx == 765


def test_break_of_hl_is_choch_even_before_lock_confirmed() -> None:
    """Внутренний пробой HL не пробивает структурный LL — break не эмитится.

    Бывший контракт «ранний flip-пробой обязательно даёт BOS» отражал старое
    Pine-Leviathan поведение, где prev_low в SHORT-тренде переписывался любым
    новым LOW-пивотом (в том числе более ВЫСОКИМ). Это давало BOS на пробое
    промежуточного HL без касания структурного LL.

    Новый контракт: prev_low в SHORT-тренде «залочен» на наименьшем
    непробитом LL, и пробой более высокого внутреннего HL событием не
    становится. Симметрично для LONG-тренда и prev_high (см. сценарий
    INJUSDT 1H 19-21.05.26 в баг-репорте пользователя).
    """
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
    # Пробой HL 84.07 без пробития структурного LL 82.95 не должен порождать
    # SHORT-событие. Если в будущем close уйдёт ниже 82.95 — там и появится BOS.
    short_breaks_at_47 = [b for b in breaks if b.direction == "SHORT" and b.broken_idx == 47]
    assert not short_breaks_at_47
    # А прежний (валидный) SHORT-пробой структурного HL≈99.61 на ходе вниз — на месте.
    assert any(b.direction == "SHORT" and abs(b.swing_price - 99.613) < 0.01 for b in breaks)


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
    breaks = extract_structure_breaks(df, swing_size=swing, use_close=True)
    state = compute_impulse_lock_state(
        df, raw, swing_size=swing, use_close=True, breaks=breaks
    )
    legs = extract_impulse_legs_confirmed(raw, breaks, swing_size=swing)
    expected = filter_pivots_by_impulse_lock(raw, state, impulse_legs=legs)
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


def test_yellow_rectangle_high_labeled_as_lh_under_short_impulse_lock() -> None:
    """Откатный high после SHORT-импульса: LH, не перекрашивается старым LONG-lock."""
    lock = ImpulseLockState(
        leg=ImpulseLeg("SHORT", 35, 180.0, 50, 120.0),
        lock_from_idx=53,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    corrective_high = Pivot(idx=60, kind="HIGH", price=170.0, label="LH")
    legs = [lock.leg]
    assert pivot_label_for_htf_display(corrective_high, lock, impulse_legs=legs) == "LH"


def test_compute_impulse_lock_uses_confirmed_short_not_phantom_long() -> None:
    """Последний геометрический HL→HH без BOS — lock на SHORT, не на фантомный LONG."""
    swing = 3
    pivots = [
        Pivot(idx=10, kind="HIGH", price=200.0, label="HH"),
        Pivot(idx=20, kind="LOW", price=150.0, label="LL"),
        Pivot(idx=30, kind="HIGH", price=180.0, label="LH"),
        Pivot(idx=40, kind="LOW", price=120.0, label="LL"),
        Pivot(idx=50, kind="LOW", price=130.0, label="HL"),
        Pivot(idx=60, kind="HIGH", price=140.0, label="HH"),
    ]
    breaks = [
        StructureBreak(
            direction="SHORT",
            kind="BOS",
            swing_idx=30,
            swing_price=180.0,
            broken_idx=35,
        ),
    ]
    closes = [160.0] * 65
    df = _df(closes, spread=0.2)
    state = compute_impulse_lock_state(
        df, pivots, swing_size=swing, use_close=True, breaks=breaks
    )
    assert state is not None
    assert state.leg.direction == "SHORT"
    assert state.leg.end_idx == 20

    state_geom = compute_impulse_lock_state(
        df, pivots, swing_size=swing, use_close=True, breaks=None
    )
    assert state_geom is not None
    assert state_geom.leg.direction == "LONG"
    assert state_geom.leg.end_idx == 60


def test_two_confirmed_short_impulse_legs_after_fib_touch() -> None:
    """Две SHORT-ноги сохраняются, если между BOS было касание 0.5 первой ноги."""
    swing = 3
    pivots = [
        Pivot(idx=0, kind="HIGH", price=200.0, label="HH"),
        Pivot(idx=10, kind="LOW", price=150.0, label="LL"),
        Pivot(idx=20, kind="HIGH", price=180.0, label="LH"),
        Pivot(idx=30, kind="LOW", price=120.0, label="LL"),
        Pivot(idx=40, kind="HIGH", price=160.0, label="LH"),
        Pivot(idx=50, kind="LOW", price=100.0, label="LL"),
    ]
    breaks = [
        StructureBreak("SHORT", "BOS", swing_idx=20, swing_price=180.0, broken_idx=35),
        StructureBreak("SHORT", "BOS", swing_idx=40, swing_price=160.0, broken_idx=55),
    ]
    bars = []
    for i, c in enumerate([160.0] * 60):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": c - 0.05,
                "high": c + 0.3,
                "low": c - 0.3,
                "close": c,
                "volume": 100.0,
            }
        )
    bars[40]["high"] = 145.0
    df = __import__("pandas").DataFrame(bars)
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing, df=df)
    short_legs = [leg for leg in legs if leg.direction == "SHORT"]
    assert len(short_legs) == 2
    assert short_legs[0].end_idx == 30
    assert short_legs[1].end_idx == 50


def test_confirmed_short_impulse_resets_without_fib_touch() -> None:
    """Повторный BOS SHORT без 0.5 оставляет только актуальную reset-ногу.

    Каждый новый BOS строит локально-тесную ногу LH→LL текущего сегмента.
    Без касания fib_half предыдущая нога сбрасывается, остаётся только
    самая свежая (LH@40 → LL@50, anchor — последний BOS).
    """
    swing = 3
    pivots = [
        Pivot(idx=0, kind="HIGH", price=200.0, label="HH"),
        Pivot(idx=10, kind="LOW", price=150.0, label="LL"),
        Pivot(idx=20, kind="HIGH", price=180.0, label="LH"),
        Pivot(idx=30, kind="LOW", price=120.0, label="LL"),
        Pivot(idx=40, kind="HIGH", price=160.0, label="LH"),
        Pivot(idx=50, kind="LOW", price=100.0, label="LL"),
    ]
    breaks = [
        StructureBreak("SHORT", "CHOCH", swing_idx=10, swing_price=150.0, broken_idx=15),
        StructureBreak("SHORT", "BOS", swing_idx=20, swing_price=180.0, broken_idx=25),
        StructureBreak("SHORT", "BOS", swing_idx=40, swing_price=160.0, broken_idx=45),
    ]
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing)
    short_legs = [leg for leg in legs if leg.direction == "SHORT"]
    assert len(short_legs) == 1
    assert short_legs[0].start_idx == 40
    assert short_legs[0].end_idx == 50
    assert short_legs[0].anchor_break_idx == 45


def test_filter_pivots_keeps_only_deepest_retrace() -> None:
    """В lock-окне остаётся один retrace-пивот — самый глубокий HL."""
    lock = ImpulseLockState(
        leg=ImpulseLeg("LONG", 0, 100.0, 10, 130.0),
        lock_from_idx=13,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    hl_shallow = Pivot(idx=15, kind="LOW", price=115.0, label="HL")
    hl_deep = Pivot(idx=20, kind="LOW", price=105.0, label="HL")
    hl_very_shallow = Pivot(idx=18, kind="LOW", price=118.0, label="HL")
    legs = [lock.leg]
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=130.0, label="HH"),
        hl_shallow,
        hl_deep,
        hl_very_shallow,
    ]
    filtered = filter_pivots_by_impulse_lock(pivots, lock, impulse_legs=legs)
    retrace_in_lock = [p for p in filtered if p.idx > lock.leg.end_idx]
    assert len(retrace_in_lock) == 1
    assert retrace_in_lock[0] == hl_deep


def test_filter_pivots_keeps_anchors_plus_one_retrace() -> None:
    """На импульс: start + end + один deepest retrace = 3 метки."""
    lock = ImpulseLockState(
        leg=ImpulseLeg("SHORT", 0, 200.0, 10, 150.0),
        lock_from_idx=13,
        broken_start_idx=-1,
        broken_end_idx=-1,
    )
    lh_shallow = Pivot(idx=15, kind="HIGH", price=170.0, label="LH")
    lh_deep = Pivot(idx=20, kind="HIGH", price=185.0, label="LH")
    legs = [lock.leg]
    pivots = [
        Pivot(idx=0, kind="HIGH", price=200.0, label="LH"),
        Pivot(idx=10, kind="LOW", price=150.0, label="LL"),
        lh_shallow,
        lh_deep,
    ]
    filtered = filter_pivots_by_impulse_lock(pivots, lock, impulse_legs=legs)
    assert len(filtered) == 3
    assert {p.idx for p in filtered} == {0, 10, 20}


def test_choch_long_leg_end_not_capped_by_next_bos_swing() -> None:
    """End CHOCH-ноги = HH после flip, а не swing_idx следующего BOS."""
    swing = 2
    pivots = [
        Pivot(idx=5, kind="LOW", price=4.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=4.24, label="LH"),
        Pivot(idx=20, kind="LOW", price=4.08, label="HL"),
        Pivot(idx=30, kind="HIGH", price=4.40, label="HH"),
    ]
    breaks = [
        StructureBreak("SHORT", "CHOCH", swing_idx=5, swing_price=4.0, broken_idx=15),
        StructureBreak("LONG", "CHOCH", swing_idx=10, swing_price=4.24, broken_idx=25),
        StructureBreak("LONG", "BOS", swing_idx=30, swing_price=4.40, broken_idx=40),
    ]
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing)
    choch_legs = [leg for leg in legs if leg.direction == "LONG" and leg.anchor_break_idx == 25]
    assert choch_legs
    assert choch_legs[-1].end_idx == 30
    assert choch_legs[-1].end_price == 4.40


def test_reanchor_drops_probe_choch_on_internal_hl() -> None:
    """Пробой internal HL без close ниже структурного HL не даёт CHOCH."""
    import pandas as pd

    pivots = [
        Pivot(idx=5, kind="LOW", price=4.90, label="HL"),
        Pivot(idx=10, kind="LOW", price=5.04, label="HL"),
    ]
    rows = []
    for i, close in enumerate([5.10, 5.08, 5.06, 5.05, 5.04, 5.03, 5.02, 5.01, 5.00, 4.99, 4.98, 4.97, 4.96, 4.95, 4.96, 4.94, 4.93, 4.92, 4.91, 4.88]):
        rows.append(
            {
                "open_time": i * 3_600_000,
                "open": close,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "volume": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    probe_breaks = reclassify_structure_break_kinds(
        [
            StructureBreak("LONG", "BOS", swing_idx=0, swing_price=5.0, broken_idx=3),
            StructureBreak("SHORT", "CHOCH", swing_idx=10, swing_price=5.04, broken_idx=15),
        ]
    )
    reanchored_probe = reanchor_choch_to_structural_swing(
        probe_breaks, pivots, df, use_close=True
    )
    assert not [b for b in reanchored_probe if b.broken_idx == 15]

    final_breaks = reclassify_structure_break_kinds(
        [
            StructureBreak("LONG", "BOS", swing_idx=0, swing_price=5.0, broken_idx=3),
            StructureBreak("SHORT", "CHOCH", swing_idx=10, swing_price=5.04, broken_idx=19),
        ]
    )
    reanchored_final = reanchor_choch_to_structural_swing(
        final_breaks, pivots, df, use_close=True
    )
    short_final = [b for b in reanchored_final if b.direction == "SHORT" and b.broken_idx == 19]
    assert short_final
    assert short_final[0].kind == "CHOCH"
    assert short_final[0].swing_price == 5.04


def test_choch_long_start_includes_pivot_at_opposite_break_bar() -> None:
    """Структурный start CHOCH LONG включает LOW на баре последнего SHORT break."""
    pivots = [
        Pivot(idx=20, kind="LOW", price=4.10, label="LL"),
        Pivot(idx=26, kind="LOW", price=4.087, label="LL"),
        Pivot(idx=30, kind="HIGH", price=4.24, label="LH"),
        Pivot(idx=39, kind="HIGH", price=4.396, label="HH"),
    ]
    br = StructureBreak("LONG", "CHOCH", swing_idx=30, swing_price=4.24, broken_idx=36)
    leg = _build_leg_from_break(
        pivots,
        br,
        min_idx=10,
        previous_opposite_broken_idx=26,
        next_same_dir_break_idx=None,
    )
    assert leg is not None
    assert leg.start_idx == 26
    assert leg.start_price == 4.087
    assert leg.end_idx == 39
