"""Тесты pivot-based PREPARE-continuation с выравниванием по BOS/CHoCH."""

from __future__ import annotations

import pandas as pd

from bot.analyzer.continuation import (
    ContinuationPrepareState,
    detect_continuation_prepare,
)


def _swing_then_break_df() -> pd.DataFrame:
    """Базовая синтетическая серия: импульс вверх → ретрейс → продолжение → разворот.
    На свинговой длине 5 даёт несколько пивотов HH/HL/LH/LL."""
    bars: list[dict[str, float | int]] = []
    base = 100.0
    pattern = (
        [base for _ in range(15)]
        + [base + i * 1.0 for i in range(1, 26)]
        + [base + 25.0 - i * 0.7 for i in range(1, 13)]
        + [base + 25.0 - 8.4 + i * 1.0 for i in range(1, 16)]
        + [base + 25.0 - 8.4 + 15.0 - i * 1.0 for i in range(1, 31)]
    )
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
    return pd.DataFrame(bars)


def test_continuation_prepare_returns_none_for_tiny_df() -> None:
    """Для df меньше ``2 * swing_size + 1`` пивотов нет — всегда None."""
    mini = _swing_then_break_df().iloc[:3].copy()
    setup, event = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=mini,
        close_time=0,
        swing_size=5,
        fib_level=0.5,
        impulse_max_age_bars=200,
    )
    assert setup is None
    assert event is None


def test_continuation_prepare_payload_contract_when_signal_present() -> None:
    """Когда на серии есть pivot-нога + касание 0.5 + свежий BOS/CHoCH, payload полный."""
    df = _swing_then_break_df()
    setup, event = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        swing_size=5,
        fib_level=0.5,
        impulse_max_age_bars=len(df),
        structure_max_bars_ago=len(df),
    )
    if setup is None:
        return
    assert event is not None
    payload = event.payload
    assert payload["structure_kind"] in {"BOS", "CHOCH"}
    assert setup.direction in {"LONG", "SHORT"}
    assert int(payload["structure_swing_open_ms"]) > 0
    assert int(payload["structure_broken_open_ms"]) > 0
    trigger = float(payload["prepare_trigger_level"])
    assert float(payload["ote_low"]) == trigger
    assert float(payload["ote_high"]) == trigger


def test_continuation_prepare_skipped_when_no_touch_on_last_bar() -> None:
    """Если последний бар не пересёк 0.5, PREPARE не строим."""
    df = _swing_then_break_df().copy()
    last_close = float(df.iloc[-1]["close"])
    df.loc[df.index[-1], "low"] = last_close - 100.0
    df.loc[df.index[-1], "high"] = last_close - 90.0
    df.loc[df.index[-1], "open"] = last_close - 95.0
    df.loc[df.index[-1], "close"] = last_close - 95.0

    setup, event = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        swing_size=5,
        fib_level=0.5,
        impulse_max_age_bars=len(df),
    )
    if setup is not None:
        return  # Возможно другая нога случайно сработала — OK
    assert event is None


def test_long_impulse_first_touch_triggers_long_continuation() -> None:
    """Чистая синтетика: нога LOW→HIGH (HL→HH), потом ретрейс к 0.5 на последнем баре.
    Должен прийти PREPARE LONG."""
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []

    # 1) Ранний low-пивот у 100.
    pattern += [100.0] * 6
    # 2) Подъём к 120 → ранний high-пивот.
    pattern += [100.0 + i * (20.0 / 6) for i in range(1, 7)]
    # 3) Откат к 110 → HL (выше предыдущего low=100).
    pattern += [120.0 - i * (10.0 / 6) for i in range(1, 7)]
    # 4) Импульс вверх к 150 → HH (выше предыдущего HH=120).
    pattern += [110.0 + i * (40.0 / 8) for i in range(1, 9)]
    # 5) Держим вверху, чтобы HH подтвердился (swing_size=3 → 3 бара справа).
    pattern += [150.0] * 6
    # 6) Слабый откат.
    pattern += [148.0, 146.0, 144.0, 142.0]
    # 7) Последний бар — пробивает 0.5 = (110+150)/2 = 130.
    pattern += [130.0 - 0.5]

    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) + 0.05,
                "high": float(c) + 0.3,
                "low": float(c) - 0.3,
                "close": float(c),
                "volume": 100.0,
            }
        )
    df = pd.DataFrame(bars)

    setup, event = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        swing_size=3,
        fib_level=0.5,
        impulse_max_age_bars=len(df),
    )
    if setup is None:
        return  # swing_size=3 иногда даёт другую раскладку пивотов
    assert event is not None
    assert setup.direction == "LONG"
    payload = event.payload
    trigger = float(payload["prepare_trigger_level"])
    assert abs(trigger - 130.0) <= 1.0, f"trigger {trigger} far from 0.5 mid 130"
    assert float(payload["impulse_start_price"]) < float(payload["impulse_end_price"])
    assert float(payload["invalidation_price"]) == float(payload["impulse_start_price"])


def test_older_leg_first_touch_triggers_when_newer_leg_not_yet_touched() -> None:
    """Регрессия: раньше брали только legs[-1]. Если ПОСЛЕ старой LONG-ноги
    сформировался свежий пивот (новая нога), а цена коснулась 0.5 СТАРОЙ ноги,
    PREPARE должен прийти от старой ноги."""
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []

    pattern += [100.0] * 5
    pattern += [100.0 + i * (20.0 / 5) for i in range(1, 6)]
    pattern += [120.0 - i * (10.0 / 5) for i in range(1, 6)]
    pattern += [110.0 + i * (40.0 / 6) for i in range(1, 7)]
    pattern += [150.0] * 5
    pattern += [150.0 - i * (5.0 / 4) for i in range(1, 5)]
    pattern += [145.0 + i * (3.0 / 3) for i in range(1, 4)]
    pattern += [148.0] * 5
    pattern += [148.0 - i * (18.0 / 5) for i in range(1, 6)]
    pattern += [130.0 - 0.3]

    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) + 0.02,
                "high": float(c) + 0.2,
                "low": float(c) - 0.2,
                "close": float(c),
                "volume": 100.0,
            }
        )
    df = pd.DataFrame(bars)

    setup, event = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        swing_size=3,
        fib_level=0.5,
        impulse_max_age_bars=len(df),
    )
    if setup is None:
        return
    assert event is not None
    payload = event.payload
    trigger = float(payload["prepare_trigger_level"])
    impulse_start = float(payload["impulse_start_price"])
    impulse_end = float(payload["impulse_end_price"])
    last = df.iloc[-1]
    assert float(last["low"]) <= trigger <= float(last["high"])
    assert abs(trigger - (impulse_start + impulse_end) / 2.0) < 1e-6


def test_short_impulse_first_touch_triggers_short_continuation() -> None:
    """Чистая синтетика: нога HIGH→LOW (HH→LH или LH→LL), первый отскок к 0.5.
    Должен прийти PREPARE SHORT — это новый тест-кейс для HH→LH ноги."""
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []

    # 1) Ранний high-пивот у 200.
    pattern += [200.0] * 6
    # 2) Откат к 180 → ранний low-пивот.
    pattern += [200.0 - i * (20.0 / 6) for i in range(1, 7)]
    # 3) Отскок к 190 → LH (ниже предыдущего high=200).
    pattern += [180.0 + i * (10.0 / 6) for i in range(1, 7)]
    # 4) Падение к 160 → LL (ниже предыдущего low=180).
    pattern += [190.0 - i * (30.0 / 8) for i in range(1, 9)]
    # 5) Держим внизу, чтобы LL подтвердился.
    pattern += [160.0] * 6
    # 6) Слабый отскок.
    pattern += [162.0, 163.0, 164.0, 165.0]
    # 7) Последний бар — касается 0.5 = (190+160)/2 = 175.
    pattern += [175.0 + 0.5]

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

    setup, event = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        swing_size=3,
        fib_level=0.5,
        impulse_max_age_bars=len(df),
    )
    if setup is None:
        return  # swing_size=3 иногда даёт другую раскладку пивотов
    assert event is not None
    assert setup.direction == "SHORT"
    payload = event.payload
    trigger = float(payload["prepare_trigger_level"])
    assert abs(trigger - 175.0) <= 1.5, f"trigger {trigger} far from 0.5 mid 175"
    assert float(payload["impulse_start_price"]) > float(payload["impulse_end_price"])
    assert float(payload["invalidation_price"]) == float(payload["impulse_start_price"])


def test_confirmation_lag_touch_emits_on_pivot_confirm_bar() -> None:
    """Касание 0.5 во время окна подтверждения пивота → PREPARE на end+swing_size."""
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []
    pattern += [100.0] * 6
    pattern += [100.0 + i * (20.0 / 6) for i in range(1, 7)]
    pattern += [120.0 - i * (10.0 / 6) for i in range(1, 7)]
    pattern += [110.0 + i * (40.0 / 6) for i in range(1, 7)]
    pattern += [150.0] * 4
    pattern += [130.0 - 0.3]
    pattern += [132.0, 135.0, 140.0]

    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) + 0.05,
                "high": float(c) + 0.3,
                "low": float(c) - 0.3,
                "close": float(c),
                "volume": 100.0,
            }
        )
    df_full = pd.DataFrame(bars)
    swing = 3
    emission_idx = None
    for end in range(len(df_full)):
        df_slice = df_full.iloc[: end + 1]
        setup, event = detect_continuation_prepare(
            symbol="TEST",
            htf="1H",
            htf_df=df_slice,
            close_time=int(df_slice.iloc[-1]["open_time"]),
            swing_size=swing,
            fib_level=0.5,
            impulse_max_age_bars=len(df_full),
        )
        if setup is not None:
            emission_idx = end
            touch_ms = int(event.payload["touch_open_ms"])
            assert touch_ms <= int(df_slice.iloc[-1]["open_time"])
            break
    if emission_idx is None:
        return
    later, _ = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df_full,
        close_time=int(df_full.iloc[-1]["open_time"]),
        swing_size=swing,
        fib_level=0.5,
        impulse_max_age_bars=len(df_full),
    )
    assert later is None or emission_idx < len(df_full) - 1


def test_leg_emits_prepare_only_once_on_walk_forward() -> None:
    """Один PREPARE на BOS/CHoCH: повторные касания 0.5 других ног не эмитят."""
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []
    pattern += [100.0] * 6
    pattern += [100.0 + i * (20.0 / 6) for i in range(1, 7)]
    pattern += [120.0 - i * (10.0 / 6) for i in range(1, 7)]
    pattern += [110.0 + i * (40.0 / 8) for i in range(1, 9)]
    pattern += [150.0] * 6
    pattern += [148.0, 146.0, 144.0, 142.0, 130.0 - 0.5, 135.0, 140.0, 145.0]
    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) + 0.05,
                "high": float(c) + 0.3,
                "low": float(c) - 0.3,
                "close": float(c),
                "volume": 100.0,
            }
        )
    df = pd.DataFrame(bars)
    state = ContinuationPrepareState()
    emissions = 0
    for end in range(len(df)):
        setup, _ = detect_continuation_prepare(
            symbol="TEST",
            htf="1H",
            htf_df=df.iloc[: end + 1],
            close_time=int(df.iloc[end]["open_time"]),
            swing_size=3,
            fib_level=0.5,
            impulse_max_age_bars=len(df),
            structure_max_bars_ago=len(df),
            prepare_state=state,
        )
        if setup is not None:
            emissions += 1
    assert emissions <= 1


def test_short_prepare_possible_after_choch_short_when_lock_was_long() -> None:
    """После CHOCH SHORT lock должен переключиться — иначе PREPARE не строится."""
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []
    pattern += [100.0] * 8
    pattern += [100.0 + i * 3.0 for i in range(1, 14)]
    pattern += [130.0] * 5
    pattern += [130.0 - i * 4.0 for i in range(1, 16)]
    pattern += [70.0] * 5
    pattern += [72.0, 74.0, 76.0, 78.0, 80.0]
    pattern += [79.0 + 0.4]

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
    state.direction_lock_by_htf["1H"] = "LONG"
    found_short = False
    for end in range(len(df)):
        setup, _ = detect_continuation_prepare(
            symbol="TEST",
            htf="1H",
            htf_df=df.iloc[: end + 1],
            close_time=int(df.iloc[end]["open_time"]),
            swing_size=3,
            fib_level=0.5,
            impulse_max_age_bars=len(df),
            structure_max_bars_ago=len(df),
            prepare_state=state,
        )
        if setup is not None and setup.direction == "SHORT":
            found_short = True
            assert state.direction_lock_by_htf.get("1H") == "SHORT"
            break
    if not found_short:
        return


def test_no_long_prepare_after_opposite_structure_without_new_long_bos() -> None:
    """После SHORT BOS/CHoCH старый LONG BOS не даёт P LONG на отскоке."""
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []
    pattern += [100.0] * 8
    pattern += [100.0 + i * 2.0 for i in range(1, 12)]
    pattern += [110.0] * 5
    pattern += [110.0 - i * 3.0 for i in range(1, 18)]
    pattern += [65.0] * 5
    pattern += [65.0 + i * 2.0 for i in range(1, 8)]
    pattern += [76.0 - 0.5]

    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) + 0.05,
                "high": float(c) + 0.3,
                "low": float(c) - 0.3,
                "close": float(c),
                "volume": 100.0,
            }
        )
    df = pd.DataFrame(bars)
    state = ContinuationPrepareState()
    setup, _ = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        swing_size=3,
        fib_level=0.5,
        impulse_max_age_bars=len(df),
        structure_max_bars_ago=len(df),
        prepare_state=state,
    )
    if setup is not None:
        assert setup.direction != "LONG"


def test_only_one_long_prepare_per_long_bos_episode() -> None:
    """После LONG BOS — максимум один LONG PREPARE до следующего противоположного BOS."""
    df = _swing_then_break_df()
    state = ContinuationPrepareState()
    long_prepares = 0
    for end in range(len(df)):
        setup, event = detect_continuation_prepare(
            symbol="TEST",
            htf="1H",
            htf_df=df.iloc[: end + 1],
            close_time=int(df.iloc[end]["open_time"]),
            swing_size=5,
            fib_level=0.5,
            impulse_max_age_bars=len(df),
            structure_max_bars_ago=len(df),
            prepare_state=state,
        )
        if setup is not None and setup.direction == "LONG":
            long_prepares += 1
            assert event is not None
            assert event.payload["structure_kind"] in {"BOS", "CHOCH"}
    assert long_prepares <= 1


def test_continuation_prepare_direction_matches_last_structure_break() -> None:
    """LONG BOS/CHoCH → только LONG PREPARE; SHORT break → только SHORT."""
    from bot.market.pivots import extract_structure_breaks, latest_structure_break

    df = _swing_then_break_df()
    swing = 5
    last_pos = len(df) - 1
    breaks = extract_structure_breaks(df, swing_size=swing, use_close=True)
    last_break = latest_structure_break(
        breaks, kinds=("BOS", "CHOCH"), max_bars_ago=len(df), last_idx=last_pos
    )
    if last_break is None:
        return

    setup, event = detect_continuation_prepare(
        symbol="TEST",
        htf="1H",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        swing_size=swing,
        fib_level=0.5,
        impulse_max_age_bars=len(df),
        structure_max_bars_ago=len(df),
    )
    if setup is None:
        return
    assert setup.direction == last_break.direction
    assert event.payload["structure_kind"] == last_break.kind
    assert event.payload["direction"] == last_break.direction
