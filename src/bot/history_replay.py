from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any

from bot.analyzer.continuation import (
    ContinuationPrepareState,
    detect_continuation_prepare,
)
from bot.analyzer.entry_ltf import (
    ltf_expected_for_htf,
    prepare_since_open_ms,
)
from bot.analyzer.fib_dca import (
    FibDcaLevel,
    deserialize_filled_fibs,
    deserialize_plan,
    filled_weight_pct,
    initial_trigger_fills,
    initialize_fib_dca_setup,
    new_fib_dca_fills,
    planned_risk,
    position_pnl,
    serialize_filled_fibs,
    weighted_average_entry,
)
from bot.analyzer.filters import (
    atr_percent,
    close_beyond_level,
    finalize_entry_levels,
    recommended_entry_stop,
)
from bot.analyzer.reentry import (
    reentry_has_new_structure_break,
    reentry_price_improved,
    reentry_swing_reset_reached,
)
from bot.analyzer.reversal import detect_reversal_prepare
from bot.analyzer.setup_lifecycle import (
    apply_reset_after_first_entry_policy,
    decide_setup_structure_transition,
)
from bot.analyzer.setup_runtime import check_price_invalidation, resolve_ltf_confirmation
from bot.analyzer.strategy_gates import (
    evaluate_continuation_prepare_detailed,
    evaluate_continuation_prepare_liberal,
    evaluate_reversal_prepare_detailed,
    evaluate_reversal_prepare_liberal,
)
from bot.config import EnvConfig, load_bot_config
from bot.exchange.bybit_client import BybitClient
from bot.market.candles import candles_to_df
from bot.market.fibo import OteZone, is_price_in_zone
from bot.market.pivots import (
    compute_impulse_lock_state,
    detect_pivots,
    detect_pivots_htf,
    extract_impulse_legs_confirmed,
    extract_structure_breaks_htf,
    impulse_invalidated,
    pivot_label_for_htf_display,
)

logger = logging.getLogger(__name__)

TF_MS: dict[str, int] = {
    "1M": 60 * 1000,
    "5M": 5 * 60 * 1000,
    "15M": 15 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}

DEFAULT_MAX_EXPANDED_BARS_PER_TF = 4_000


@dataclass(slots=True)
class ReplaySetup:
    id: str
    symbol: str
    setup_type: str
    direction: str
    htf: str
    ltf_expected: str
    invalidation_price: float
    state: str
    close_time: int
    expires_time: int
    ote_low: float
    ote_high: float
    phase: str = "WAIT_CHOCH"
    is_liberal: bool = False
    prepare_since_ms: int | None = None
    entry_count: int = 0
    last_entry_bar_ms: int | None = None
    last_entry_price: float | None = None
    last_entry_swing_level: float | None = None
    entry_cascade_stage: int = 0
    entry_cascade_since_ms: int | None = None
    entry_cascade_touch_ms: int | None = None
    entry_cascade_retrace_level: float | None = None
    entry_mode: str = "simple"
    entry_advanced_stage: str = "WAIT_SWEEP"
    entry_sweep_level: float | None = None
    entry_sweep_extreme: float | None = None
    entry_sweep_ms: int | None = None
    entry_reclaim_ms: int | None = None
    entry_confirm_level: float | None = None
    entry_confirm_ms: int | None = None
    entry_target_price: float | None = None
    fib_dca_plan_json: str | None = None
    fib_dca_filled_json: str | None = None
    fib_dca_average_entry: float | None = None
    fib_dca_filled_weight_pct: float = 0.0
    fib_dca_last_fill_ms: int | None = None


@dataclass(slots=True)
class OpenTrade:
    setup_id: str
    symbol: str
    setup_type: str
    direction: str
    tf: str
    entry_time: int
    entry: float
    sl: float
    tp: float
    risk: float


@dataclass(slots=True)
class FibOpenPosition:
    setup_id: str
    symbol: str
    setup_type: str
    direction: str
    tf: str
    entry_time: int
    plan: list[FibDcaLevel]
    filled_fibs: set[float]
    sl: float
    tp: float


@dataclass(slots=True)
class ClosedTrade:
    setup_id: str
    symbol: str
    setup_type: str
    direction: str
    tf: str
    entry_time: int
    exit_time: int
    entry: float
    exit: float
    r_multiple: float
    exit_reason: str


@dataclass(slots=True)
class ReplaySummary:
    symbol: str
    mode: str
    trades: int
    wins: int
    losses: int
    breakeven: int
    winrate_pct: float
    avg_r: float
    median_r: float
    total_r: float
    max_drawdown_r: float
    profit_factor: float


def _find_config_path() -> Path:
    cwd = Path.cwd()
    for candidate in (cwd / "config.yaml", Path(__file__).resolve().parents[2] / "config.yaml"):
        if candidate.exists():
            return candidate
    msg = "Не найден config.yaml (запускайте из каталога crypto-signal-bot)."
    raise SystemExit(msg)


def _load_timeframes(
    mode: str,
    cfg: Any,
    focus_htf: str | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    replay_targets: list[str] = []
    if mode in {"reversal", "both"} and (focus_htf is None or focus_htf == "4H"):
        replay_targets.append("reversal")
    if mode in {"continuation", "both"} and (
        focus_htf is None or focus_htf in set(cfg.prepare_htfs())
    ):
        replay_targets.append("continuation")
    needed: set[str] = set()

    if "reversal" in replay_targets:
        needed.add("4H")
        if cfg.entry.mode == "fib_dca":
            needed.add(str(cfg.entry.fib_dca.monitoring_tf_by_htf.get("4H", "4H")))
        else:
            needed.update(
                tf
                for tf in str(ltf_expected_for_htf("4H", cfg.entry)).split("|")
                if tf and tf in TF_MS
            )

    if "continuation" in replay_targets:
        if focus_htf is None:
            cont_htfs = cfg.prepare_htfs()
        else:
            cont_htfs = tuple(htf for htf in cfg.prepare_htfs() if htf == focus_htf)
        for htf in cont_htfs:
            needed.add(htf)
            if cfg.entry.mode == "fib_dca":
                needed.add(str(cfg.entry.fib_dca.monitoring_tf_by_htf.get(htf, htf)))
            else:
                needed.update(
                    tf
                    for tf in str(ltf_expected_for_htf(htf, cfg.entry)).split("|")
                    if tf and tf in TF_MS
                )

    # Safety net: replay requires at least one LTF stream for ENTRY checks.
    if not any(tf in needed for tf in ("1M", "5M", "15M", "1H", "4H")):
        needed.add("5M")

    return tuple(sorted(needed, key=lambda tf: TF_MS[tf])), tuple(replay_targets)


def _apply_entry_cascade_update(setup: Any, update: Any) -> None:
    setup.entry_cascade_stage = int(update.stage)
    setup.entry_cascade_since_ms = update.since_ms
    setup.entry_cascade_touch_ms = update.touch_ms
    setup.entry_cascade_retrace_level = update.retrace_level


def _apply_advanced_entry_update(setup: Any, update: Any) -> None:
    setup.entry_advanced_stage = str(update.stage)
    setup.entry_sweep_level = update.sweep_level
    setup.entry_sweep_extreme = update.sweep_extreme
    setup.entry_sweep_ms = update.sweep_ms
    setup.entry_reclaim_ms = update.reclaim_ms
    setup.entry_confirm_level = update.confirm_level
    setup.entry_confirm_ms = update.confirm_ms


def _expanded_limits_by_tf(
    *,
    needed_tfs: tuple[str, ...],
    limit: int,
    max_bars_per_tf: int,
) -> dict[str, int]:
    """Расширить лимиты младших TF до горизонта старшего TF.

    Пример: при ``limit=1000`` и старшем TF=4H для ``5M`` нужно ~48000 баров,
    иначе ранние PREPARE на 1H/4H остаются без LTF-подтверждения только из-за
    нехватки глубины младшего таймфрейма.
    """
    if limit <= 0:
        return {tf: 0 for tf in needed_tfs}
    max_tf_ms = max(TF_MS[tf] for tf in needed_tfs)
    out: dict[str, int] = {}
    for tf in needed_tfs:
        tf_ms = TF_MS[tf]
        # ceil(limit * max_tf_ms / tf_ms)
        scaled = (limit * max_tf_ms + tf_ms - 1) // tf_ms
        out[tf] = min(max_bars_per_tf, max(limit, int(scaled)))
    return out


def _format_bybit_download_error(
    *,
    symbol: str,
    needed_tfs: tuple[str, ...],
    limit_by_tf: dict[str, int],
    exc: Exception,
) -> str:
    proxy_keys = (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    )
    proxy_env = [key for key in proxy_keys if os.environ.get(key)]
    tf_limits = ", ".join(f"{tf}:{limit_by_tf[tf]}" for tf in needed_tfs)
    lines = [
        f"Не удалось загрузить свечи Bybit для {symbol}.",
        f"TF/limit: {tf_limits}",
        f"Ошибка: {type(exc).__name__}: {exc}",
    ]
    if "WRONG_VERSION_NUMBER" in str(exc).upper():
        lines.extend(
            [
                "",
                "SSL WRONG_VERSION_NUMBER чаще всего означает неверный proxy/VPN:",
                "- HTTPS_PROXY/HTTP_PROXY указывает не на тот порт или схему;",
                "- HTTPS-прокси задан как https://, хотя он принимает обычный http:// CONNECT;",
                "- корпоративный/VPN proxy подменяет TLS или Bybit недоступен через текущую сеть.",
            ]
        )
    if proxy_env:
        lines.append("")
        lines.append("Найдены proxy-переменные:")
        lines.extend(f"- {key}=<set>" for key in proxy_env)
        lines.append("")
        lines.append("Для быстрой проверки попробуйте временно снять proxy и повторить команду:")
        lines.append("unset HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy")
    else:
        lines.append("")
        lines.append("Proxy-переменные в окружении процесса не найдены.")
    return "\n".join(lines)


def _print_fetch_progress(tf: str, loaded: int, limit: int) -> None:
    print(f"Fetching Bybit candles {tf}: {loaded}/{limit}", flush=True)


def _print_replay_progress(done: int, total: int, events_count: int) -> None:
    print(f"Replaying history: {done}/{total} bars, events={events_count}", flush=True)


def _print_progress(message: str) -> None:
    print(message, flush=True)


def _invalidate_armed_replay_setups_by_key(
    setups: list[ReplaySetup],
    *,
    key: tuple[str, str, str, str],
) -> int:
    """Сбрасывает ARMED setup'ы по dedup-ключу.

    Возвращает количество сброшенных setup'ов.
    """
    symbol, setup_type, htf, direction = key
    invalidated = 0
    for setup in setups:
        if setup.state != "ARMED":
            continue
        if (
            setup.symbol == symbol
            and setup.setup_type == setup_type
            and setup.htf == htf
            and setup.direction == direction
        ):
            setup.state = "INVALIDATED"
            invalidated += 1
    return invalidated


def _emit_fresh_pivot_events(
    *,
    df: Any,
    htf: str,
    symbol: str,
    swing_size: int,
    bos_use_close: bool,
    events_out: list[dict[str, Any]],
) -> None:
    """Эмитим Pine-style события, чьё «появление» приходится на закрытый бар.

    Три вида событий:

    * **PIVOT** — HH/LH/HL/LL-метка. Пивот подтверждается через ``swing_size``
      баров после самого пивота, поэтому на текущем баре в `events_out`
      кладётся пивот, попавший на ``last_pos - swing_size``.
    * **IMPULSE** — leg HL→HH (LONG) или LH→LL (SHORT), подтверждённая BOS/CHoCH
      на текущем баре (``anchor_break_idx == last_pos``). На overlay = диагональ
      + 0.5-линия.
    * **STRUCTURE** — BOS / CHoCH (первое close-пересечение активного
      prevHigh/prevLow). Эмитится мгновенно, без задержки на ``swing_size``.
    """
    raw_pivots = detect_pivots(df, swing_size=swing_size)
    pivots = detect_pivots_htf(
        df, swing_size=swing_size, use_close=bos_use_close, impulse_lock=True
    )
    if not pivots and len(df) < 2 * swing_size + 1:
        return
    last_pos = int(df.index[-1])
    confirm_idx = last_pos - swing_size
    visible_breaks = extract_structure_breaks_htf(
        df, swing_size=swing_size, use_close=bos_use_close, impulse_lock=True
    )
    impulse_legs = extract_impulse_legs_confirmed(
        raw_pivots, visible_breaks, swing_size=swing_size, df=df
    )
    lock_state = compute_impulse_lock_state(
        df,
        raw_pivots,
        swing_size=swing_size,
        use_close=bos_use_close,
        breaks=visible_breaks,
    )

    for pivot in pivots:
        if pivot.idx != confirm_idx:
            continue
        events_out.append(
            {
                "kind": "PIVOT",
                "symbol": symbol,
                "htf": htf,
                "label": pivot_label_for_htf_display(
                    pivot, lock_state, impulse_legs=impulse_legs
                ),
                "pivot_kind": pivot.kind,
                "price": pivot.price,
                "bar_open_ms": int(df.iloc[pivot.idx]["open_time"]),
            }
        )

    for leg in impulse_legs:
        if leg.anchor_break_idx is None:
            continue
        # IMPULSE появляется на чарте, когда нога первый раз становится
        # buildable — для этого должны быть подтверждены и BOS/CHoCH (broken_idx),
        # и end-pivot (end_idx + swing_size, чтобы он стал валидным pivot'ом).
        # Если end формируется уже после пробоя (continuation BOS), эмиссия
        # уезжает на бар подтверждения нового пика, иначе IMPULSE никогда не
        # попадёт на overlay.
        confirm_bar = max(leg.anchor_break_idx, leg.end_idx + swing_size)
        if confirm_bar != last_pos:
            continue
        events_out.append(
            {
                "kind": "IMPULSE",
                "symbol": symbol,
                "htf": htf,
                "direction": leg.direction,
                "start_open_ms": int(df.iloc[leg.start_idx]["open_time"]),
                "start_price": float(leg.start_price),
                "end_open_ms": int(df.iloc[leg.end_idx]["open_time"]),
                "end_price": float(leg.end_price),
                "bar_open_ms": int(df.iloc[last_pos]["open_time"]),
            }
        )
    for br_pos, br in enumerate(visible_breaks):
        if br.broken_idx != last_pos:
            continue
        swing_idx = br.swing_idx
        swing_price = float(br.swing_price)
        # Переякориваем только CHOCH: для BOS оставляем штатный swing из ядра,
        # иначе continuation может визуально ссылаться на слишком старый HH/LL
        # и давать «дополнительный HL/LH без обновления экстремума».
        if br.kind == "CHOCH":
            last_opposite_broken = -1
            for prev in reversed(visible_breaks[:br_pos]):
                if prev.direction != br.direction:
                    last_opposite_broken = prev.broken_idx
                    break
            if br.direction == "SHORT":
                lows = [
                    p
                    for p in raw_pivots
                    if p.kind == "LOW" and last_opposite_broken < p.idx < br.broken_idx
                ]
                if lows:
                    best = min(lows, key=lambda p: (p.price, p.idx))
                    swing_idx = best.idx
                    swing_price = float(best.price)
            else:
                highs = [
                    p
                    for p in raw_pivots
                    if p.kind == "HIGH" and last_opposite_broken < p.idx < br.broken_idx
                ]
                if highs:
                    best = max(highs, key=lambda p: (p.price, -p.idx))
                    swing_idx = best.idx
                    swing_price = float(best.price)
        try:
            swing_open_ms = int(df.iloc[swing_idx]["open_time"])
            break_open_ms = int(df.iloc[br.broken_idx]["open_time"])
        except (KeyError, IndexError):
            continue
        # Если swing-pivot был скрыт HTF фильтрацией, но именно от него строится
        # STRUCTURE, добавляем pivot в overlay ретроспективно, чтобы CHOCH/BOS
        # визуально «шёл от HL/LH».
        swing_pivot = next((p for p in raw_pivots if p.idx == swing_idx), None)
        if swing_pivot is not None:
            events_out.append(
                {
                    "kind": "PIVOT",
                    "symbol": symbol,
                    "htf": htf,
                    "label": pivot_label_for_htf_display(
                        swing_pivot, lock_state, impulse_legs=impulse_legs
                    ),
                    "pivot_kind": swing_pivot.kind,
                    "price": float(swing_pivot.price),
                    "bar_open_ms": swing_open_ms,
                }
            )
        events_out.append(
            {
                "kind": "STRUCTURE",
                "symbol": symbol,
                "htf": htf,
                "subkind": br.kind,
                "direction": br.direction,
                    "level": swing_price,
                "swing_open_ms": swing_open_ms,
                "bar_open_ms": break_open_ms,
            }
        )


def _filter_invalidated_impulses(
    events: list[dict[str, Any]],
    dfs: dict[str, Any],
) -> None:
    """Пост-пасс: убираем IMPULSE-маркеры структурно сломанных импульсов.

    После пика импульса (LONG: HL→HH; SHORT: LH→LL) проверяем по всему
    оставшемуся df: если цена пробила ``start_price`` (для LONG — low < HL,
    для SHORT — high > LH), импульс структурно мёртв и диагональ не должна
    отображаться. Это убирает «длинные зелёные линии от перекрытых
    импульсов», на которые жаловался пользователь.

    Pine-индикатор так не делает (рисует все исторические HL→HH leg'и), но
    для нашего overlay это удобнее — иначе старые impulse-линии
    накапливаются на чарте.
    """
    open_time_to_idx: dict[str, dict[int, int]] = {
        tf: {int(t): i for i, t in enumerate(df["open_time"].tolist())}
        for tf, df in dfs.items()
    }

    referenced_impulses: set[tuple[str, int, int, str]] = set()
    for ev in events:
        if ev.get("kind") != "PREPARE":
            continue
        htf = str(ev.get("htf") or "")
        direction = str(ev.get("direction") or "")
        start_ms = int(ev.get("impulse_leg_start_open_ms") or 0)
        end_ms = int(ev.get("impulse_leg_end_open_ms") or 0)
        if not htf or direction not in {"LONG", "SHORT"} or start_ms <= 0 or end_ms <= 0:
            continue
        referenced_impulses.add((htf, start_ms, end_ms, direction))

    keep: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("kind") != "IMPULSE":
            keep.append(ev)
            continue
        htf = str(ev.get("htf") or "")
        df = dfs.get(htf)
        if df is None or df.empty:
            keep.append(ev)
            continue
        end_ms = int(ev.get("end_open_ms") or 0)
        end_idx = open_time_to_idx.get(htf, {}).get(end_ms, -1)
        direction = str(ev.get("direction") or "")
        try:
            start_price = float(ev.get("start_price") or 0.0)
        except (TypeError, ValueError):
            keep.append(ev)
            continue
        if end_idx < 0 or direction not in ("LONG", "SHORT"):
            keep.append(ev)
            continue
        impulse_key = (htf, int(ev.get("start_open_ms") or 0), end_ms, direction)
        if impulse_key in referenced_impulses:
            # Если IMPULSE реально использован в PREPARE, оставляем его на чарте,
            # даже если позже этот импульс был структурно сломан.
            keep.append(ev)
            continue
        if impulse_invalidated(
            df,
            direction=direction,
            start_price=start_price,
            after_idx=end_idx,
        ):
            continue
        keep.append(ev)
    events[:] = keep


def _normalize_structure_sequence(events: list[dict[str, Any]]) -> None:
    """Нормализация STRUCTURE для overlay: CHOCH только при смене направления.

    На графике пользователю нужна последовательность:
    - смена направления -> CHOCH
    - продолжение в том же направлении -> BOS
    """
    last_dir_by_htf: dict[str, str] = {}
    for ev in events:
        if ev.get("kind") != "STRUCTURE":
            continue
        htf = str(ev.get("htf") or "")
        direction = str(ev.get("direction") or "")
        if not htf or direction not in {"LONG", "SHORT"}:
            continue
        prev_dir = last_dir_by_htf.get(htf)
        ev["subkind"] = "BOS" if prev_dir == direction else "CHOCH"
        last_dir_by_htf[htf] = direction


def _filter_stale_structure_events(
    events: list[dict[str, Any]],
    dfs: dict[str, Any],
    cfg: Any,
) -> None:
    """Оставляет только STRUCTURE, которые присутствуют в финальном HTF-пересчёте.

    В walk-forward ранние flip-пробои могут исчезать после появления более
    валидного BOS/CHoCH в том же сегменте (ретроспективная нормализация
    ``extract_structure_breaks_htf``). Для overlay сохраняем только итоговые
    видимые STRUCTURE по полному хвосту каждого HTF.
    """
    valid_by_htf: dict[str, set[tuple[int, str]]] = {}
    for htf, df in dfs.items():
        if df is None or df.empty:
            continue
        swing = _swing_size_for_htf(cfg, htf)
        breaks = extract_structure_breaks_htf(
            df,
            swing_size=swing,
            use_close=cfg.pivots.bos_use_close,
            impulse_lock=True,
        )
        keys: set[tuple[int, str]] = set()
        for br in breaks:
            try:
                broken_open_ms = int(df.iloc[br.broken_idx]["open_time"])
            except (KeyError, IndexError):
                continue
            keys.add((broken_open_ms, br.direction))
        valid_by_htf[htf] = keys

    out: list[dict[str, Any]] = []
    removed_prepare_ids: set[str] = set()
    kept_prepare_ids: set[str] = set()
    for ev in events:
        kind = str(ev.get("kind") or "")
        if kind == "PREPARE":
            htf = str(ev.get("htf") or "")
            valid = valid_by_htf.get(htf)
            if valid is None:
                out.append(ev)
                if ev.get("setup_id"):
                    kept_prepare_ids.add(str(ev.get("setup_id")))
                continue
            key = (
                int(ev.get("structure_broken_open_ms") or 0),
                str(ev.get("direction") or ""),
            )
            if key in valid:
                out.append(ev)
                if ev.get("setup_id"):
                    kept_prepare_ids.add(str(ev.get("setup_id")))
            else:
                if ev.get("setup_id"):
                    removed_prepare_ids.add(str(ev.get("setup_id")))
            continue
        if kind != "STRUCTURE":
            out.append(ev)
            continue
        htf = str(ev.get("htf") or "")
        valid = valid_by_htf.get(htf)
        if valid is None:
            out.append(ev)
            continue
        key = (
            int(ev.get("bar_open_ms") or 0),
            str(ev.get("direction") or ""),
        )
        if key in valid:
            out.append(ev)
    if removed_prepare_ids:
        out = [
            ev
            for ev in out
            if not (
                str(ev.get("kind") or "") in {"ENTRY", "INVALIDATED"}
                and str(ev.get("setup_id") or "") in removed_prepare_ids
                and str(ev.get("setup_id") or "") not in kept_prepare_ids
            )
        ]
    events[:] = out


def _dedupe_overlay_events(events: list[dict[str, Any]]) -> None:
    """Удаляет дубли PIVOT/STRUCTURE/IMPULSE/PREPARE по стабильным ключам."""
    keep: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for ev in events:
        kind = str(ev.get("kind") or "")
        if kind == "PIVOT":
            key = (
                kind,
                str(ev.get("symbol") or ""),
                str(ev.get("htf") or ""),
                int(ev.get("bar_open_ms") or 0),
                str(ev.get("label") or ""),
                str(ev.get("pivot_kind") or ""),
            )
        elif kind == "STRUCTURE":
            key = (
                kind,
                str(ev.get("symbol") or ""),
                str(ev.get("htf") or ""),
                int(ev.get("bar_open_ms") or 0),
                str(ev.get("direction") or ""),
                str(ev.get("subkind") or ""),
                int(ev.get("swing_open_ms") or 0),
            )
        elif kind == "IMPULSE":
            key = (
                kind,
                str(ev.get("symbol") or ""),
                str(ev.get("htf") or ""),
                int(ev.get("start_open_ms") or 0),
                int(ev.get("end_open_ms") or 0),
                str(ev.get("direction") or ""),
            )
        elif kind == "PREPARE":
            key = (
                kind,
                str(ev.get("setup_id") or ""),
                int(ev.get("bar_open_ms") or 0),
            )
        elif kind == "INVALIDATED":
            key = (
                kind,
                str(ev.get("setup_id") or ""),
                int(ev.get("bar_open_ms") or 0),
            )
        else:
            key = (kind, id(ev))
        if key in seen:
            continue
        seen.add(key)
        keep.append(ev)
    events[:] = keep


def _keep_single_retrace_pivot_per_leg(
    events: list[dict[str, Any]],
    _dfs: dict[str, Any] | None = None,
    _cfg: Any | None = None,
) -> None:
    """Оставляет ровно один retrace-pivot между соседними STRUCTURE на каждом TF.

    Правило:
    - после STRUCTURE LONG ищем только LOW-пивоты (коррекция вниз), оставляем
      самый глубокий (min price);
    - после STRUCTURE SHORT ищем только HIGH-пивоты (коррекция вверх), оставляем
      самый глубокий (max price).

    Это устраняет ложные LH/HL без нового BOS/CHOCH и синхронизирует overlay с
    ожиданием «одна коррекционная метка между структурными событиями».

    Дополнительно сохраняются якоря подтверждённых IMPULSE-ног (start/end) —
    HH/LL на конце импульса всегда должен быть виден на overlay, даже если
    структурно он не служит swing-ом ни для какого BOS/CHOCH.
    """
    piv_by_tf: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    struct_by_tf: dict[str, list[dict[str, Any]]] = {}
    impulse_anchor_by_tf: dict[str, set[int]] = {}
    for i, ev in enumerate(events):
        kind = str(ev.get("kind") or "")
        tf = str(ev.get("htf") or "")
        if not tf:
            continue
        if kind == "PIVOT":
            piv_by_tf.setdefault(tf, []).append((i, ev))
        elif kind == "STRUCTURE":
            struct_by_tf.setdefault(tf, []).append(ev)
        elif kind == "IMPULSE":
            anchors = impulse_anchor_by_tf.setdefault(tf, set())
            start_ms = int(ev.get("start_open_ms") or 0)
            end_ms = int(ev.get("end_open_ms") or 0)
            if start_ms > 0:
                anchors.add(start_ms)
            if end_ms > 0:
                anchors.add(end_ms)
        elif kind == "PREPARE":
            anchors = impulse_anchor_by_tf.setdefault(tf, set())
            start_ms = int(ev.get("impulse_leg_start_open_ms") or 0)
            end_ms = int(ev.get("impulse_leg_end_open_ms") or 0)
            if start_ms > 0:
                anchors.add(start_ms)
            if end_ms > 0:
                anchors.add(end_ms)

    keep_pivot_idx: set[int] = set()
    for tf, pivots in piv_by_tf.items():
        structures = sorted(
            struct_by_tf.get(tf, []),
            key=lambda e: int(e.get("bar_open_ms") or 0),
        )
        anchor_pivot_ms = {
            int(e.get("swing_open_ms") or 0)
            for e in structures
            if int(e.get("swing_open_ms") or 0) > 0
        }
        anchor_pivot_ms |= impulse_anchor_by_tf.get(tf, set())
        if not structures:
            keep_pivot_idx.update(i for i, _ in pivots)
            continue

        first_struct_ms = int(structures[0].get("bar_open_ms") or 0)
        first_before_struct_by_label: dict[tuple[str, str], int] = {}
        for i, ev in pivots:
            p_ms = int(ev.get("bar_open_ms") or 0)
            if p_ms in anchor_pivot_ms:
                # Swing-anchors (для STRUCTURE/IMPULSE) всегда сохраняем:
                # даже при дубле label в сегменте иначе пропадает ключевая
                # HH/HL/LH/LL-метка, к которой привязана структура.
                keep_pivot_idx.add(i)
                continue
            if p_ms <= first_struct_ms:
                lbl = str(ev.get("label") or "")
                p_kind = str(ev.get("pivot_kind") or "")
                key = (lbl, p_kind)
                existing_idx = first_before_struct_by_label.get(key)
                if existing_idx is None:
                    first_before_struct_by_label[key] = i
                else:
                    ex_ms = int(events[existing_idx].get("bar_open_ms") or 0)
                    if p_ms < ex_ms:
                        first_before_struct_by_label[key] = i
        keep_pivot_idx.update(first_before_struct_by_label.values())

        for idx, st in enumerate(structures):
            direction = str(st.get("direction") or "")
            if direction not in {"LONG", "SHORT"}:
                continue
            start_ms = int(st.get("bar_open_ms") or 0)
            end_ms = (
                int(structures[idx + 1].get("bar_open_ms") or 0)
                if idx + 1 < len(structures)
                else 2**63 - 1
            )
            expected_kind = "LOW" if direction == "LONG" else "HIGH"
            candidates: list[tuple[int, dict[str, Any]]] = []
            for pidx, pev in pivots:
                p_ms = int(pev.get("bar_open_ms") or 0)
                if not (start_ms < p_ms < end_ms):
                    continue
                if str(pev.get("pivot_kind") or "") != expected_kind:
                    continue
                candidates.append((pidx, pev))
            if not candidates:
                continue
            if direction == "LONG":
                best = min(
                    candidates,
                    key=lambda x: (
                        float(x[1].get("price") or 0.0),
                        -int(x[1].get("bar_open_ms") or 0),
                    ),
                )
            else:
                best = max(
                    candidates,
                    key=lambda x: (
                        float(x[1].get("price") or 0.0),
                        int(x[1].get("bar_open_ms") or 0),
                    ),
                )
            keep_pivot_idx.add(best[0])

    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        if ev.get("kind") != "PIVOT" or i in keep_pivot_idx:
            out.append(ev)
    events[:] = out


def _collapse_impulse_fanout_by_start(events: list[dict[str, Any]]) -> None:
    """Оставляет один IMPULSE на стартовую точку (start_open_ms) в сегменте.

    В replay при последовательных BOS одна и та же стартовая HL/LH-точка может
    порождать несколько импульсов с разными end_open_ms. На overlay это
    выглядит как «веер» линий из одного бара.

    Правило выбора:
    - если среди кандидатов есть импульс, использованный в PREPARE, он
      приоритетнее;
    - внутри приоритета берём самый свежий end_open_ms.
    """
    prepare_refs: set[tuple[str, int, int, str]] = set()
    for ev in events:
        if str(ev.get("kind") or "") != "PREPARE":
            continue
        htf = str(ev.get("htf") or "")
        direction = str(ev.get("direction") or "")
        start_ms = int(ev.get("impulse_leg_start_open_ms") or 0)
        end_ms = int(ev.get("impulse_leg_end_open_ms") or 0)
        if not htf or direction not in {"LONG", "SHORT"} or start_ms <= 0 or end_ms <= 0:
            continue
        prepare_refs.add((htf, start_ms, end_ms, direction))

    best_idx_by_group: dict[tuple[str, str, str, int], int] = {}
    best_score_by_group: dict[tuple[str, str, str, int], tuple[int, int]] = {}
    for i, ev in enumerate(events):
        if str(ev.get("kind") or "") != "IMPULSE":
            continue
        symbol = str(ev.get("symbol") or "")
        htf = str(ev.get("htf") or "")
        direction = str(ev.get("direction") or "")
        start_ms = int(ev.get("start_open_ms") or 0)
        end_ms = int(ev.get("end_open_ms") or 0)
        if not symbol or not htf or direction not in {"LONG", "SHORT"}:
            continue
        if start_ms <= 0 or end_ms <= 0:
            continue
        group = (symbol, htf, direction, start_ms)
        is_prepare_ref = 1 if (htf, start_ms, end_ms, direction) in prepare_refs else 0
        score = (is_prepare_ref, end_ms)
        prev = best_score_by_group.get(group)
        if prev is None or score > prev:
            best_score_by_group[group] = score
            best_idx_by_group[group] = i

    keep_impulse_idx = set(best_idx_by_group.values())
    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        if str(ev.get("kind") or "") != "IMPULSE" or i in keep_impulse_idx:
            out.append(ev)
    events[:] = out


def _swing_size_for_htf(cfg: Any, tf: str) -> int:
    return int(cfg.pivots.swing_size_by_tf.get(tf, 15))


def _calc_r(direction: str, entry: float, exit_price: float, risk: float) -> float:
    if risk == 0:
        return 0.0
    if direction == "LONG":
        return (exit_price - entry) / risk
    return (entry - exit_price) / risk


def _resolve_trade_exit(
    trade: OpenTrade,
    high: float,
    low: float,
) -> tuple[bool, float, float, str]:
    if trade.direction == "LONG":
        hit_sl = low <= trade.sl
        hit_tp = high >= trade.tp
        if hit_sl and hit_tp:
            return True, trade.sl, -1.0, "both_hit_same_bar_sl_first"
        if hit_sl:
            return True, trade.sl, -1.0, "sl"
        if hit_tp:
            r = _calc_r(direction="LONG", entry=trade.entry, exit_price=trade.tp, risk=trade.risk)
            return True, trade.tp, r, "tp"
        return False, trade.entry, 0.0, "none"

    hit_sl = high >= trade.sl
    hit_tp = low <= trade.tp
    if hit_sl and hit_tp:
        return True, trade.sl, -1.0, "both_hit_same_bar_sl_first"
    if hit_sl:
        return True, trade.sl, -1.0, "sl"
    if hit_tp:
        r = _calc_r(direction="SHORT", entry=trade.entry, exit_price=trade.tp, risk=trade.risk)
        return True, trade.tp, r, "tp"
    return False, trade.entry, 0.0, "none"


def _has_open_position(
    open_trades: list[OpenTrade],
    fib_positions: dict[str, FibOpenPosition],
    *,
    setup_id: str | None = None,
) -> bool:
    if setup_id is None:
        return bool(open_trades or fib_positions)
    return any(trade.setup_id != setup_id for trade in open_trades) or any(
        current_setup_id != setup_id for current_setup_id in fib_positions
    )


def _append_invalidated_event(
    events_out: list[dict[str, Any]] | None,
    *,
    setup_id: str,
    symbol: str,
    setup_type: str,
    direction: str,
    timeframe: str,
    bar_open_ms: int,
    invalidation_price: float,
) -> None:
    if events_out is None:
        return
    events_out.append(
        {
            "kind": "INVALIDATED",
            "setup_id": setup_id,
            "symbol": symbol,
            "setup_type": setup_type,
            "direction": direction,
            "htf": timeframe,
            "bar_open_ms": bar_open_ms,
            "origin_price": invalidation_price,
            "invalidation_price": invalidation_price,
            "mark_price": invalidation_price,
        }
    )


def _max_drawdown_r(closed_trades: list[ClosedTrade]) -> float:
    if not closed_trades:
        return 0.0
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in sorted(closed_trades, key=lambda t: t.exit_time):
        running += trade.r_multiple
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _summarize(symbol: str, mode: str, closed_trades: list[ClosedTrade]) -> ReplaySummary:
    trades = len(closed_trades)
    if trades == 0:
        return ReplaySummary(
            symbol=symbol,
            mode=mode,
            trades=0,
            wins=0,
            losses=0,
            breakeven=0,
            winrate_pct=0.0,
            avg_r=0.0,
            median_r=0.0,
            total_r=0.0,
            max_drawdown_r=0.0,
            profit_factor=0.0,
        )

    rs = [t.r_multiple for t in closed_trades]
    wins = sum(1 for r in rs if r > 0)
    losses = sum(1 for r in rs if r < 0)
    breakeven = trades - wins - losses
    total_profit = sum(r for r in rs if r > 0)
    total_loss = abs(sum(r for r in rs if r < 0))
    profit_factor = float("inf") if total_loss == 0 and total_profit > 0 else 0.0
    if total_loss > 0:
        profit_factor = total_profit / total_loss

    return ReplaySummary(
        symbol=symbol,
        mode=mode,
        trades=trades,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        winrate_pct=(wins / trades) * 100.0,
        avg_r=mean(rs),
        median_r=median(rs),
        total_r=sum(rs),
        max_drawdown_r=_max_drawdown_r(closed_trades),
        profit_factor=profit_factor,
    )


def _print_report(
    summary: ReplaySummary,
    closed_trades: list[ClosedTrade],
    funnel: Counter[str],
    top_reasons: int,
) -> None:
    print("=== History Replay (PREPARE -> ENTRY -> TP/SL) ===")
    print(f"Symbol: {summary.symbol} | mode: {summary.mode}")
    print(
        f"Trades: {summary.trades} | Wins: {summary.wins} | Losses: {summary.losses} "
        f"| BE: {summary.breakeven} | Winrate: {summary.winrate_pct:.2f}%"
    )
    print(
        "avgR={avg:.3f} | medianR={med:.3f} | totalR={tot:.3f} | maxDD={dd:.3f}R | PF={pf}".format(
            avg=summary.avg_r,
            med=summary.median_r,
            tot=summary.total_r,
            dd=summary.max_drawdown_r,
            pf=("inf" if summary.profit_factor == float("inf") else f"{summary.profit_factor:.3f}"),
        )
    )

    if closed_trades:
        by_type: dict[str, list[ClosedTrade]] = {}
        for tr in closed_trades:
            by_type.setdefault(tr.setup_type, []).append(tr)
        print("\nBy setup type:")
        for setup_type, rows in sorted(by_type.items()):
            rs = [r.r_multiple for r in rows]
            wins = sum(1 for r in rs if r > 0)
            print(
                f"  {setup_type}: trades={len(rows)} winrate={(wins / len(rows)) * 100:.2f}% "
                f"avgR={mean(rs):.3f} totalR={sum(rs):.3f}"
            )

    print("\nTop funnel reasons:")
    for key, val in funnel.most_common(top_reasons):
        print(f"  {key}: {val}")


async def run_history_replay(
    *,
    symbol: str,
    limit: int,
    mode: str,
    top_reasons: int,
    config_path: Path,
    events_out: list[dict[str, Any]] | None = None,
    quiet: bool = False,
    focus_htf: str | None = None,
    progress: bool = False,
    overlay_tfs: set[str] | None = None,
    max_expanded_bars_per_tf: int | None = None,
) -> None:
    if not quiet:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_bot_config(config_path)
    max_entries_per_setup = max(1, int(cfg.entry.max_entries_per_setup))
    env = EnvConfig()
    client = BybitClient(
        category=cfg.exchange.category,
        api_key=env.bybit_api_key,
        api_secret=env.bybit_api_secret,
        domain=cfg.exchange.domain,
        tld=cfg.exchange.tld,
    )

    needed_tfs, replay_targets = _load_timeframes(mode, cfg, focus_htf=focus_htf)
    max_bars_per_tf = int(
        max_expanded_bars_per_tf
        if max_expanded_bars_per_tf is not None
        else cfg.history_replay.max_expanded_bars_per_tf
    )
    max_bars_per_tf = max(limit, max_bars_per_tf, DEFAULT_MAX_EXPANDED_BARS_PER_TF)
    limit_by_tf = _expanded_limits_by_tf(
        needed_tfs=needed_tfs,
        limit=limit,
        max_bars_per_tf=max_bars_per_tf,
    )

    if progress:
        tf_limits = ", ".join(f"{tf}:{limit_by_tf[tf]}" for tf in needed_tfs)
        print(f"Loading Bybit history for {symbol}: {tf_limits}", flush=True)

    tasks = [
        client.fetch_klines(
            symbol=symbol,
            timeframe=tf,
            limit=limit_by_tf[tf],
            progress=_print_fetch_progress if progress else None,
        )
        for tf in needed_tfs
    ]
    try:
        candles_by_tf = await asyncio.gather(*tasks)
    except Exception as exc:
        raise SystemExit(
            _format_bybit_download_error(
                symbol=symbol,
                needed_tfs=needed_tfs,
                limit_by_tf=limit_by_tf,
                exc=exc,
            )
        ) from exc
    dfs: dict[str, Any] = {}
    for tf, candles in zip(needed_tfs, candles_by_tf, strict=True):
        df = candles_to_df(candles)
        if df.empty:
            raise SystemExit(f"Пустые свечи для {symbol} {tf}")
        df = df.sort_values("open_time").reset_index(drop=True)
        dfs[tf] = df

    index_by_time: dict[str, dict[int, int]] = {
        tf: {int(open_time): int(i) for i, open_time in enumerate(df["open_time"].tolist())}
        for tf, df in dfs.items()
    }

    timeline = sorted({int(ts) for df in dfs.values() for ts in df["open_time"].tolist()})
    last_idx = {tf: -1 for tf in dfs}

    setups: list[ReplaySetup] = []
    open_trades: list[OpenTrade] = []
    fib_positions: dict[str, FibOpenPosition] = {}
    closed_trades: list[ClosedTrade] = []
    funnel: Counter[str] = Counter()
    continuation_prepare_state = ContinuationPrepareState()

    latest_4h_df: Any = None

    if progress:
        _print_progress(f"Replaying history: 0/{len(timeline)} bars")

    for pos, ts in enumerate(timeline, start=1):
        closed_now: list[str] = []
        for tf in needed_tfs:
            idx = index_by_time[tf].get(ts)
            if idx is not None:
                last_idx[tf] = idx
                closed_now.append(tf)

        if not closed_now:
            continue

        series: dict[str, Any] = {}
        for tf, idx in last_idx.items():
            if idx >= 0:
                series[tf] = dfs[tf].iloc[: idx + 1]

        if "4H" in series:
            latest_4h_df = series["4H"]

        if events_out is not None:
            for tf in closed_now:
                if overlay_tfs is not None and tf not in overlay_tfs:
                    continue
                _emit_fresh_pivot_events(
                    df=series[tf],
                    htf=tf,
                    symbol=symbol,
                    swing_size=_swing_size_for_htf(cfg, tf),
                    bos_use_close=cfg.pivots.bos_use_close,
                    events_out=events_out,
                )

        prepare_htfs = cfg.prepare_htfs()
        if focus_htf is not None:
            prepare_htfs = tuple(tf for tf in prepare_htfs if tf == focus_htf)
        if "reversal" in replay_targets and "4H" in prepare_htfs and "4H" in closed_now:
            df_4h = series["4H"]
            liberal_cfg = cfg.paper_mode.liberal
            strict_lookback = cfg.reversal.choch_lookback_bars
            swing_4h = _swing_size_for_htf(cfg, "4H")

            atr_v = atr_percent(df_4h)
            if atr_v < cfg.filters.min_atr_pct and not (
                liberal_cfg.enabled and atr_v >= liberal_cfg.min_atr_pct
            ):
                funnel["reversal_low_atr"] += 1
            else:
                # Сначала пытаемся строгое окно; если ничего нет — расширенное
                # liberal-окно (только в paper-чат).
                rev_ltf = ltf_expected_for_htf("4H", cfg.entry)
                setup_obj, event = detect_reversal_prepare(
                    symbol=symbol,
                    htf_df=df_4h,
                    close_time=int(df_4h.iloc[-1]["open_time"]),
                    ttl_hours=cfg.reversal.ttl_bars_4h * 4,
                    swing_size=swing_4h,
                    max_bars_ago_choch=strict_lookback,
                    impulse_max_age_bars=cfg.pivots.impulse_max_age_bars,
                    bos_use_close=cfg.pivots.bos_use_close,
                    funnel=funnel,
                    ltf_expected=rev_ltf,
                    entry_mode=cfg.entry.mode,
                )
                liberal_wider_choch = False
                if (setup_obj is None or event is None) and liberal_cfg.enabled:
                    setup_obj, event = detect_reversal_prepare(
                        symbol=symbol,
                        htf_df=df_4h,
                        close_time=int(df_4h.iloc[-1]["open_time"]),
                        ttl_hours=cfg.reversal.ttl_bars_4h * 4,
                        swing_size=swing_4h,
                        max_bars_ago_choch=liberal_cfg.max_bars_ago_4h,
                        impulse_max_age_bars=cfg.pivots.impulse_max_age_bars,
                        bos_use_close=cfg.pivots.bos_use_close,
                        funnel=funnel,
                        ltf_expected=rev_ltf,
                        entry_mode=cfg.entry.mode,
                    )
                    liberal_wider_choch = setup_obj is not None
                if setup_obj is None or event is None:
                    funnel["reversal_no_prepare_candidate"] += 1
                else:
                    initialize_fib_dca_setup(
                        setup=setup_obj,
                        prepare_payload=event.payload,
                        config=cfg.entry.fib_dca,
                    )
                    dedup_key = (symbol, setup_obj.type, "4H", setup_obj.direction)
                    replaced = _invalidate_armed_replay_setups_by_key(
                        setups,
                        key=dedup_key,
                    )
                    if replaced > 0:
                        funnel["reversal_prepare_replaced_by_new_structure"] += replaced

                    gate = evaluate_reversal_prepare_detailed(
                        df=df_4h,
                        choch_direction=setup_obj.direction,
                        setup=setup_obj,
                        event=event,
                        features=cfg.strategy_features,
                    )
                    accepted = False
                    if gate.ok and not liberal_wider_choch:
                        setup_obj.score = gate.score
                        setup_obj.is_liberal = False
                        accepted = True
                    elif liberal_cfg.enabled:
                        lib_gate = evaluate_reversal_prepare_liberal(
                            df=df_4h,
                            choch_direction=setup_obj.direction,
                            setup=setup_obj,
                            event=event,
                            features=cfg.strategy_features,
                            liberal=liberal_cfg,
                        )
                        if lib_gate.ok:
                            setup_obj.score = lib_gate.score
                            setup_obj.is_liberal = True
                            accepted = True
                        else:
                            funnel[gate.reason] += 1
                            funnel[f"liberal_{lib_gate.reason}"] += 1
                    else:
                        funnel[gate.reason] += 1

                    if accepted:
                        setups.append(
                            ReplaySetup(
                                id=setup_obj.id,
                                symbol=symbol,
                                setup_type=setup_obj.type,
                                direction=setup_obj.direction,
                                htf="4H",
                                ltf_expected=setup_obj.ltf_expected,
                                invalidation_price=setup_obj.invalidation_price,
                                state="ARMED",
                                close_time=int(df_4h.iloc[-1]["open_time"]),
                                expires_time=int(df_4h.iloc[-1]["open_time"])
                                + (cfg.reversal.ttl_bars_4h * TF_MS["4H"]),
                                ote_low=setup_obj.ote_low,
                                ote_high=setup_obj.ote_high,
                                phase=setup_obj.phase,
                                is_liberal=setup_obj.is_liberal,
                                prepare_since_ms=int(
                                    event.payload.get(
                                        "touch_open_ms",
                                        df_4h.iloc[-1]["open_time"],
                                    )
                                ),
                                entry_mode=setup_obj.entry_mode,
                                entry_target_price=setup_obj.entry_target_price,
                                fib_dca_plan_json=setup_obj.fib_dca_plan_json,
                                fib_dca_filled_json=setup_obj.fib_dca_filled_json,
                                fib_dca_average_entry=setup_obj.fib_dca_average_entry,
                                fib_dca_filled_weight_pct=setup_obj.fib_dca_filled_weight_pct,
                                fib_dca_last_fill_ms=setup_obj.fib_dca_last_fill_ms,
                            )
                        )
                        funnel["reversal_prepare_created"] += 1
                        if events_out is not None:
                            events_out.append(
                                {
                                    "kind": "PREPARE",
                                    "setup_id": setup_obj.id,
                                    "symbol": symbol,
                                    "setup_type": setup_obj.type,
                                    "direction": setup_obj.direction,
                                    "htf": "4H",
                                    "bar_open_ms": int(
                                        event.payload.get(
                                            "touch_open_ms",
                                            df_4h.iloc[-1]["open_time"],
                                        )
                                    ),
                                    "origin_price": setup_obj.origin_price,
                                    "ote_low": setup_obj.ote_low,
                                    "ote_high": setup_obj.ote_high,
                                    "invalidation_price": setup_obj.invalidation_price,
                                    "is_liberal": setup_obj.is_liberal,
                                    "structure_kind": event.payload.get("structure_kind"),
                                    "structure_swing_open_ms": event.payload.get(
                                        "structure_swing_open_ms"
                                    ),
                                    "structure_broken_open_ms": event.payload.get(
                                        "structure_broken_open_ms"
                                    ),
                                    "impulse_leg_start_open_ms": event.payload.get(
                                        "impulse_leg_start_open_ms"
                                    ),
                                    "impulse_leg_end_open_ms": event.payload.get(
                                        "impulse_leg_end_open_ms"
                                    ),
                                    "structure_break_key": event.payload.get(
                                        "structure_break_key"
                                    ),
                                    "entry_mode": setup_obj.entry_mode,
                                    "fib_dca_levels": event.payload.get("fib_dca_levels"),
                                }
                            )

        if "continuation" in replay_targets:
            liberal_cfg = cfg.paper_mode.liberal
            for htf in prepare_htfs:
                if htf not in closed_now:
                    continue
                df_htf = series[htf]
                setup_obj, event = detect_continuation_prepare(
                    symbol=symbol,
                    htf=htf,
                    htf_df=df_htf,
                    close_time=int(df_htf.iloc[-1]["open_time"]),
                    swing_size=_swing_size_for_htf(cfg, htf),
                    structure_max_bars_ago=cfg.continuation.structure_max_bars_ago,
                    fib_level=cfg.continuation.fib_low,
                    impulse_max_age_bars=cfg.pivots.impulse_max_age_bars,
                    bos_use_close=cfg.pivots.bos_use_close,
                    ttl_hours=24,
                    funnel=funnel,
                    prepare_state=continuation_prepare_state,
                    ltf_expected=ltf_expected_for_htf(htf, cfg.entry),
                    entry_mode=cfg.entry.mode,
                )
                if setup_obj is None or event is None:
                    funnel[f"continuation_{htf.lower()}_no_prepare_candidate"] += 1
                    continue
                initialize_fib_dca_setup(
                    setup=setup_obj,
                    prepare_payload=event.payload,
                    config=cfg.entry.fib_dca,
                )

                dedup_key = (symbol, setup_obj.type, htf, setup_obj.direction)
                replaced = _invalidate_armed_replay_setups_by_key(
                    setups,
                    key=dedup_key,
                )
                if replaced > 0:
                    key = f"continuation_{htf.lower()}_prepare_replaced_by_new_structure"
                    funnel[key] += replaced

                gate = evaluate_continuation_prepare_detailed(
                    df_htf=df_htf,
                    setup=setup_obj,
                    event=event,
                    features=cfg.strategy_features,
                    df_4h=latest_4h_df,
                )
                accepted = False
                if gate.ok:
                    setup_obj.score = gate.score
                    setup_obj.is_liberal = False
                    accepted = True
                elif liberal_cfg.enabled:
                    lib_gate = evaluate_continuation_prepare_liberal(
                        df_htf=df_htf,
                        setup=setup_obj,
                        event=event,
                        features=cfg.strategy_features,
                        df_4h=latest_4h_df,
                        liberal=liberal_cfg,
                    )
                    if lib_gate.ok:
                        setup_obj.score = lib_gate.score
                        setup_obj.is_liberal = True
                        accepted = True
                    else:
                        funnel[gate.reason] += 1
                        funnel[f"liberal_{lib_gate.reason}"] += 1
                else:
                    funnel[gate.reason] += 1

                if not accepted:
                    continue

                ttl_ms = 24 * 60 * 60 * 1000
                setups.append(
                    ReplaySetup(
                        id=setup_obj.id,
                        symbol=symbol,
                        setup_type=setup_obj.type,
                        direction=setup_obj.direction,
                        htf=htf,
                        ltf_expected=setup_obj.ltf_expected,
                        invalidation_price=setup_obj.invalidation_price,
                        state="ARMED",
                        close_time=int(df_htf.iloc[-1]["open_time"]),
                        expires_time=int(df_htf.iloc[-1]["open_time"]) + ttl_ms,
                        ote_low=setup_obj.ote_low,
                        ote_high=setup_obj.ote_high,
                        phase=setup_obj.phase,
                        is_liberal=setup_obj.is_liberal,
                        prepare_since_ms=int(
                            event.payload.get(
                                "touch_open_ms",
                                df_htf.iloc[-1]["open_time"],
                            )
                        ),
                        entry_mode=setup_obj.entry_mode,
                        entry_target_price=setup_obj.entry_target_price,
                        fib_dca_plan_json=setup_obj.fib_dca_plan_json,
                        fib_dca_filled_json=setup_obj.fib_dca_filled_json,
                        fib_dca_average_entry=setup_obj.fib_dca_average_entry,
                        fib_dca_filled_weight_pct=setup_obj.fib_dca_filled_weight_pct,
                        fib_dca_last_fill_ms=setup_obj.fib_dca_last_fill_ms,
                    )
                )
                funnel[f"continuation_{htf.lower()}_prepare_created"] += 1
                if events_out is not None:
                    events_out.append(
                        {
                            "kind": "PREPARE",
                            "setup_id": setup_obj.id,
                            "symbol": symbol,
                            "setup_type": setup_obj.type,
                            "direction": setup_obj.direction,
                            "htf": htf,
                            "bar_open_ms": int(
                                event.payload.get(
                                    "touch_open_ms",
                                    df_htf.iloc[-1]["open_time"],
                                )
                            ),
                            "origin_price": setup_obj.origin_price,
                            "ote_low": setup_obj.ote_low,
                            "ote_high": setup_obj.ote_high,
                            "invalidation_price": setup_obj.invalidation_price,
                            "is_liberal": setup_obj.is_liberal,
                            "structure_kind": event.payload.get("structure_kind"),
                            "structure_swing_open_ms": event.payload.get(
                                "structure_swing_open_ms"
                            ),
                            "structure_broken_open_ms": event.payload.get(
                                "structure_broken_open_ms"
                            ),
                            "impulse_leg_start_open_ms": event.payload.get(
                                "impulse_leg_start_open_ms"
                            ),
                            "impulse_leg_end_open_ms": event.payload.get(
                                "impulse_leg_end_open_ms"
                            ),
                            "structure_break_key": event.payload.get(
                                "structure_break_key"
                            ),
                            "entry_mode": setup_obj.entry_mode,
                            "fib_dca_levels": event.payload.get("fib_dca_levels"),
                        }
                    )

        htf_breaks_cache: dict[str, list[Any]] = {}
        for setup in setups:
            if setup.state != "ARMED":
                continue
            if ts >= setup.expires_time:
                setup.state = "EXPIRED"
                funnel["setup_expired"] += 1
                continue

            htf_df = series.get(setup.htf)
            if htf_df is not None and not htf_df.empty:
                breaks = htf_breaks_cache.get(setup.htf)
                if breaks is None:
                    swing = _swing_size_for_htf(cfg, setup.htf)
                    breaks = extract_structure_breaks_htf(
                        htf_df,
                        swing_size=swing,
                        use_close=cfg.pivots.bos_use_close,
                        impulse_lock=True,
                    )
                    htf_breaks_cache[setup.htf] = breaks
                decision = decide_setup_structure_transition(
                    breaks=breaks,
                    df=htf_df,
                    setup_direction=setup.direction,
                    since_open_ms=prepare_since_open_ms(setup),
                )
                raw_action = decision.action
                decision = apply_reset_after_first_entry_policy(
                    decision=decision,
                    entry_count=int(setup.entry_count or 0),
                )
                if raw_action == "RESET_SAME_DIRECTION" and decision.action == "KEEP":
                    funnel["setup_reset_same_direction_skipped_before_first_entry"] += 1
                if decision.action == "INVALIDATE_OPPOSITE":
                    keep_fib_dca = (
                        str(setup.entry_mode).lower() == "fib_dca"
                        and not cfg.entry.fib_dca.cancel_remaining_on_opposite_structure
                    )
                    if keep_fib_dca:
                        funnel["fib_dca_kept_after_opposite_structure"] += 1
                    else:
                        setup.state = "INVALIDATED"
                        funnel["setup_invalidated_by_opposite_structure"] += 1
                        funnel[
                            f"setup_invalidated_by_opposite_structure_{setup.htf.lower()}"
                        ] += 1
                        continue
                if decision.action == "RESET_SAME_DIRECTION":
                    setup.state = "INVALIDATED"
                    funnel["setup_reset_by_new_structure_same_direction"] += 1
                    funnel[f"setup_reset_by_new_structure_same_direction_{setup.htf.lower()}"] += 1
                    continue

            inv_result = check_price_invalidation(
                setup=setup,
                series=series,
                entry=cfg.entry,
            )
            if inv_result.invalidated:
                setup.state = "INVALIDATED"
                funnel["setup_invalidated_before_entry"] += 1
                funnel[f"setup_invalidated_on_{inv_result.inv_tf.lower()}"] += 1
                inv_row = inv_result.row
                if events_out is not None and inv_row is not None:
                    events_out.append(
                        {
                            "kind": "INVALIDATED",
                            "setup_id": setup.id,
                            "symbol": symbol,
                            "setup_type": setup.setup_type,
                            "direction": setup.direction,
                            "htf": inv_result.inv_tf,
                            "bar_open_ms": int(inv_row["open_time"]),
                            "origin_price": setup.invalidation_price,
                            "ote_low": setup.ote_low,
                            "ote_high": setup.ote_high,
                            "invalidation_price": setup.invalidation_price,
                            "is_liberal": setup.is_liberal,
                        }
                    )
                continue

            if str(setup.entry_mode).lower() == "fib_dca":
                if _has_open_position(open_trades, fib_positions, setup_id=setup.id):
                    funnel["entry_skipped_open_position"] += 1
                    continue
                plan = deserialize_plan(setup.fib_dca_plan_json)
                monitor_tf = str(
                    cfg.entry.fib_dca.monitoring_tf_by_htf.get(setup.htf, setup.htf)
                )
                monitor_df = series.get(monitor_tf)
                if (
                    not plan
                    or monitor_tf not in closed_now
                    or monitor_df is None
                    or monitor_df.empty
                ):
                    continue
                row = monitor_df.iloc[-1]
                bar_open_ms = int(row["open_time"])
                monitor_invalidated = (
                    float(row["low"]) <= float(setup.invalidation_price)
                    if setup.direction == "LONG"
                    else float(row["high"]) >= float(setup.invalidation_price)
                )
                if monitor_invalidated:
                    setup.state = "INVALIDATED"
                    funnel["fib_dca_invalidated_on_monitor_tf"] += 1
                    _append_invalidated_event(
                        events_out,
                        setup_id=setup.id,
                        symbol=symbol,
                        setup_type=setup.setup_type,
                        direction=setup.direction,
                        timeframe=monitor_tf,
                        bar_open_ms=bar_open_ms,
                        invalidation_price=float(setup.invalidation_price),
                    )
                    continue
                filled = deserialize_filled_fibs(setup.fib_dca_filled_json)
                fills = new_fib_dca_fills(
                    direction=setup.direction,
                    plan=plan,
                    filled_fibs=filled,
                    price_low=float(row["low"]),
                    price_high=float(row["high"]),
                )
                initial_fills = initial_trigger_fills(
                    plan=plan,
                    filled_fibs=filled,
                    trigger_price=float(setup.ote_low),
                )
                fills = list(
                    {level.fib: level for level in [*initial_fills, *fills]}.values()
                )
                for level in fills:
                    filled.add(level.fib)
                    setup.fib_dca_filled_json = serialize_filled_fibs(filled)
                    setup.fib_dca_average_entry = weighted_average_entry(plan, filled)
                    setup.fib_dca_filled_weight_pct = filled_weight_pct(plan, filled)
                    setup.fib_dca_last_fill_ms = bar_open_ms
                    setup.entry_count = len(filled)
                    position = fib_positions.get(setup.id)
                    if position is None:
                        position = FibOpenPosition(
                            setup_id=setup.id,
                            symbol=symbol,
                            setup_type=setup.setup_type,
                            direction=setup.direction,
                            tf=monitor_tf,
                            entry_time=bar_open_ms,
                            plan=plan,
                            filled_fibs=set(),
                            sl=float(setup.invalidation_price),
                            tp=float(setup.entry_target_price),
                        )
                        fib_positions[setup.id] = position
                    position.filled_fibs.add(level.fib)
                    funnel["fib_dca_entry_opened"] += 1
                    funnel[f"fib_dca_entry_{level.fib:g}"] += 1
                    if events_out is not None:
                        events_out.append(
                            {
                                "kind": "ENTRY",
                                "setup_id": setup.id,
                                "symbol": symbol,
                                "setup_type": setup.setup_type,
                                "direction": setup.direction,
                                "htf": monitor_tf,
                                "entry_ltf": monitor_tf,
                                "setup_htf": setup.htf,
                                "bar_open_ms": bar_open_ms,
                                "entry": level.price,
                                "entry_mode": "fib_dca",
                                "fib": level.fib,
                                "weight_pct": level.weight_pct,
                                "filled_weight_pct": setup.fib_dca_filled_weight_pct,
                                "average_entry": setup.fib_dca_average_entry,
                                "recommended_stop": setup.invalidation_price,
                                "target_price": setup.entry_target_price,
                                "invalidation_price": setup.invalidation_price,
                            }
                        )
                if (
                    not filled
                    and cfg.entry.fib_dca.cancel_remaining_on_target
                    and bar_open_ms > int(prepare_since_open_ms(setup))
                ):
                    target_reached = (
                        float(row["high"]) >= float(setup.entry_target_price)
                        if setup.direction == "LONG"
                        else float(row["low"]) <= float(setup.entry_target_price)
                    )
                    if target_reached:
                        setup.state = "CONFIRMED"
                        funnel["fib_dca_target_reached_without_fill"] += 1
                continue

            lib = cfg.paper_mode.liberal
            if _has_open_position(open_trades, fib_positions):
                funnel["entry_skipped_open_position"] += 1
                continue
            ltf_result = resolve_ltf_confirmation(
                setup=setup,
                series=series,
                closed_tfs=closed_now,
                entry=cfg.entry,
                pivot_swing_by_tf=cfg.pivots.swing_size_by_tf,
                liberal_swing_override=lib.ltf_swing_length_override if lib.enabled else None,
                use_close=cfg.pivots.bos_use_close,
            )
            if ltf_result.cascade_update is not None:
                _apply_entry_cascade_update(setup, ltf_result.cascade_update)
                funnel["entry_cascade_state_updated"] += 1
            if ltf_result.advanced_update is not None:
                _apply_advanced_entry_update(setup, ltf_result.advanced_update)
                funnel["entry_advanced_state_updated"] += 1
            if ltf_result.status in {"NO_MATCHING_LTF", "LTF_NOT_CLOSED"}:
                continue

            used_tf = ltf_result.used_tf
            ltf_df = ltf_result.ltf_df
            row = ltf_result.row
            if used_tf is None or ltf_df is None or row is None:
                continue
            low = float(row["low"])
            high = float(row["high"])

            phase = setup.phase
            if phase == "WAIT_OTE":
                if not is_price_in_zone(
                    low,
                    high,
                    OteZone(low=setup.ote_low, high=setup.ote_high),
                ):
                    funnel["setup_waiting_ote_touch"] += 1
                    continue
                setup.phase = "WAIT_CHOCH"

            if ltf_result.status == "WAITING_CONFIRM":
                suffix = ltf_result.wait_suffix or "structure"
                funnel[f"setup_waiting_ltf_{suffix}"] += 1
                continue
            if ltf_result.status == "CASCADE_ADVANCED":
                choch = ltf_result.choch
                if choch is not None:
                    funnel[f"entry_cascade_{choch.kind.lower()}_{used_tf.lower()}"] += 1
                funnel[f"entry_cascade_advanced_{used_tf.lower()}"] += 1
                continue

            choch = ltf_result.choch
            if choch is None:
                continue
            funnel[f"entry_confirm_{choch.kind.lower()}_{used_tf.lower()}"] += 1
            bar_open_ms = int(row["open_time"])
            if setup.last_entry_bar_ms is not None and int(setup.last_entry_bar_ms) == bar_open_ms:
                funnel["entry_skipped_duplicate_bar"] += 1
                continue
            if int(setup.entry_count or 0) >= max_entries_per_setup:
                setup.state = "CONFIRMED"
                funnel["entry_limit_reached"] += 1
                continue
            if int(setup.entry_count or 0) > 0:
                if not reentry_has_new_structure_break(
                    confirm_broken_open_ms=choch.broken_open_ms,
                    last_entry_bar_ms=setup.last_entry_bar_ms,
                ):
                    funnel["entry_reentry_wait_new_structure_break"] += 1
                    continue
                if not reentry_swing_reset_reached(
                    ltf_df=ltf_df,
                    direction=setup.direction,
                    last_entry_bar_ms=setup.last_entry_bar_ms,
                    last_entry_swing_level=setup.last_entry_swing_level,
                ):
                    funnel["entry_reentry_wait_reset_swing"] += 1
                    continue

            entry = float(row["close"])
            simple_stop, simple_stop_source = recommended_entry_stop(
                entry=entry,
                direction=setup.direction,
                reset_level=choch.reset_level,
                invalidation_price=setup.invalidation_price,
            )
            recommended_stop = (
                float(ltf_result.recommended_stop)
                if ltf_result.recommended_stop is not None
                else simple_stop
            )
            if int(setup.entry_count or 0) > 0 and not reentry_price_improved(
                direction=setup.direction,
                entry_price=entry,
                last_entry_price=setup.last_entry_price,
            ):
                funnel["entry_reentry_wait_better_price"] += 1
                continue
            if cfg.entry.require_close_beyond_choch:
                level = float(choch.level)
                if not close_beyond_level(entry, level, setup.direction):
                    funnel["entry_rejected_close_not_beyond_level"] += 1
                    continue

            min_rr = (
                cfg.paper_mode.liberal.min_rr
                if setup.is_liberal and cfg.paper_mode.liberal.enabled
                else cfg.filters.min_rr
            )
            if ltf_result.recommended_stop is not None and ltf_result.target_price is not None:
                levels = (
                    {
                        "sl": recommended_stop,
                        "tp": float(ltf_result.target_price),
                        "tp1": float(ltf_result.target_price),
                    }
                    if cfg.entry.compute_sl_tp
                    else None
                )
                reject = None
            else:
                levels, reject = finalize_entry_levels(
                    entry=entry,
                    direction=setup.direction,
                    invalidation_price=float(setup.invalidation_price),
                    compute_sl_tp=cfg.entry.compute_sl_tp,
                    min_rr=min_rr,
                )
            if reject == "zero_risk":
                funnel["entry_rejected_zero_risk"] += 1
                continue
            if reject == "rr_below_min":
                funnel["entry_rejected_rr_below_min"] += 1
                continue

            sl: float | None = None
            tp: float | None = None
            if levels is not None:
                sl = float(levels["sl"])
                tp = float(levels["tp"])
            trade_sl = sl if sl is not None else recommended_stop
            trade_tp = (
                tp
                if tp is not None
                else float(ltf_result.target_price or setup.entry_target_price)
            )
            if abs(entry - trade_sl) > 0:
                open_trades.append(
                    OpenTrade(
                        setup_id=setup.id,
                        symbol=symbol,
                        setup_type=setup.setup_type,
                        direction=setup.direction,
                        tf=used_tf,
                        entry_time=bar_open_ms,
                        entry=entry,
                        sl=trade_sl,
                        tp=trade_tp,
                        risk=abs(entry - trade_sl),
                    )
                )
                funnel["entry_opened"] += 1
            else:
                funnel["entry_opened_signal_only"] += 1

            setup.entry_count = int(setup.entry_count or 0) + 1
            setup.last_entry_bar_ms = bar_open_ms
            setup.last_entry_price = entry
            setup.last_entry_swing_level = (
                float(choch.reset_level) if choch.reset_level is not None else None
            )
            # Keep the setup active only as the owner of the open position.
            # The global position lock prevents any re-entry until TP/SL.
            setup.state = "ARMED"
            funnel["entry_sent_keep_setup_armed"] += 1

            if events_out is not None:
                ev: dict[str, Any] = {
                    "kind": "ENTRY",
                    "setup_id": setup.id,
                    "symbol": symbol,
                    "setup_type": setup.setup_type,
                    "direction": setup.direction,
                    "htf": used_tf,
                    "entry_ltf": used_tf,
                    "setup_htf": setup.htf,
                    "ltf_expected": setup.ltf_expected,
                    "bar_open_ms": bar_open_ms,
                    "entry": entry,
                    "ote_low": setup.ote_low,
                    "ote_high": setup.ote_high,
                    "is_liberal": setup.is_liberal,
                    "entry_index": int(setup.entry_count),
                    "entries_max": int(max_entries_per_setup),
                    "confirm_kind": str(choch.kind),
                    "confirm_level": float(choch.level),
                    "confirm_bars_ago": int(choch.bars_ago),
                    "confirm_broken_open_ms": choch.broken_open_ms,
                    "confirm_reset_level": choch.reset_level,
                    "entry_mode": str(getattr(setup, "entry_mode", "simple")),
                    "recommended_stop": recommended_stop,
                    "recommended_stop_source": (
                        str(ltf_result.recommended_stop_source or "advanced")
                        if ltf_result.recommended_stop is not None
                        else simple_stop_source
                    ),
                }
                if ltf_result.target_price is not None:
                    ev["target_price"] = float(ltf_result.target_price)
                elif setup.entry_target_price is not None:
                    ev["target_price"] = float(setup.entry_target_price)
                if ltf_result.rr_to_target is not None:
                    ev["rr_to_target"] = float(ltf_result.rr_to_target)
                if sl is not None and tp is not None:
                    ev["sl"] = sl
                    ev["tp"] = tp
                    ev["tp1"] = tp
                events_out.append(ev)

        still_open: list[OpenTrade] = []
        for trade in open_trades:
            if trade.tf not in closed_now:
                still_open.append(trade)
                continue
            tf_df = series.get(trade.tf)
            if tf_df is None or tf_df.empty:
                still_open.append(trade)
                continue

            row = tf_df.iloc[-1]
            high = float(row["high"])
            low = float(row["low"])
            hit, exit_price, r_mult, reason = _resolve_trade_exit(trade=trade, high=high, low=low)
            if not hit:
                still_open.append(trade)
                continue

            closed_trades.append(
                ClosedTrade(
                    setup_id=trade.setup_id,
                    symbol=trade.symbol,
                    setup_type=trade.setup_type,
                    direction=trade.direction,
                    tf=trade.tf,
                    entry_time=trade.entry_time,
                    exit_time=int(row["open_time"]),
                    entry=trade.entry,
                    exit=exit_price,
                    r_multiple=r_mult,
                    exit_reason=reason,
                )
            )
            for setup in setups:
                if setup.id == trade.setup_id and setup.state == "ARMED":
                    setup.state = "INVALIDATED" if r_mult < 0 else "CONFIRMED"
                    break
            if r_mult < 0:
                _append_invalidated_event(
                    events_out,
                    setup_id=trade.setup_id,
                    symbol=trade.symbol,
                    setup_type=trade.setup_type,
                    direction=trade.direction,
                    timeframe=trade.tf,
                    bar_open_ms=int(row["open_time"]),
                    invalidation_price=trade.sl,
                )
            funnel[f"trade_closed_{reason}"] += 1

        open_trades = still_open

        for setup_id, position in list(fib_positions.items()):
            if position.tf not in closed_now:
                continue
            tf_df = series.get(position.tf)
            if tf_df is None or tf_df.empty:
                continue
            row = tf_df.iloc[-1]
            high = float(row["high"])
            low = float(row["low"])
            if position.direction == "LONG":
                hit_sl = low <= position.sl
                hit_tp = high >= position.tp
            else:
                hit_sl = high >= position.sl
                hit_tp = low <= position.tp
            if not hit_sl and not hit_tp:
                continue
            exit_price = position.sl if hit_sl else position.tp
            risk = planned_risk(position.plan, invalidation_price=position.sl)
            pnl = position_pnl(
                position.plan,
                filled_fibs=position.filled_fibs,
                direction=position.direction,
                exit_price=exit_price,
            )
            avg_entry = weighted_average_entry(position.plan, position.filled_fibs)
            closed_trades.append(
                ClosedTrade(
                    setup_id=position.setup_id,
                    symbol=position.symbol,
                    setup_type=position.setup_type,
                    direction=position.direction,
                    tf=position.tf,
                    entry_time=position.entry_time,
                    exit_time=int(row["open_time"]),
                    entry=float(avg_entry or 0.0),
                    exit=exit_price,
                    r_multiple=(pnl / risk) if risk > 0 else 0.0,
                    exit_reason=(
                        "both_hit_same_bar_sl_first"
                        if hit_sl and hit_tp
                        else ("sl" if hit_sl else "tp")
                    ),
                )
            )
            del fib_positions[setup_id]
            for setup in setups:
                if setup.id == setup_id and setup.state == "ARMED":
                    if hit_sl:
                        setup.state = "INVALIDATED"
                    elif cfg.entry.fib_dca.cancel_remaining_on_target:
                        setup.state = "CONFIRMED"
                    break
            if hit_sl:
                _append_invalidated_event(
                    events_out,
                    setup_id=position.setup_id,
                    symbol=position.symbol,
                    setup_type=position.setup_type,
                    direction=position.direction,
                    timeframe=position.tf,
                    bar_open_ms=int(row["open_time"]),
                    invalidation_price=position.sl,
                )
            funnel["fib_dca_trade_closed_sl" if hit_sl else "fib_dca_trade_closed_tp"] += 1

        if progress and (pos == len(timeline) or pos % 500 == 0):
            _print_replay_progress(
                pos,
                len(timeline),
                len(events_out) if events_out is not None else 0,
            )

    for trade in open_trades:
        tf_df = dfs[trade.tf]
        row = tf_df.iloc[-1]
        exit_price = float(row["close"])
        closed_trades.append(
            ClosedTrade(
                setup_id=trade.setup_id,
                symbol=trade.symbol,
                setup_type=trade.setup_type,
                direction=trade.direction,
                tf=trade.tf,
                entry_time=trade.entry_time,
                exit_time=int(row["open_time"]),
                entry=trade.entry,
                exit=exit_price,
                r_multiple=_calc_r(trade.direction, trade.entry, exit_price, trade.risk),
                exit_reason="eod_close",
            )
        )
        funnel["trade_closed_eod"] += 1

    for position in fib_positions.values():
        tf_df = dfs[position.tf]
        row = tf_df.iloc[-1]
        exit_price = float(row["close"])
        risk = planned_risk(position.plan, invalidation_price=position.sl)
        pnl = position_pnl(
            position.plan,
            filled_fibs=position.filled_fibs,
            direction=position.direction,
            exit_price=exit_price,
        )
        avg_entry = weighted_average_entry(position.plan, position.filled_fibs)
        closed_trades.append(
            ClosedTrade(
                setup_id=position.setup_id,
                symbol=position.symbol,
                setup_type=position.setup_type,
                direction=position.direction,
                tf=position.tf,
                entry_time=position.entry_time,
                exit_time=int(row["open_time"]),
                entry=float(avg_entry or 0.0),
                exit=exit_price,
                r_multiple=(pnl / risk) if risk > 0 else 0.0,
                exit_reason="eod_close",
            )
        )
        funnel["fib_dca_trade_closed_eod"] += 1

    if events_out is not None:
        if progress:
            _print_progress(f"Post-processing overlay events: {len(events_out)} raw")
        _dedupe_overlay_events(events_out)
        if progress:
            _print_progress(f"  dedupe: {len(events_out)}")
        _filter_stale_structure_events(events_out, dfs, cfg)
        if progress:
            _print_progress(f"  stale structure filter: {len(events_out)}")
        _normalize_structure_sequence(events_out)
        if progress:
            _print_progress(f"  structure sequence normalize: {len(events_out)}")
        _filter_invalidated_impulses(events_out, dfs)
        if progress:
            _print_progress(f"  invalidated impulse filter: {len(events_out)}")
        _collapse_impulse_fanout_by_start(events_out)
        if progress:
            _print_progress(f"  impulse fanout collapse: {len(events_out)}")
        _keep_single_retrace_pivot_per_leg(events_out, dfs, cfg)
        if progress:
            _print_progress(f"  retrace pivot filter: {len(events_out)}")

    summary = _summarize(symbol=symbol, mode=mode, closed_trades=closed_trades)
    if not quiet:
        _print_report(
            summary=summary,
            closed_trades=closed_trades,
            funnel=funnel,
            top_reasons=top_reasons,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward replay: PREPARE -> ENTRY -> TP/SL on Bybit history",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Bybit linear symbol, e.g. BTCUSDT")
    parser.add_argument(
        "--mode",
        default="both",
        choices=("reversal", "continuation", "both"),
        help="Какие сетапы симулировать",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Bars on the highest TF; lower TF limits are auto-expanded (with safety cap)",
    )
    parser.add_argument(
        "--top-reasons",
        type=int,
        default=15,
        help="Сколько причин воронки показывать в отчёте",
    )
    parser.add_argument(
        "--focus-htf",
        choices=("4H", "1H", "15M"),
        default=None,
        help="Ограничить replay одним HTF, как делает Pine export с --tf",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Показывать прогресс загрузки и replay",
    )
    parser.add_argument(
        "--max-expanded-bars-per-tf",
        type=int,
        default=None,
        help="Переопределить cap авторасширения младших TF для replay",
    )
    args = parser.parse_args()

    config_path = _find_config_path()
    asyncio.run(
        run_history_replay(
            symbol=args.symbol,
            mode=args.mode,
            limit=args.limit,
            top_reasons=args.top_reasons,
            config_path=config_path,
            focus_htf=args.focus_htf,
            progress=args.progress,
            max_expanded_bars_per_tf=args.max_expanded_bars_per_tf,
        )
    )


if __name__ == "__main__":
    main()
