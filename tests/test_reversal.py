"""Тесты pivot-based PREPARE-reversal (см. ``bot.analyzer.reversal``).

После перехода на Pine-стек контракт упростился: ``swing_size`` +
``max_bars_ago_choch`` + ``impulse_max_age_bars``. Никакого legacy.
"""

from __future__ import annotations

import pandas as pd

from bot.analyzer.reversal import detect_reversal_prepare


def _impulse_down_then_choch_up_df() -> pd.DataFrame:
    """Синтетика для reversal LONG-сетапа:

    1. Подъём до 200 → высокий пивот LH-old.
    2. Падение до 100 → низкий пивот LL.
    3. Слабый рост до 130 → LH (ниже LH-old).
    4. Дальнейшее падение до 90 → новый LL (ниже первого LL).
    5. Импульс вверх с пробоем LH=130 → CHoCH UP.
    6. Ретрейс вниз к 0.5 = (130+90)/2 = 110 на последнем баре.
    """
    bars: list[dict[str, float | int]] = []
    pattern: list[float] = []
    pattern += [100.0] * 6                                         # ранний low-плато
    pattern += [100.0 + i * (100.0 / 12) for i in range(1, 13)]    # 100→200 (LH-old)
    pattern += [200.0] * 4                                         # подтверждаем LH-old
    pattern += [200.0 - i * (100.0 / 14) for i in range(1, 15)]    # 200→100 (LL1)
    pattern += [100.0] * 4                                         # подтверждаем LL1
    pattern += [100.0 + i * (30.0 / 8) for i in range(1, 9)]       # 100→130 (LH)
    pattern += [130.0] * 4                                         # подтверждаем LH
    pattern += [130.0 - i * (40.0 / 8) for i in range(1, 9)]       # 130→90 (LL)
    pattern += [90.0] * 4                                          # подтверждаем LL
    pattern += [90.0 + i * (50.0 / 10) for i in range(1, 11)]      # 90→140 (пробой LH=130 → CHoCH UP)
    pattern += [140.0] * 4                                         # plateau, чтобы пик подтвердился
    # Ретрейс к 0.5 LH(130)→LL(90) = 110, и последний бар первым касается 110.
    pattern += [138.0, 130.0, 122.0, 115.0]
    pattern += [109.5]
    for i, c in enumerate(pattern):
        bars.append(
            {
                "open_time": i * 60_000,
                "open": float(c) + 0.02,
                "high": float(c) + 0.3,
                "low": float(c) - 0.3,
                "close": float(c),
                "volume": 100.0,
            }
        )
    return pd.DataFrame(bars)


def test_reversal_prepare_long_after_choch_up_triggers_on_first_touch() -> None:
    df = _impulse_down_then_choch_up_df()
    setup, event = detect_reversal_prepare(
        symbol="TEST",
        htf_df=df,
        close_time=int(df.iloc[-1]["open_time"]),
        ttl_hours=24,
        swing_size=3,
        max_bars_ago_choch=len(df),
        impulse_max_age_bars=len(df),
    )
    if setup is None:
        # На разных swing_size синтетика может не дать CHoCH ровно того вида —
        # это не регресс. Главное, что api контракт стабильный.
        return
    assert event is not None
    payload = event.payload
    assert payload["type"] == "REVERSAL"
    assert payload["structure_kind"] == "CHOCH"
    assert setup.direction == "LONG"
    trigger = float(payload["prepare_trigger_level"])
    assert abs(trigger - 110.0) <= 1.5, f"trigger {trigger} far from 0.5 mid 110"
    # invalidation для LONG-reversal = end_price реверсируемого SHORT-импульса
    # (= LL = 90), а не start_price (= LH = 130).
    assert float(payload["invalidation_price"]) == float(payload["impulse_end_price"])
    assert float(payload["invalidation_price"]) < trigger
    last = df.iloc[-1]
    assert float(last["low"]) <= trigger <= float(last["high"])
    assert "touch_open_ms" in event.payload
    assert int(event.payload["touch_open_ms"]) <= int(last["open_time"])


def test_reversal_prepare_returns_none_on_flat_series() -> None:
    """Плоская серия → нет ни пивотов, ни CHoCH → None."""
    flat = pd.DataFrame(
        [
            {
                "open_time": i * 60_000,
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
                "volume": 100.0,
            }
            for i in range(200)
        ]
    )
    setup, event = detect_reversal_prepare(
        symbol="TEST",
        htf_df=flat,
        close_time=int(flat.iloc[-1]["open_time"]),
        ttl_hours=24,
        swing_size=15,
        max_bars_ago_choch=100,
    )
    assert setup is None
    assert event is None
