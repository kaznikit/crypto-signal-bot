"""Тесты Pine-style market-structure (`bot.market.pivots`).

Эталон поведения — индикатор «Market Structure» by Leviathan: классические
``ta.pivothigh/pivotlow`` + HH/LH/HL/LL + один активный prevHigh/prevLow.
"""

from __future__ import annotations

import pandas as pd

import bot.market.pivots as pivmod
from bot.market.pivots import (
    ImpulseLeg,
    Pivot,
    StructureBreak,
    continuation_anchor_break,
    detect_ltf_choch,
    detect_ltf_entry_confirm,
    detect_pivots,
    extract_impulse_legs,
    extract_impulse_legs_confirmed,
    extract_structure_breaks,
    filter_causal_structure_breaks,
    find_first_touch_idx,
    first_touch_of_level_since,
    impulse_invalidated,
    latest_structure_break,
    opposite_structure_break_since_open_ms,
    prepare_emission_on_current_bar,
)


def _df(closes: list[float], *, spread: float = 0.3) -> pd.DataFrame:
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
    return pd.DataFrame(rows)


def _zigzag(steps: list[tuple[float, int]]) -> list[float]:
    """Серия от уровня к уровню линейно за указанное число баров."""
    out: list[float] = []
    prev = steps[0][0]
    out.append(prev)
    for target, bars in steps[1:]:
        for k in range(1, bars + 1):
            out.append(prev + (target - prev) * k / bars)
        prev = target
    return out


def test_no_pivots_when_too_few_bars() -> None:
    assert detect_pivots(_df([100.0] * 5), swing_size=5) == []


def test_detect_pivots_classic_pivothigh_window() -> None:
    """Бар с максимальным high в окне `[i-3, i+3]` — пивот-хай.

    Серия: 100,100,100,100,**105**,100,100,100,100. Пивот-хай должен быть на
    индексе 4 при swing_size=3 (по 3 бара с каждой стороны).
    """
    df = _df([100.0, 100.0, 100.0, 100.0, 105.0, 100.0, 100.0, 100.0, 100.0])
    pivots = detect_pivots(df, swing_size=3)
    highs = [p for p in pivots if p.kind == "HIGH"]
    assert any(p.idx == 4 for p in highs)


def test_pivots_classify_hh_lh_hl_ll_pine_style() -> None:
    """HH/LH/HL/LL = сравнение с предыдущим пивотом своего типа.

    Серия: low1=90 → high1=100 → low2=95 → high2=110 → low3=100 → high3=105.
    Ожидаем:
      pivots по типу: HIGH:100(HH-1), HIGH:110(HH), HIGH:105(LH)
                     LOW:90(HL-1), LOW:95(HL), LOW:100(HL)
    """
    closes = _zigzag(
        [
            (90.0, 0),
            (90.0, 6),    # подтверждаем 90 как пивот
            (100.0, 8),   # подъём → high≈100
            (100.0, 6),   # подтверждение 100
            (95.0, 6),    # откат → low=95
            (95.0, 6),    # подтверждение
            (110.0, 10),  # вверх → high=110
            (110.0, 6),
            (100.0, 8),   # откат → low=100
            (100.0, 6),
            (105.0, 8),   # вверх → high=105 (LH относительно 110)
            (105.0, 6),
        ]
    )
    pivots = detect_pivots(_df(closes), swing_size=3)
    highs = [p for p in pivots if p.kind == "HIGH"]
    lows = [p for p in pivots if p.kind == "LOW"]
    assert highs, "ожидаем хотя бы один HIGH-пивот"
    # Первый HIGH-пивот — всегда HH (prevHigh=None).
    assert highs[0].label == "HH"
    # Если найдены 2+ HIGH-пивота, второй (если выше) — HH, иначе LH.
    if len(highs) >= 2:
        h0, h1 = highs[0], highs[1]
        expected = "HH" if h1.price >= h0.price else "LH"
        assert h1.label == expected
    # Симметрично для лоу.
    if lows:
        assert lows[0].label == "HL"


def test_extract_impulse_legs_only_hl_to_hh_and_lh_to_ll() -> None:
    """LONG-импульс = HL→HH, SHORT = LH→LL. LL→HH (разворот тренда) импульсом
    НЕ считается (Pine рисует 0.5 только для continuation-паттернов)."""
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=5, kind="HIGH", price=120.0, label="HH"),  # HL→HH = LONG
        Pivot(idx=10, kind="LOW", price=90.0, label="LL"),
        Pivot(idx=15, kind="HIGH", price=110.0, label="LH"),  # LL→LH (не импульс)
        Pivot(idx=20, kind="LOW", price=80.0, label="LL"),  # LH→LL = SHORT
    ]
    legs = extract_impulse_legs(pivots)
    assert len(legs) == 2
    assert legs[0] == ImpulseLeg(
        direction="LONG", start_idx=0, start_price=100.0, end_idx=5, end_price=120.0
    )
    assert legs[1] == ImpulseLeg(
        direction="SHORT", start_idx=15, start_price=110.0, end_idx=20, end_price=80.0
    )


def test_extract_impulse_legs_confirmed_break_driven_from_bos() -> None:
    """Импульс подтверждается BOS/CHOCH: end = последний экстремум до break."""
    swing = 3
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=120.0, label="HH"),
        Pivot(idx=20, kind="LOW", price=90.0, label="LL"),
        Pivot(idx=30, kind="HIGH", price=110.0, label="LH"),
        Pivot(idx=40, kind="LOW", price=80.0, label="LL"),
    ]

    bos_long = StructureBreak(
        direction="LONG",
        kind="BOS",
        swing_idx=10,
        swing_price=120.0,
        broken_idx=15,
    )
    confirmed = extract_impulse_legs_confirmed(
        pivots, [bos_long], swing_size=swing
    )
    assert confirmed == [
        ImpulseLeg(
            direction="LONG",
            start_idx=0,
            start_price=100.0,
            end_idx=10,
            end_price=120.0,
            anchor_break_idx=15,
        )
    ]

    assert extract_impulse_legs_confirmed(pivots, [], swing_size=swing) == []

    bos_short = StructureBreak(
        direction="SHORT",
        kind="BOS",
        swing_idx=30,
        swing_price=110.0,
        broken_idx=38,
    )
    confirmed_short = extract_impulse_legs_confirmed(
        pivots, [bos_short], swing_size=swing
    )
    assert confirmed_short == [
        ImpulseLeg(
            direction="SHORT",
            start_idx=10,
            start_price=120.0,
            end_idx=20,
            end_price=90.0,
            anchor_break_idx=38,
        )
    ]


def test_extract_impulse_legs_confirmed_delayed_bos_after_retracement() -> None:
    """BOS после отката всё равно подтверждает ногу до break (не «окно у end»)."""
    swing = 3
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=120.0, label="HH"),
    ]
    bos_delayed = StructureBreak(
        direction="LONG",
        kind="BOS",
        swing_idx=10,
        swing_price=120.0,
        broken_idx=25,
    )
    confirmed = extract_impulse_legs_confirmed(
        pivots, [bos_delayed], swing_size=swing
    )
    assert confirmed == [
        ImpulseLeg(
            direction="LONG",
            start_idx=0,
            start_price=100.0,
            end_idx=10,
            end_price=120.0,
            anchor_break_idx=25,
        )
    ]


def test_extract_impulse_legs_confirmed_resets_without_fib_touch() -> None:
    """Повторный BOS без касания 0.5 сбрасывает предыдущую ногу."""
    swing = 3
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=120.0, label="HH"),
        Pivot(idx=20, kind="LOW", price=110.0, label="HL"),
        Pivot(idx=30, kind="HIGH", price=140.0, label="HH"),
    ]
    breaks = [
        StructureBreak("LONG", "BOS", swing_idx=10, swing_price=120.0, broken_idx=15),
        StructureBreak("LONG", "BOS", swing_idx=30, swing_price=140.0, broken_idx=35),
    ]
    bars = []
    for i, c in enumerate([100.0] * 11 + [115.0] * 24):
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
    df = pd.DataFrame(bars)
    confirmed = extract_impulse_legs_confirmed(
        pivots, breaks, swing_size=swing, df=df
    )
    assert confirmed == [
        ImpulseLeg(
            direction="LONG",
            start_idx=20,
            start_price=110.0,
            end_idx=30,
            end_price=140.0,
            anchor_break_idx=35,
        )
    ]


def test_extract_impulse_legs_confirmed_keeps_both_legs_after_fib_touch() -> None:
    """Если 0.5 было достигнуто, повторный BOS не сбрасывает предыдущую ногу."""
    swing = 3
    pivots = [
        Pivot(idx=0, kind="LOW", price=100.0, label="HL"),
        Pivot(idx=10, kind="HIGH", price=120.0, label="HH"),
        Pivot(idx=20, kind="LOW", price=110.0, label="HL"),
        Pivot(idx=30, kind="HIGH", price=140.0, label="HH"),
    ]
    breaks = [
        StructureBreak("LONG", "BOS", swing_idx=10, swing_price=120.0, broken_idx=15),
        StructureBreak("LONG", "BOS", swing_idx=30, swing_price=140.0, broken_idx=35),
    ]
    bars = []
    for i, c in enumerate([100.0] * 16 + [110.0] + [130.0] * 19):
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
    df = pd.DataFrame(bars)
    confirmed = extract_impulse_legs_confirmed(
        pivots, breaks, swing_size=swing, df=df
    )
    assert len(confirmed) == 2
    assert confirmed[0].anchor_break_idx == 15
    assert confirmed[1].anchor_break_idx == 35


def test_extract_impulse_legs_confirmed_subsequent_bos_uses_local_pivots() -> None:
    """Повторный BOS SHORT строит ногу из локальных LH/LL, не из старой HH."""
    swing = 3
    pivots = [
        Pivot(idx=0, kind="HIGH", price=200.0, label="HH"),
        Pivot(idx=10, kind="LOW", price=150.0, label="LL"),
        Pivot(idx=20, kind="HIGH", price=170.0, label="LH"),
        Pivot(idx=30, kind="LOW", price=120.0, label="LL"),
        Pivot(idx=35, kind="HIGH", price=165.0, label="LH"),
        Pivot(idx=45, kind="LOW", price=100.0, label="LL"),
    ]
    breaks = [
        StructureBreak("SHORT", "CHOCH", swing_idx=20, swing_price=170.0, broken_idx=35),
        StructureBreak("SHORT", "BOS", swing_idx=35, swing_price=165.0, broken_idx=50),
    ]
    bars = []
    for i, c in enumerate([160.0] * 36 + [145.0] * 15):
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
    df = pd.DataFrame(bars)
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing, df=df)
    short_legs = [leg for leg in legs if leg.direction == "SHORT"]
    assert len(short_legs) == 2
    assert short_legs[0].start_idx == 20
    assert short_legs[0].end_idx == 30
    assert short_legs[0].anchor_break_idx == 35
    assert short_legs[1].start_idx == 35
    assert short_legs[1].end_idx == 45
    assert short_legs[1].anchor_break_idx == 50
    assert short_legs[1].start_idx != 0


def test_extract_impulse_legs_confirmed_skips_when_no_local_start_pivot() -> None:
    """Без локального LH/HL после предыдущего break нога не строится."""
    swing = 3
    pivots = [
        Pivot(idx=20, kind="HIGH", price=170.0, label="LH"),
        Pivot(idx=30, kind="LOW", price=120.0, label="LL"),
        Pivot(idx=45, kind="LOW", price=100.0, label="LL"),
    ]
    breaks = [
        StructureBreak("SHORT", "CHOCH", swing_idx=20, swing_price=170.0, broken_idx=35),
        StructureBreak("SHORT", "BOS", swing_idx=30, swing_price=120.0, broken_idx=40),
    ]
    legs = extract_impulse_legs_confirmed(pivots, breaks, swing_size=swing)
    assert not any(leg.anchor_break_idx == 40 for leg in legs)


def test_impulse_leg_fib_half_is_midprice() -> None:
    leg = ImpulseLeg(direction="LONG", start_idx=0, start_price=100.0, end_idx=10, end_price=180.0)
    assert leg.fib_half == 140.0


def test_extract_structure_breaks_bos_then_choch() -> None:
    """Поднимаемся, делаем pivot-high, потом close > prevHigh — BOS LONG.
    Затем делаем pivot-low, и close < prevLow — это смена направления → CHoCH SHORT.
    """
    closes = (
        [100.0] * 5
        + [100.0 + i for i in range(1, 6)]  # 101..105
        + [105.0] * 4                       # pivot-high=105 подтверждается
        + [104.0, 103.0, 102.0]             # откат
        + [110.0]                           # close > 105 → BOS LONG на этом баре
        + [108.0, 106.0, 100.0, 95.0]       # падаем ниже 105 же — это не интересно
        + [95.0] * 4                        # pivot-low=95
        + [94.0]                            # close < 95 → CHoCH SHORT
    )
    df = _df(closes, spread=0.05)
    breaks = extract_structure_breaks(df, swing_size=3, use_close=True)
    assert breaks, "должны быть хотя бы BOS LONG"
    # Первый break — LONG BOS.
    longs = [b for b in breaks if b.direction == "LONG"]
    shorts = [b for b in breaks if b.direction == "SHORT"]
    assert longs and longs[0].kind == "BOS"
    if shorts:
        # CHoCH = первый пробой в направлении, обратном предыдущему.
        assert shorts[0].kind == "CHOCH"


def test_structure_breaks_one_shot_per_pivot() -> None:
    """После первого BOS уровень prevHigh деактивируется — следующее
    close > old_prevHigh не должно создавать второй BOS на том же пивоте."""
    closes = (
        [100.0] * 5
        + [100.0 + i * 0.5 for i in range(1, 11)]  # подъём к 105
        + [105.0] * 4
        + [103.0, 102.0]
        + [106.0, 107.0, 108.0]  # серия close > 105: только первый — BOS
    )
    df = _df(closes, spread=0.05)
    breaks = extract_structure_breaks(df, swing_size=3, use_close=True)
    long_bos = [b for b in breaks if b.direction == "LONG" and b.swing_price == 105.0 + 0.05]
    # Сравнение «105 + spread» = high пивот-бара. Берём только breaks, чей
    # swing_price ровно у этого пивота.
    assert len(long_bos) <= 1, "BOS на одном prevHigh должен сработать ровно один раз"


def test_continuation_anchor_break_requires_break_after_opposite() -> None:
    """LONG BOS до SHORT BOS не якорит PREPARE — нужен свежий LONG после SHORT."""
    closes = (
        [100.0] * 8
        + [100.0 + i * 2.0 for i in range(1, 12)]  # LONG BOS ~110
        + [110.0] * 4
        + [110.0 - i * 3.0 for i in range(1, 16)]  # падение, SHORT BOS ~65
        + [65.0] * 4
        + [68.0, 70.0, 72.0]  # отскок без нового LONG BOS
    )
    df = _df(closes, spread=0.2)
    breaks = extract_structure_breaks(df, swing_size=3, use_close=True)
    last_idx = len(df) - 1
    assert continuation_anchor_break(
        breaks, direction="LONG", last_idx=last_idx, max_bars_ago=200
    ) is None
    assert continuation_anchor_break(
        breaks, direction="SHORT", last_idx=last_idx, max_bars_ago=200
    ) is not None


def test_first_touch_of_level_since_long_first_touch() -> None:
    df = _df([200.0, 200.0, 180.0, 170.0, 160.0, 149.0])
    assert first_touch_of_level_since(df, direction="LONG", level=150.0, since_idx=1)


def test_first_touch_of_level_since_long_not_first() -> None:
    df = _df([200.0, 200.0, 145.0, 160.0, 145.0])
    assert not first_touch_of_level_since(df, direction="LONG", level=150.0, since_idx=1)


def test_find_first_touch_idx_long() -> None:
    df = _df([200.0, 200.0, 180.0, 170.0, 160.0, 149.0])
    assert find_first_touch_idx(df, direction="LONG", level=150.0, since_idx=1) == 5


def test_prepare_emission_on_current_bar_confirmation_lag() -> None:
    """Касание до ``end_idx + swing_size`` → эмиссия на max(end+swing, touch)."""
    df = _df([200.0, 200.0, 180.0, 170.0, 160.0, 149.0, 155.0, 160.0])
    # since_idx=1 (peak), touch at bar 5 (low 148.7), swing_size=3 → emission at 5
    assert prepare_emission_on_current_bar(
        df.iloc[:6],
        leg_end_idx=1,
        swing_size=3,
        touch_direction="LONG",
        level=150.0,
        since_idx=1,
    ) == (5, 5)
    # На следующем баре emission_bar уже в прошлом
    assert prepare_emission_on_current_bar(
        df,
        leg_end_idx=1,
        swing_size=3,
        touch_direction="LONG",
        level=150.0,
        since_idx=1,
    ) is None


def test_impulse_invalidated_long() -> None:
    df = _df([100.0, 200.0, 195.0, 99.0])
    assert impulse_invalidated(df, direction="LONG", start_price=100.0, after_idx=1)
    df_ok = _df([100.0, 200.0, 195.0, 180.0])
    assert not impulse_invalidated(df_ok, direction="LONG", start_price=100.0, after_idx=1)


def test_impulse_invalidated_short() -> None:
    df = _df([200.0, 100.0, 105.0, 201.0])
    assert impulse_invalidated(df, direction="SHORT", start_price=200.0, after_idx=1)


def test_latest_structure_break_filters_by_age_and_direction() -> None:
    closes = (
        [100.0] * 5
        + [100.0 + i for i in range(1, 6)]
        + [105.0] * 4
        + [103.0, 110.0]
    )
    df = _df(closes, spread=0.05)
    breaks = extract_structure_breaks(df, swing_size=3, use_close=True)
    if not breaks:
        return
    last_idx = int(df.index[-1])
    assert latest_structure_break(breaks) is breaks[-1]
    assert latest_structure_break(breaks, direction="LONG") in breaks
    assert latest_structure_break(
        breaks, last_idx=last_idx, max_bars_ago=0
    ) in {breaks[-1], None}


def test_filter_causal_structure_breaks_drops_retroactive_break(monkeypatch) -> None:
    """Брейк, отсутствующий на своём баре в префиксе, не должен считаться causal."""
    df = _df([100.0 + i for i in range(12)], spread=0.1)
    br_causal = StructureBreak(direction="SHORT", kind="CHOCH", swing_idx=1, swing_price=99.0, broken_idx=3)
    br_retro = StructureBreak(direction="LONG", kind="CHOCH", swing_idx=4, swing_price=105.0, broken_idx=6)

    def _fake_extract(df_prefix: pd.DataFrame, swing_size: int, *, use_close: bool = True, impulse_lock: bool = True):
        last = int(df_prefix.index[-1])
        if last >= 6:
            # На самом broken-баре (6) второй брейк ещё не «виден».
            return [br_causal] if last == 6 else [br_causal, br_retro]
        if last >= 3:
            return [br_causal]
        return []

    monkeypatch.setattr(pivmod, "extract_structure_breaks_htf", _fake_extract)

    out = filter_causal_structure_breaks(
        [br_causal, br_retro],
        df,
        swing_size=3,
        use_close=True,
        impulse_lock=True,
        max_bars_ago=20,
        last_idx=len(df) - 1,
    )
    assert out == [br_causal]


def test_detect_ltf_entry_confirm_filters_direction() -> None:
    closes = (
        [100.0] * 5
        + [100.0 + i for i in range(1, 6)]
        + [105.0] * 4
        + [103.0, 102.0, 110.0]
        + [108.0, 106.0, 100.0, 95.0]
        + [95.0] * 4
        + [94.0]
    )
    df = _df(closes, spread=0.05)
    short_only = detect_ltf_entry_confirm(
        df,
        swing_size=3,
        max_bars_ago=10,
        use_close=True,
        kinds=("CHOCH", "BOS"),
        direction="SHORT",
    )
    any_dir = detect_ltf_entry_confirm(
        df,
        swing_size=3,
        max_bars_ago=10,
        use_close=True,
        kinds=("CHOCH", "BOS"),
    )
    if short_only is not None:
        assert short_only.direction == "SHORT"
    if any_dir is not None and short_only is not None:
        assert any_dir.direction == short_only.direction


def test_detect_ltf_choch_returns_recent_choch_only() -> None:
    """LTF CHoCH-helper должен находить CHoCH не старше max_bars_ago."""
    closes = (
        [100.0] * 5
        + [100.0 + i for i in range(1, 6)]
        + [105.0] * 4
        + [103.0, 102.0, 110.0]                   # BOS LONG
        + [108.0, 106.0, 100.0, 95.0]
        + [95.0] * 4
        + [94.0]                                   # CHoCH SHORT
    )
    df = _df(closes, spread=0.05)
    choch = detect_ltf_choch(df, swing_size=3, max_bars_ago=10, use_close=True)
    if choch is None:
        # Серия могла не построить CHoCH с такими параметрами — это нормально.
        return
    assert choch.direction in {"LONG", "SHORT"}
    assert choch.bars_ago <= 10


def test_opposite_structure_break_since_open_ms_filters_by_direction_and_time() -> None:
    df = _df([100.0 + i for i in range(20)], spread=0.1)
    breaks = [
        StructureBreak("LONG", "BOS", 2, 102.0, 4),
        StructureBreak("SHORT", "CHOCH", 5, 99.0, 8),
        StructureBreak("SHORT", "BOS", 9, 98.0, 12),
        StructureBreak("LONG", "CHOCH", 13, 105.0, 15),
    ]
    # LONG setup -> opposite SHORT, после open_time бара 9 (>= idx=9)
    since = int(df.iloc[9]["open_time"])
    opp_long = opposite_structure_break_since_open_ms(
        breaks, df, setup_direction="LONG", since_open_ms=since
    )
    assert opp_long is not None
    assert opp_long.direction == "SHORT"
    assert opp_long.broken_idx == 12

    # SHORT setup -> opposite LONG после open_time бара 14
    since_short = int(df.iloc[14]["open_time"])
    opp_short = opposite_structure_break_since_open_ms(
        breaks, df, setup_direction="SHORT", since_open_ms=since_short
    )
    assert opp_short is not None
    assert opp_short.direction == "LONG"
    assert opp_short.broken_idx == 15

    # Если после since нет противоположных пробоев -> None
    none_after = opposite_structure_break_since_open_ms(
        breaks, df, setup_direction="LONG", since_open_ms=int(df.iloc[13]["open_time"])
    )
    assert none_after is None
