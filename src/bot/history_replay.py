from __future__ import annotations

import argparse
import asyncio
import logging
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
    finest_closed_ltf,
    invalidation_tf_for_setup,
    ltf_expected_for_htf,
    prepare_since_open_ms,
    try_entry_confirm,
)
from bot.analyzer.filters import atr_percent, close_beyond_level, finalize_entry_levels
from bot.analyzer.reversal import detect_reversal_prepare
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
    impulse_leg_anchor_idxs,
    opposite_structure_break_since_open_ms,
    pivot_label_for_htf_display,
)

logger = logging.getLogger(__name__)

TF_MS: dict[str, int] = {
    "5M": 5 * 60 * 1000,
    "15M": 15 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}


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
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    needed = {"5M", "15M", "1H"}
    if mode in {"reversal", "both", "continuation"}:
        needed.add("4H")

    replay_targets: list[str] = []
    if mode in {"reversal", "both"}:
        replay_targets.append("reversal")
    if mode in {"continuation", "both"}:
        replay_targets.append("continuation")

    return tuple(sorted(needed, key=lambda tf: TF_MS[tf])), tuple(replay_targets)


def _armed_setup_ids(setups: list[ReplaySetup]) -> set[str]:
    return {s.id for s in setups if s.state == "ARMED"}


def _armed_dedup_keys(setups: list[ReplaySetup]) -> set[tuple[str, str, str, str]]:
    """Уникальный ключ активного сетапа: symbol + type + htf + direction."""
    return {
        (s.symbol, s.setup_type, s.htf, s.direction) for s in setups if s.state == "ARMED"
    }


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
    for br in visible_breaks:
        if br.broken_idx != last_pos:
            continue
        try:
            swing_open_ms = int(df.iloc[br.swing_idx]["open_time"])
            break_open_ms = int(df.iloc[br.broken_idx]["open_time"])
        except (KeyError, IndexError):
            continue
        events_out.append(
            {
                "kind": "STRUCTURE",
                "symbol": symbol,
                "htf": htf,
                "subkind": br.kind,
                "direction": br.direction,
                "level": float(br.swing_price),
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
    """
    piv_by_tf: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    struct_by_tf: dict[str, list[dict[str, Any]]] = {}
    for i, ev in enumerate(events):
        kind = str(ev.get("kind") or "")
        tf = str(ev.get("htf") or "")
        if not tf:
            continue
        if kind == "PIVOT":
            piv_by_tf.setdefault(tf, []).append((i, ev))
        elif kind == "STRUCTURE":
            struct_by_tf.setdefault(tf, []).append(ev)

    keep_pivot_idx: set[int] = set()
    for tf, pivots in piv_by_tf.items():
        structures = sorted(
            struct_by_tf.get(tf, []),
            key=lambda e: int(e.get("bar_open_ms") or 0),
        )
        if not structures:
            keep_pivot_idx.update(i for i, _ in pivots)
            continue

        first_struct_ms = int(structures[0].get("bar_open_ms") or 0)
        for i, ev in pivots:
            if int(ev.get("bar_open_ms") or 0) <= first_struct_ms:
                keep_pivot_idx.add(i)

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
                    key=lambda x: (float(x[1].get("price") or 0.0), -int(x[1].get("bar_open_ms") or 0)),
                )
            else:
                best = max(
                    candidates,
                    key=lambda x: (float(x[1].get("price") or 0.0), int(x[1].get("bar_open_ms") or 0)),
                )
            keep_pivot_idx.add(best[0])

    out: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        if ev.get("kind") != "PIVOT" or i in keep_pivot_idx:
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
) -> None:
    if not quiet:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_bot_config(config_path)
    env = EnvConfig()
    client = BybitClient(
        category=cfg.exchange.category,
        api_key=env.bybit_api_key,
        api_secret=env.bybit_api_secret,
    )

    needed_tfs, replay_targets = _load_timeframes(mode)

    tasks = [client.fetch_klines(symbol=symbol, timeframe=tf, limit=limit) for tf in needed_tfs]
    candles_by_tf = await asyncio.gather(*tasks)
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
    closed_trades: list[ClosedTrade] = []
    funnel: Counter[str] = Counter()
    continuation_prepare_state = ContinuationPrepareState()

    latest_4h_df: Any = None

    for ts in timeline:
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
                _emit_fresh_pivot_events(
                    df=series[tf],
                    htf=tf,
                    symbol=symbol,
                    swing_size=_swing_size_for_htf(cfg, tf),
                    bos_use_close=cfg.pivots.bos_use_close,
                    events_out=events_out,
                )

        prepare_htfs = cfg.prepare_htfs()
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
                    )
                    liberal_wider_choch = setup_obj is not None
                if setup_obj is None or event is None:
                    funnel["reversal_no_prepare_candidate"] += 1
                else:
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
                )
                if setup_obj is None or event is None:
                    funnel[f"continuation_{htf.lower()}_no_prepare_candidate"] += 1
                    continue

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
                opposite = opposite_structure_break_since_open_ms(
                    breaks,
                    htf_df,
                    setup_direction=setup.direction,
                    since_open_ms=prepare_since_open_ms(setup),
                )
                if opposite is not None:
                    setup.state = "INVALIDATED"
                    funnel["setup_invalidated_by_opposite_structure"] += 1
                    funnel[f"setup_invalidated_by_opposite_structure_{setup.htf.lower()}"] += 1
                    continue

            series_keys = set(series.keys())
            inv_tf = invalidation_tf_for_setup(
                setup.htf,
                setup.ltf_expected,
                cfg.entry,
                series_keys,
            )
            inv_df = series.get(inv_tf)
            if inv_df is not None and not inv_df.empty:
                inv_row = inv_df.iloc[-1]
                inv_low = float(inv_row["low"])
                inv_high = float(inv_row["high"])
                if setup.direction == "LONG" and inv_low <= setup.invalidation_price:
                    setup.state = "INVALIDATED"
                    funnel["setup_invalidated_before_entry"] += 1
                    funnel[f"setup_invalidated_on_{inv_tf.lower()}"] += 1
                    if events_out is not None:
                        events_out.append(
                            {
                                "kind": "INVALIDATED",
                                "setup_id": setup.id,
                                "symbol": symbol,
                                "setup_type": setup.setup_type,
                                "direction": setup.direction,
                                "htf": inv_tf,
                                "bar_open_ms": int(inv_row["open_time"]),
                                "origin_price": setup.invalidation_price,
                                "ote_low": setup.ote_low,
                                "ote_high": setup.ote_high,
                                "invalidation_price": setup.invalidation_price,
                                "is_liberal": setup.is_liberal,
                            }
                        )
                    continue
                if setup.direction == "SHORT" and inv_high >= setup.invalidation_price:
                    setup.state = "INVALIDATED"
                    funnel["setup_invalidated_before_entry"] += 1
                    funnel[f"setup_invalidated_on_{inv_tf.lower()}"] += 1
                    if events_out is not None:
                        events_out.append(
                            {
                                "kind": "INVALIDATED",
                                "setup_id": setup.id,
                                "symbol": symbol,
                                "setup_type": setup.setup_type,
                                "direction": setup.direction,
                                "htf": inv_tf,
                                "bar_open_ms": int(inv_row["open_time"]),
                                "origin_price": setup.invalidation_price,
                                "ote_low": setup.ote_low,
                                "ote_high": setup.ote_high,
                                "invalidation_price": setup.invalidation_price,
                                "is_liberal": setup.is_liberal,
                            }
                        )
                    continue

            used_tf = finest_closed_ltf(
                setup.ltf_expected,
                closed_tfs=closed_now,
                available=series_keys,
            )
            if used_tf is None:
                continue
            ltf_df = series.get(used_tf)
            if ltf_df is None or ltf_df.empty:
                continue

            row = ltf_df.iloc[-1]
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

            lib = cfg.paper_mode.liberal
            ok, choch = try_entry_confirm(
                entry=cfg.entry,
                ltf_df=ltf_df,
                used_tf=used_tf,
                setup=setup,
                pivot_swing_by_tf=cfg.pivots.swing_size_by_tf,
                liberal_swing_override=lib.ltf_swing_length_override if lib.enabled else None,
                is_liberal=setup.is_liberal,
                use_close=cfg.pivots.bos_use_close,
            )
            if not ok or choch is None:
                suffix = (
                    "directional_close"
                    if cfg.entry.confirm_mode == "directional_close"
                    else "structure"
                )
                funnel[f"setup_waiting_ltf_{suffix}"] += 1
                continue
            funnel[f"entry_confirm_{choch.kind.lower()}_{used_tf.lower()}"] += 1

            setup.state = "CONFIRMED"

            entry = float(row["close"])
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
                open_trades.append(
                    OpenTrade(
                        setup_id=setup.id,
                        symbol=symbol,
                        setup_type=setup.setup_type,
                        direction=setup.direction,
                        tf=used_tf,
                        entry_time=int(row["open_time"]),
                        entry=entry,
                        sl=sl,
                        tp=tp,
                        risk=abs(entry - sl),
                    )
                )
                funnel["entry_opened"] += 1
            else:
                funnel["entry_opened_signal_only"] += 1

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
                    "bar_open_ms": int(row["open_time"]),
                    "entry": entry,
                    "ote_low": setup.ote_low,
                    "ote_high": setup.ote_high,
                    "is_liberal": setup.is_liberal,
                }
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
            funnel[f"trade_closed_{reason}"] += 1

        open_trades = still_open

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

    if events_out is not None:
        _dedupe_overlay_events(events_out)
        _normalize_structure_sequence(events_out)
        _filter_invalidated_impulses(events_out, dfs)
        _keep_single_retrace_pivot_per_leg(events_out, dfs, cfg)

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
    parser.add_argument("--limit", type=int, default=1000, help="Candles per timeframe (max 1000)")
    parser.add_argument(
        "--top-reasons",
        type=int,
        default=15,
        help="Сколько причин воронки показывать в отчёте",
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
        )
    )


if __name__ == "__main__":
    main()
