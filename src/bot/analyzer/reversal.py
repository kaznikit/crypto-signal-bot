from __future__ import annotations

from collections import Counter

from bot.analyzer.continuation import detect_continuation_prepare
from bot.analyzer.setup_machine import SetupEvent
from bot.storage.models import Setup, SetupType

REVERSAL_TRIGGER_FIB: float = 0.5


def _funnel_inc(funnel: Counter[str] | None, key: str) -> None:
    if funnel is not None:
        funnel[key] += 1


def detect_reversal_prepare(
    *,
    symbol: str,
    htf_df,
    close_time: int,
    swing_size: int,
    max_bars_ago_choch: int,
    impulse_max_age_bars: int = 60,
    bos_use_close: bool = True,
    ttl_hours: int,
    funnel: Counter[str] | None = None,
    ltf_expected: str = "5M|15M|1H",
    entry_mode: str = "simple",
) -> tuple[Setup | None, SetupEvent | None]:
    """PREPARE-reversal как частный случай continuation.

    Reversal теперь использует тот же алгоритм построения импульса и 0.5:
    направление сетапа берётся по CHOCH, а импульс — в этом же направлении.
    Отличие от continuation только в якоре структуры (CHOCH only).
    """
    return detect_continuation_prepare(
        symbol=symbol,
        htf="4H",
        htf_df=htf_df,
        close_time=close_time,
        swing_size=swing_size,
        fib_level=REVERSAL_TRIGGER_FIB,
        impulse_max_age_bars=impulse_max_age_bars,
        bos_use_close=bos_use_close,
        ttl_hours=ttl_hours,
        funnel=funnel,
        structure_max_bars_ago=max_bars_ago_choch,
        prepare_state=None,
        ltf_expected=ltf_expected,
        entry_mode=entry_mode,
        setup_type=SetupType.REVERSAL,
        anchor_kinds=("CHOCH",),
    )
