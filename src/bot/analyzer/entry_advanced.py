from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from bot.analyzer.entry_ltf import detect_entry_structure_confirm, resolve_entry_swing_size
from bot.config import EntryConfig
from bot.market.pivots import LtfChoCh, detect_pivots


@dataclass(slots=True, frozen=True)
class AdvancedEntryUpdate:
    stage: str
    sweep_level: float | None
    sweep_extreme: float | None
    sweep_ms: int | None
    reclaim_ms: int | None
    confirm_level: float | None
    confirm_ms: int | None


@dataclass(slots=True, frozen=True)
class AdvancedEntryResult:
    status: str  # WAITING_CONFIRM | CONFIRMED
    wait_suffix: str
    update: AdvancedEntryUpdate | None = None
    choch: LtfChoCh | None = None
    recommended_stop: float | None = None
    recommended_stop_source: str | None = None
    target_price: float | None = None
    rr_to_target: float | None = None


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    work = df.tail(max(period + 1, 2)).copy()
    prev_close = work["close"].shift(1)
    tr = pd.concat(
        [
            (work["high"] - work["low"]).abs(),
            (work["high"] - prev_close).abs(),
            (work["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = float(tr.tail(period).mean())
    return max(value, 0.0)


def _bars_after(df: pd.DataFrame, open_ms: int | None) -> int:
    if open_ms is None:
        return 0
    return int((df["open_time"] > int(open_ms)).sum())


def _last_sweep_level(
    df: pd.DataFrame,
    *,
    direction: str,
    swing_size: int,
    lookback_bars: int,
) -> float | None:
    last_pos = int(df.index[-1])
    min_idx = max(0, last_pos - max(1, int(lookback_bars)))
    pivots = detect_pivots(df, swing_size=swing_size)
    wanted = "LOW" if direction == "LONG" else "HIGH"
    candidates = [
        pivot
        for pivot in pivots
        if pivot.kind == wanted
        and min_idx <= pivot.idx < last_pos
    ]
    return float(candidates[-1].price) if candidates else None


def _reclaimed(*, direction: str, close: float, sweep_level: float) -> bool:
    if direction == "LONG":
        return close > sweep_level
    return close < sweep_level


def _swept(*, direction: str, low: float, high: float, sweep_level: float) -> bool:
    if direction == "LONG":
        return low < sweep_level
    return high > sweep_level


def _updated_extreme(*, direction: str, current: float, previous: float | None) -> float:
    if previous is None:
        return current
    if direction == "LONG":
        return min(current, previous)
    return max(current, previous)


def _extreme_broken(*, direction: str, low: float, high: float, extreme: float) -> bool:
    if direction == "LONG":
        return low < extreme
    return high > extreme


def _confirm_filters_ok(
    df: pd.DataFrame,
    *,
    choch: LtfChoCh,
    entry: EntryConfig,
    atr: float,
) -> bool:
    cfg = entry.advanced
    if choch.broken_open_ms is None:
        return False
    matched = df[df["open_time"] == int(choch.broken_open_ms)]
    if matched.empty:
        return False
    row = matched.iloc[-1]
    if cfg.require_displacement:
        body = abs(float(row["close"]) - float(row["open"]))
        if atr <= 0 or body < atr * float(cfg.displacement_body_atr_min):
            return False
    if cfg.require_volume_expansion:
        pos = int(matched.index[-1])
        baseline = df.iloc[max(0, pos - 20) : pos]["volume"]
        if baseline.empty:
            return False
        if float(row["volume"]) < float(baseline.mean()) * float(cfg.volume_multiplier):
            return False
    return True


def _reclaim_filters_ok(
    df: pd.DataFrame,
    *,
    direction: str,
    entry: EntryConfig,
    atr: float,
) -> bool:
    cfg = entry.advanced
    row = df.iloc[-1]
    if cfg.require_displacement:
        if cfg.require_directional_reclaim:
            if direction == "LONG" and float(row["close"]) <= float(row["open"]):
                return False
            if direction == "SHORT" and float(row["close"]) >= float(row["open"]):
                return False
        body = abs(float(row["close"]) - float(row["open"]))
        if atr <= 0 or body < atr * float(cfg.displacement_body_atr_min):
            return False
    if cfg.require_volume_expansion:
        baseline = df.iloc[max(0, len(df) - 21) : -1]["volume"]
        if baseline.empty:
            return False
        if float(row["volume"]) < float(baseline.mean()) * float(cfg.volume_multiplier):
            return False
    return True


def _recommended_stop(
    *,
    direction: str,
    sweep_extreme: float,
    atr: float,
    buffer_atr: float,
) -> float:
    buffer = atr * buffer_atr
    if direction == "LONG":
        return sweep_extreme - buffer
    return sweep_extreme + buffer


def _rr_to_target(*, direction: str, entry_price: float, stop: float, target: float) -> float:
    risk = abs(entry_price - stop)
    if risk == 0:
        return 0.0
    if direction == "LONG":
        return (target - entry_price) / risk
    return (entry_price - target) / risk


def _confirmed_reclaim_result(
    *,
    setup: Any,
    direction: str,
    close: float,
    sweep_level: float,
    sweep_extreme: float,
    bar_ms: int,
    atr: float,
    entry: EntryConfig,
) -> AdvancedEntryResult:
    cfg = entry.advanced
    stop = _recommended_stop(
        direction=direction,
        sweep_extreme=sweep_extreme,
        atr=atr,
        buffer_atr=float(cfg.stop_buffer_atr),
    )
    if atr <= 0 or abs(close - stop) > atr * float(cfg.max_stop_atr):
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="sweep_reclaim_stop_too_wide",
            update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
        )
    target = getattr(setup, "entry_target_price", None)
    if target is None:
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="sweep_reclaim_no_target",
            update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
        )
    rr = _rr_to_target(direction=direction, entry_price=close, stop=stop, target=float(target))
    if rr < float(cfg.min_rr_to_htf_target):
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="sweep_reclaim_rr",
            update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
        )
    return AdvancedEntryResult(
        status="CONFIRMED",
        wait_suffix="sweep_reclaim_confirmed",
        choch=LtfChoCh(
            direction=direction,
            level=sweep_level,
            bars_ago=0,
            kind="RECLAIM",
            broken_open_ms=bar_ms,
            reset_level=sweep_extreme,
        ),
        recommended_stop=stop,
        recommended_stop_source="sweep_extreme",
        target_price=float(target),
        rr_to_target=rr,
    )


def resolve_advanced_entry(
    *,
    setup: Any,
    ltf_df: pd.DataFrame,
    used_tf: str,
    entry: EntryConfig,
    pivot_swing_by_tf: dict[str, int] | None,
    liberal_swing_override: dict[str, int] | None,
    use_close: bool,
) -> AdvancedEntryResult:
    row = ltf_df.iloc[-1]
    direction = str(setup.direction)
    stage = str(getattr(setup, "entry_advanced_stage", None) or "WAIT_SWEEP")
    bar_ms = int(row["open_time"])
    low = float(row["low"])
    high = float(row["high"])
    close = float(row["close"])
    atr = _atr(ltf_df)
    cfg = entry.advanced
    mode = str(getattr(setup, "entry_mode", None) or entry.mode).lower()

    sweep_level = getattr(setup, "entry_sweep_level", None)
    sweep_extreme = getattr(setup, "entry_sweep_extreme", None)
    sweep_ms = getattr(setup, "entry_sweep_ms", None)
    reclaim_ms = getattr(setup, "entry_reclaim_ms", None)
    confirm_level = getattr(setup, "entry_confirm_level", None)
    confirm_ms = getattr(setup, "entry_confirm_ms", None)

    def update(next_stage: str) -> AdvancedEntryUpdate:
        return AdvancedEntryUpdate(
            stage=next_stage,
            sweep_level=None if sweep_level is None else float(sweep_level),
            sweep_extreme=None if sweep_extreme is None else float(sweep_extreme),
            sweep_ms=None if sweep_ms is None else int(sweep_ms),
            reclaim_ms=None if reclaim_ms is None else int(reclaim_ms),
            confirm_level=None if confirm_level is None else float(confirm_level),
            confirm_ms=None if confirm_ms is None else int(confirm_ms),
        )

    swing = resolve_entry_swing_size(
        entry,
        used_tf,
        is_liberal=bool(getattr(setup, "is_liberal", False)),
        liberal_override=liberal_swing_override,
        pivot_swing_by_tf=pivot_swing_by_tf,
    )

    if stage == "WAIT_SWEEP":
        sweep_level = _last_sweep_level(
            ltf_df,
            direction=direction,
            swing_size=swing,
            lookback_bars=cfg.sweep_lookback_bars,
        )
        if sweep_level is None or not _swept(
            direction=direction, low=low, high=high, sweep_level=float(sweep_level)
        ):
            return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_sweep")
        sweep_extreme = low if direction == "LONG" else high
        sweep_ms = bar_ms
        if _reclaimed(direction=direction, close=close, sweep_level=float(sweep_level)):
            if mode == "sweep_reclaim":
                if not _reclaim_filters_ok(ltf_df, direction=direction, entry=entry, atr=atr):
                    return AdvancedEntryResult(
                        status="WAITING_CONFIRM",
                        wait_suffix="sweep_reclaim_displacement",
                        update=AdvancedEntryUpdate(
                            "WAIT_SWEEP", None, None, None, None, None, None
                        ),
                    )
                return _confirmed_reclaim_result(
                    setup=setup,
                    direction=direction,
                    close=close,
                    sweep_level=float(sweep_level),
                    sweep_extreme=float(sweep_extreme),
                    bar_ms=bar_ms,
                    atr=atr,
                    entry=entry,
                )
            reclaim_ms = bar_ms
            return AdvancedEntryResult(
                status="WAITING_CONFIRM",
                wait_suffix="advanced_choch",
                update=update("WAIT_CHOCH"),
            )
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="advanced_reclaim",
            update=update("WAIT_RECLAIM"),
        )

    if sweep_level is None or sweep_extreme is None:
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="advanced_reset",
            update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
        )

    if stage == "WAIT_RECLAIM":
        sweep_extreme = _updated_extreme(
            direction=direction,
            current=low if direction == "LONG" else high,
            previous=float(sweep_extreme),
        )
        if _bars_after(ltf_df, sweep_ms) > int(cfg.reclaim_max_bars):
            return AdvancedEntryResult(
                status="WAITING_CONFIRM",
                wait_suffix="advanced_sweep",
                update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
            )
        if not _reclaimed(direction=direction, close=close, sweep_level=float(sweep_level)):
            return AdvancedEntryResult(
                status="WAITING_CONFIRM",
                wait_suffix="advanced_reclaim",
                update=update("WAIT_RECLAIM"),
            )
        if mode == "sweep_reclaim":
            if not _reclaim_filters_ok(ltf_df, direction=direction, entry=entry, atr=atr):
                return AdvancedEntryResult(
                    status="WAITING_CONFIRM",
                    wait_suffix="sweep_reclaim_displacement",
                    update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
                )
            return _confirmed_reclaim_result(
                setup=setup,
                direction=direction,
                close=close,
                sweep_level=float(sweep_level),
                sweep_extreme=float(sweep_extreme),
                bar_ms=bar_ms,
                atr=atr,
                entry=entry,
            )
        reclaim_ms = bar_ms
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="advanced_choch",
            update=update("WAIT_CHOCH"),
        )

    if stage in {"WAIT_CHOCH", "WAIT_RETEST"} and _extreme_broken(
        direction=direction, low=low, high=high, extreme=float(sweep_extreme)
    ):
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="advanced_sweep",
            update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
        )

    if stage == "WAIT_CHOCH":
        if _bars_after(ltf_df, reclaim_ms) > int(cfg.confirm_max_bars):
            return AdvancedEntryResult(
                status="WAITING_CONFIRM",
                wait_suffix="advanced_sweep",
                update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
            )
        choch = detect_entry_structure_confirm(
            entry=entry,
            ltf_df=ltf_df,
            used_tf=used_tf,
            direction=direction,
            since_open_ms=(int(reclaim_ms) + 1) if reclaim_ms is not None else None,
            pivot_swing_by_tf=pivot_swing_by_tf,
            liberal_swing_override=liberal_swing_override,
            is_liberal=bool(getattr(setup, "is_liberal", False)),
            use_close=use_close,
            structure_kinds=tuple(cfg.confirm_structure_kinds),
            lookback_mode="since_prepare",
        )
        if choch is None or choch.bars_ago > int(cfg.confirm_max_bars):
            return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_choch")
        if not _confirm_filters_ok(ltf_df, choch=choch, entry=entry, atr=atr):
            return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_displacement")
        confirm_level = float(choch.level)
        confirm_ms = int(choch.broken_open_ms or bar_ms)
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="advanced_retest",
            update=update("WAIT_RETEST"),
        )

    if stage != "WAIT_RETEST" or confirm_level is None or confirm_ms is None:
        return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_reset")
    if _bars_after(ltf_df, confirm_ms) > int(cfg.retest_max_bars):
        return AdvancedEntryResult(
            status="WAITING_CONFIRM",
            wait_suffix="advanced_sweep",
            update=AdvancedEntryUpdate("WAIT_SWEEP", None, None, None, None, None, None),
        )
    if bar_ms <= int(confirm_ms):
        return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_retest")

    tolerance = atr * float(cfg.retest_tolerance_atr)
    retest_ok = (
        low <= float(confirm_level) + tolerance and close > float(confirm_level)
        if direction == "LONG"
        else high >= float(confirm_level) - tolerance and close < float(confirm_level)
    )
    if not retest_ok:
        return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_retest")

    if cfg.stop_source == "retest_extreme":
        stop_extreme = low if direction == "LONG" else high
    else:
        stop_extreme = float(sweep_extreme)
    stop = _recommended_stop(
        direction=direction,
        sweep_extreme=float(stop_extreme),
        atr=atr,
        buffer_atr=float(cfg.stop_buffer_atr),
    )
    if atr <= 0 or abs(close - stop) > atr * float(cfg.max_stop_atr):
        return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_stop_too_wide")
    target = getattr(setup, "entry_target_price", None)
    if target is None:
        return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_no_target")
    rr = _rr_to_target(direction=direction, entry_price=close, stop=stop, target=float(target))
    if rr < float(cfg.min_rr_to_htf_target):
        return AdvancedEntryResult(status="WAITING_CONFIRM", wait_suffix="advanced_rr")

    return AdvancedEntryResult(
        status="CONFIRMED",
        wait_suffix="advanced_confirmed",
        choch=LtfChoCh(
            direction=direction,
            level=float(confirm_level),
            bars_ago=0,
            kind="CHOCH",
            broken_open_ms=int(confirm_ms),
            reset_level=float(sweep_extreme),
        ),
        recommended_stop=stop,
        recommended_stop_source=str(cfg.stop_source),
        target_price=float(target),
        rr_to_target=rr,
    )
