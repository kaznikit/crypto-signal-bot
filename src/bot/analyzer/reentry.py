from __future__ import annotations

import pandas as pd


def reentry_swing_reset_reached(
    *,
    ltf_df: pd.DataFrame | None,
    direction: str,
    last_entry_bar_ms: int | None,
    last_entry_swing_level: float | None,
) -> bool:
    """Проверка reset для re-entry: цена должна проколоть прошлый LTF swing."""
    if last_entry_bar_ms is None:
        return True
    if last_entry_swing_level is None:
        return False
    if ltf_df is None or ltf_df.empty:
        return False
    tail = ltf_df[ltf_df["open_time"] > int(last_entry_bar_ms)]
    if tail.empty:
        return False
    if direction == "LONG":
        return float(tail["low"].min()) <= float(last_entry_swing_level)
    if direction == "SHORT":
        return float(tail["high"].max()) >= float(last_entry_swing_level)
    return False


def reentry_price_improved(
    *,
    direction: str,
    entry_price: float,
    last_entry_price: float | None,
) -> bool:
    """Повторный вход должен быть по цене лучше предыдущего."""
    if last_entry_price is None:
        return True
    if direction == "LONG":
        return float(entry_price) < float(last_entry_price)
    if direction == "SHORT":
        return float(entry_price) > float(last_entry_price)
    return False


def reentry_has_new_structure_break(
    *,
    confirm_broken_open_ms: int | None,
    last_entry_bar_ms: int | None,
) -> bool:
    """Повторный вход допустим только после нового BOS/CHoCH."""
    if confirm_broken_open_ms is None or last_entry_bar_ms is None:
        return False
    return int(confirm_broken_open_ms) > int(last_entry_bar_ms)
