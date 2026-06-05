from __future__ import annotations

from typing import Any

import pandas as pd

from bot.config import EntryConfig
from bot.market.pivots import (
    LtfChoCh,
    detect_ltf_entry_confirm,
)

# Дефолты, если в конфиге нет ключа для HTF.
DEFAULT_LTF_BY_HTF: dict[str, str] = {
    "4H": "5M|15M|1H",
    "1H": "5M|15M",
    "15M": "5M",
}

FINE_TO_COARSE = ("1M", "5M", "15M", "1H", "4H")
COARSE_TO_FINE = ("4H", "1H", "15M", "5M", "1M")


def parse_ltf_pipe(value: str) -> list[str]:
    return [part.strip() for part in value.split("|") if part.strip()]


def ltf_expected_for_htf(htf: str, entry: EntryConfig) -> str:
    """Pipe-список LTF для ENTRY (порядок = приоритет: сначала младший TF)."""
    cascade = cascade_sequence_for_htf(htf, entry)
    if cascade:
        return "|".join(cascade)
    if entry.ltf_by_htf and htf in entry.ltf_by_htf:
        return str(entry.ltf_by_htf[htf])
    return DEFAULT_LTF_BY_HTF.get(htf, "5M")


def cascade_sequence_for_htf(htf: str, entry: EntryConfig) -> list[str]:
    if not bool(entry.cascade_enabled):
        return []
    if not entry.cascade_by_htf or htf not in entry.cascade_by_htf:
        return []
    return parse_ltf_pipe(str(entry.cascade_by_htf[htf]))


def finest_closed_ltf(
    ltf_expected: str,
    *,
    closed_tfs: list[str],
    available: set[str],
) -> str | None:
    """Младший TF из списка, чей бар закрылся в этом тике."""
    closed = set(closed_tfs)
    candidates = parse_ltf_pipe(ltf_expected)
    for tf in FINE_TO_COARSE:
        if tf in candidates and tf in closed and tf in available:
            return tf
    return None


def resolve_entry_swing_size(
    entry: EntryConfig,
    tf: str,
    *,
    is_liberal: bool,
    liberal_override: dict[str, int] | None = None,
    pivot_swing_by_tf: dict[str, int] | None = None,
) -> int:
    """Swing для LTF BOS/CHoCH. ``ltf_swing_use_pivot_sizes`` — как на оверлее."""
    if is_liberal and liberal_override:
        if tf in liberal_override:
            return int(liberal_override[tf])
    if entry.ltf_swing_use_pivot_sizes and pivot_swing_by_tf and tf in pivot_swing_by_tf:
        return int(pivot_swing_by_tf[tf])
    return int(entry.ltf_swing_length.get(tf, 8))


def prepare_since_open_ms(setup: Any) -> int:
    """Момент PREPARE для since_prepare (touch HTF, не open текущего 4H-бара)."""
    ms = getattr(setup, "prepare_since_ms", None)
    if ms is not None:
        return int(ms)
    close_time = getattr(setup, "close_time", None)
    if close_time is not None:
        return int(close_time)
    created = getattr(setup, "created_at", None)
    if created is not None:
        return int(created.timestamp() * 1000)
    return 0


def ltf_directional_close_ok(*, close: float, open_: float, direction: str) -> bool:
    if direction == "LONG":
        return close > open_
    return close < open_


def detect_entry_structure_confirm(
    *,
    entry: EntryConfig,
    ltf_df: pd.DataFrame,
    used_tf: str,
    direction: str,
    since_open_ms: int | None,
    pivot_swing_by_tf: dict[str, int] | None,
    liberal_swing_override: dict[str, int] | None,
    is_liberal: bool,
    use_close: bool,
    structure_kinds: tuple[str, ...],
    lookback_mode: str,
) -> LtfChoCh | None:
    swing = resolve_entry_swing_size(
        entry,
        used_tf,
        is_liberal=is_liberal,
        liberal_override=liberal_swing_override,
        pivot_swing_by_tf=pivot_swing_by_tf,
    )
    max_bars = int(entry.ltf_max_bars_ago_by_tf.get(used_tf, entry.ltf_max_bars_ago))
    kinds = tuple(k.upper() for k in structure_kinds) or ("CHOCH",)
    return detect_ltf_entry_confirm(
        ltf_df,
        swing_size=swing,
        max_bars_ago=max_bars,
        use_close=use_close,
        kinds=kinds,
        direction=direction,
        since_open_ms=since_open_ms,
        lookback_mode=lookback_mode,
    )


def try_entry_confirm(
    *,
    entry: EntryConfig,
    ltf_df: pd.DataFrame,
    used_tf: str,
    setup: Any,
    pivot_swing_by_tf: dict[str, int] | None,
    liberal_swing_override: dict[str, int] | None,
    is_liberal: bool,
    use_close: bool = True,
) -> tuple[bool, LtfChoCh | None]:
    """Подтверждение ENTRY на закрытом LTF-баре."""
    row = ltf_df.iloc[-1]
    since_ms = prepare_since_open_ms(setup)
    mode = (entry.confirm_mode or "structure_break").lower()

    if mode == "directional_close":
        if not ltf_directional_close_ok(
            close=float(row["close"]),
            open_=float(row["open"]),
            direction=str(setup.direction),
        ):
            return False, None
        return True, LtfChoCh(
            direction=str(setup.direction),
            level=float(row["open"]),
            bars_ago=0,
            kind="CLOSE",
        )

    kinds = tuple(k.upper() for k in entry.confirm_structure_kinds) or ("CHOCH",)
    choch = detect_entry_structure_confirm(
        entry=entry,
        ltf_df=ltf_df,
        used_tf=used_tf,
        direction=str(setup.direction),
        since_open_ms=since_ms,
        pivot_swing_by_tf=pivot_swing_by_tf,
        liberal_swing_override=liberal_swing_override,
        is_liberal=is_liberal,
        use_close=use_close,
        structure_kinds=kinds,
        lookback_mode=entry.structure_lookback,
    )
    if choch is None:
        return False, None
    return True, choch


def invalidation_tf_for_setup(
    setup_htf: str,
    ltf_expected: str,
    entry: EntryConfig,
    available: set[str],
) -> str:
    """TF для проверки invalidation (по умолчанию HTF сетапа, не младший ENTRY-TF)."""
    if entry.invalidation_ltf_by_htf and setup_htf in entry.invalidation_ltf_by_htf:
        for tf in COARSE_TO_FINE:
            if tf in parse_ltf_pipe(entry.invalidation_ltf_by_htf[setup_htf]) and tf in available:
                return tf
    if setup_htf in available:
        return setup_htf
    candidates = parse_ltf_pipe(ltf_expected)
    for tf in COARSE_TO_FINE:
        if tf in candidates and tf in available:
            return tf
    return candidates[0] if candidates else setup_htf
