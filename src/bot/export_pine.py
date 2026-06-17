"""CLI: выгрузка сигналов в Pine Script для TradingView.

Два источника данных:
  - `--from-replay` — прогоняет историю Bybit через ту же логику, что и live-бот,
    собирает PREPARE или ENTRY/TAKE_PROFIT/STOP_LOSS и сразу выгружает их в Pine
    (не зависит от состояния `bot.db`);
  - по умолчанию — читает уже отправленные сигналы из SQLite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot.config import EnvConfig, find_bot_config_source
from bot.history_replay import run_history_replay
from bot.storage.repo import Repository


def _find_config_dir() -> Path:
    cwd = Path.cwd()
    for candidate in (cwd, Path(__file__).resolve().parents[2]):
        if (candidate / "config").is_dir() or (candidate / "config.yaml").exists():
            return candidate
    return cwd


def _find_template() -> Path:
    pkg = Path(__file__).resolve().parent / "pine_data" / "signal_bot_overlay.pine.tmpl"
    if pkg.exists():
        return pkg
    root = _find_config_dir() / "pine" / "signal_bot_overlay.pine.tmpl"
    if root.exists():
        return root
    msg = f"Pine template not found (expected {pkg} or {root})"
    raise SystemExit(msg)


def _find_config_path() -> Path:
    return find_bot_config_source()


def _parse_since(s: str | None) -> int | None:
    if not s:
        return None
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _pine_float(v: float | None) -> str:
    return "na" if v is None else f"{float(v)}"


def _pine_str(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _marker_label_count(
    kind: str,
    *,
    impulse_start_time: int,
    impulse_start_price: float | None,
    impulse_end_time: int,
    impulse_end_price: float | None,
) -> int:
    count = 1
    if kind == "PREPARE":
        if impulse_start_time > 0 and impulse_start_price is not None:
            count += 1
        if impulse_end_time > 0 and impulse_end_price is not None:
            count += 1
    return count


def _label_budget_slice_start(
    *,
    kinds_out: list[str],
    impulse_start_times: list[int],
    impulse_start_prices: list[float | None],
    impulse_end_times: list[int],
    impulse_end_prices: list[float | None],
    max_labels: int,
) -> tuple[int, int, int]:
    label_counts = [
        _marker_label_count(
            kind,
            impulse_start_time=impulse_start_times[i],
            impulse_start_price=impulse_start_prices[i],
            impulse_end_time=impulse_end_times[i],
            impulse_end_price=impulse_end_prices[i],
        )
        for i, kind in enumerate(kinds_out)
    ]
    total_labels = sum(label_counts)
    if max_labels <= 0 or total_labels <= max_labels:
        return 0, total_labels, total_labels

    kept_labels = 0
    start = len(label_counts)
    for i in range(len(label_counts) - 1, -1, -1):
        next_count = label_counts[i]
        if kept_labels + next_count > max_labels:
            break
        kept_labels += next_count
        start = i
    return start, kept_labels, total_labels


def _suppress_start_anchors_overlapping_end_anchors(
    *,
    impulse_start_times: list[int],
    impulse_start_prices: list[float | None],
    impulse_end_times: list[int],
    impulse_end_prices: list[float | None],
) -> tuple[list[int], list[float | None]]:
    resolved_start_times = list(impulse_start_times)
    resolved_start_prices = list(impulse_start_prices)
    end_anchor_times = {
        int(t)
        for t, price in zip(impulse_end_times, impulse_end_prices, strict=False)
        if int(t) > 0 and price is not None
    }
    for i, (t, price) in enumerate(
        zip(resolved_start_times, resolved_start_prices, strict=False)
    ):
        if int(t) > 0 and price is not None and int(t) in end_anchor_times:
            resolved_start_times[i] = 0
            resolved_start_prices[i] = None
    return resolved_start_times, resolved_start_prices


def _render_pine(
    *,
    times: list[int],
    times2: list[int],
    prices: list[float],
    kinds_out: list[str],
    subkinds: list[str],
    dirs: list[str],
    ote_lo: list[float],
    ote_hi: list[float],
    sls: list[float | None],
    tps: list[float | None],
    impulse_start_times: list[int],
    impulse_end_times: list[int],
    impulse_start_prices: list[float | None],
    impulse_end_prices: list[float | None],
    out_path: Path,
    max_markers: int,
) -> None:
    if not times:
        msg = "No signals matched filters; nothing written."
        raise SystemExit(msg)
    impulse_start_times, impulse_start_prices = _suppress_start_anchors_overlapping_end_anchors(
        impulse_start_times=impulse_start_times,
        impulse_start_prices=impulse_start_prices,
        impulse_end_times=impulse_end_times,
        impulse_end_prices=impulse_end_prices,
    )
    total = len(times)
    if max_markers > 0:
        start, kept_labels, total_labels = _label_budget_slice_start(
            kinds_out=kinds_out,
            impulse_start_times=impulse_start_times,
            impulse_start_prices=impulse_start_prices,
            impulse_end_times=impulse_end_times,
            impulse_end_prices=impulse_end_prices,
            max_labels=max_markers,
        )
        if start > 0:
            times = times[start:]
            times2 = times2[start:]
            prices = prices[start:]
            kinds_out = kinds_out[start:]
            subkinds = subkinds[start:]
            dirs = dirs[start:]
            ote_lo = ote_lo[start:]
            ote_hi = ote_hi[start:]
            sls = sls[start:]
            tps = tps[start:]
            impulse_start_times = impulse_start_times[start:]
            impulse_end_times = impulse_end_times[start:]
            impulse_start_prices = impulse_start_prices[start:]
            impulse_end_prices = impulse_end_prices[start:]
            print(
                f"Capped to last {len(times)}/{total} markers "
                f"({kept_labels}/{total_labels} labels; TradingView Pine limit ~500)."
            )
    lines: list[str] = []
    indent = "    "
    for i, t_ms in enumerate(times):
        lines.append(f"{indent}array.push(sig_t, {int(t_ms)})")
        lines.append(f"{indent}array.push(sig_t2, {int(times2[i])})")
        lines.append(f"{indent}array.push(sig_price, {_pine_float(prices[i])})")
        lines.append(f"{indent}array.push(sig_kind, {_pine_str(kinds_out[i])})")
        lines.append(f"{indent}array.push(sig_subkind, {_pine_str(subkinds[i])})")
        lines.append(f"{indent}array.push(sig_dir, {_pine_str(dirs[i])})")
        lines.append(f"{indent}array.push(sig_ote_lo, {_pine_float(ote_lo[i])})")
        lines.append(f"{indent}array.push(sig_ote_hi, {_pine_float(ote_hi[i])})")
        lines.append(f"{indent}array.push(sig_sl, {_pine_float(sls[i])})")
        lines.append(f"{indent}array.push(sig_tp, {_pine_float(tps[i])})")
        lines.append(f"{indent}array.push(sig_imp_start_t, {int(impulse_start_times[i])})")
        lines.append(f"{indent}array.push(sig_imp_end_t, {int(impulse_end_times[i])})")
        lines.append(
            f"{indent}array.push(sig_imp_start_price, {_pine_float(impulse_start_prices[i])})"
        )
        lines.append(
            f"{indent}array.push(sig_imp_end_price, {_pine_float(impulse_end_prices[i])})"
        )
    push_block = "\n".join(lines)
    tpl = _find_template().read_text(encoding="utf-8")
    rendered = tpl.replace("{{PUSH_BLOCK}}", push_block)
    out_path.write_text(rendered, encoding="utf-8")
    counts: dict[str, int] = {}
    for k in kinds_out:
        counts[k] = counts.get(k, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"Wrote {len(times)} markers to {out_path} ({summary})")


def _collect_arrays(
    events: Iterable[dict[str, Any]],
    *,
    tf: str | None,
    since_ms: int | None,
    include_liberal: bool,
    kinds: set[str],
    symbol: str,
) -> dict[str, list]:
    times: list[int] = []
    times2: list[int] = []
    prices: list[float] = []
    kinds_out: list[str] = []
    subkinds: list[str] = []
    dirs: list[str] = []
    ote_lo: list[float] = []
    ote_hi: list[float] = []
    sls: list[float | None] = []
    tps: list[float | None] = []
    impulse_start_times: list[int] = []
    impulse_end_times: list[int] = []
    impulse_start_prices: list[float | None] = []
    impulse_end_prices: list[float | None] = []

    for ev in events:
        kind = str(ev.get("kind", ""))
        display_kind = kind
        if kind == "INVALIDATED":
            if ev.get("after_entry") is not True:
                continue
            display_kind = "STOP_LOSS"
        if display_kind not in kinds:
            continue
        if ev.get("symbol") != symbol:
            continue
        if not include_liberal and ev.get("liberal", ev.get("is_liberal")):
            continue
        bar_ms = int(ev.get("bar_open_ms") or 0)
        if since_ms is not None and bar_ms and bar_ms < since_ms:
            continue
        if tf:
            row_tf = str(ev.get("htf") or "")
            setup_tf = str(ev.get("setup_htf") or "")
            if display_kind in {"ENTRY", "STOP_LOSS", "TAKE_PROFIT"}:
                if row_tf != tf and setup_tf != tf:
                    continue
            elif row_tf != tf:
                continue
        if not bar_ms:
            continue

        times.append(bar_ms)
        kinds_out.append(display_kind)
        subkinds.append(str(ev.get("subkind") or ""))
        dirs.append(str(ev.get("direction", "NA")))
        ote_lo.append(float(ev.get("ote_low") or 0))
        ote_hi.append(float(ev.get("ote_high") or 0))
        impulse_start_times.append(0)
        impulse_end_times.append(0)
        impulse_start_prices.append(None)
        impulse_end_prices.append(None)

        if kind == "PREPARE":
            prices.append(
                float(
                    ev.get("origin_price")
                    or ev.get("prepare_trigger_level")
                    or ev.get("mark_price")
                    or 0
                )
            )
            sls.append(None)
            tps.append(None)
            anchor_ms = int(ev.get("structure_broken_open_ms") or 0)
            times2.append(anchor_ms)
            impulse_start_times[-1] = _int_or_zero(
                ev.get("impulse_leg_start_open_ms")
                or ev.get("impulse_start_open_ms")
            )
            impulse_end_times[-1] = _int_or_zero(
                ev.get("impulse_leg_end_open_ms")
                or ev.get("impulse_end_open_ms")
            )
            impulse_start_prices[-1] = _float_or_none(
                ev.get("impulse_start_price") or ev.get("invalidation_price")
            )
            impulse_end_prices[-1] = _float_or_none(
                ev.get("impulse_end_price") or ev.get("target_price")
            )
        elif display_kind == "ENTRY":
            prices.append(float(ev.get("entry") or 0))
            sl = ev.get("sl")
            if sl is None:
                sl = ev.get("recommended_stop")
            sls.append(float(sl) if sl is not None else None)
            tp = ev.get("tp1")
            if tp is None:
                tp = ev.get("tp")
            if tp is None:
                tp = ev.get("target_price")
            tps.append(float(tp) if tp is not None else None)
            times2.append(0)
        elif display_kind in {"STOP_LOSS", "TAKE_PROFIT"}:
            prices.append(
                float(
                    ev.get("exit_price")
                    or ev.get("mark_price")
                    or ev.get("invalidation_price")
                    or 0
                )
            )
            sls.append(None)
            tps.append(None)
            times2.append(0)
            subkinds[-1] = "SL" if display_kind == "STOP_LOSS" else "TP"
        elif kind == "STRUCTURE":
            prices.append(float(ev.get("level") or 0))
            sls.append(None)
            tps.append(None)
            times2.append(int(ev.get("swing_open_ms") or 0))
        elif kind == "IMPULSE":
            # IMPULSE рисуем как диагональную ногу: (times[i], prices[i]) →
            # (times2[i], ote_lo[i]). prices[i] = старт ноги, ote_lo[i] = конец
            # (HH для LONG, LL для SHORT). times[i] держим как start_open_ms,
            # чтобы фильтр `--since` отсекал импульсы по началу ноги.
            times[-1] = int(ev.get("start_open_ms") or times[-1])
            prices.append(float(ev.get("start_price") or 0))
            sls.append(None)
            tps.append(None)
            times2.append(int(ev.get("end_open_ms") or 0))
            ote_lo[-1] = float(ev.get("end_price") or 0)
            ote_hi[-1] = 0.0
            impulse_start_times[-1] = _int_or_zero(ev.get("start_open_ms"))
            impulse_end_times[-1] = _int_or_zero(ev.get("end_open_ms"))
            impulse_start_prices[-1] = _float_or_none(ev.get("start_price"))
            impulse_end_prices[-1] = _float_or_none(ev.get("end_price"))
        elif kind == "PIVOT":
            # PIVOT-маркер = HH/LH/HL/LL-метка на пивот-баре. Pine-индикатор
            # Leviathan'а ставит label.style_label_down над HH/LH и _up под HL/LL.
            # subkind = текст метки (HH/LH/HL/LL), price = координата пивота.
            prices.append(float(ev.get("price") or 0))
            sls.append(None)
            tps.append(None)
            times2.append(0)
            subkinds[-1] = str(ev.get("label") or "")
            # dir не несёт смысла для пивота; используем pivot_kind для выбора стиля
            dirs[-1] = "HIGH" if str(ev.get("pivot_kind", "")) == "HIGH" else "LOW"
        else:
            prices.append(float(ev.get("mark_price") or ev.get("invalidation_price") or 0))
            sls.append(None)
            tps.append(None)
            times2.append(0)

    return {
        "times": times,
        "times2": times2,
        "prices": prices,
        "kinds": kinds_out,
        "subkinds": subkinds,
        "dirs": dirs,
        "ote_lo": ote_lo,
        "ote_hi": ote_hi,
        "sls": sls,
        "tps": tps,
        "impulse_start_times": impulse_start_times,
        "impulse_end_times": impulse_end_times,
        "impulse_start_prices": impulse_start_prices,
        "impulse_end_prices": impulse_end_prices,
    }


def run_export_from_db(
    *,
    symbol: str,
    tf: str | None,
    since_ms: int | None,
    kinds: tuple[str, ...],
    include_liberal: bool,
    out_path: Path,
    db_url: str,
    max_markers: int,
) -> None:
    repo = Repository(db_url)
    query_kinds = kinds
    if "STOP_LOSS" in kinds and "INVALIDATED" not in kinds:
        query_kinds = (*kinds, "INVALIDATED")
    rows = repo.load_signals_for_export(kinds=query_kinds)

    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload: dict[str, Any] = json.loads(row.payload_json)
        except json.JSONDecodeError:
            continue
        ev = dict(payload)
        ev["kind"] = row.kind
        if "bar_open_ms" not in ev:
            ev["bar_open_ms"] = int(row.sent_at.timestamp() * 1000)
        events.append(ev)

    arrs = _collect_arrays(
        events,
        tf=tf,
        since_ms=since_ms,
        include_liberal=include_liberal,
        kinds=set(kinds),
        symbol=symbol,
    )
    _render_pine(
        times=arrs["times"],
        times2=arrs["times2"],
        prices=arrs["prices"],
        kinds_out=arrs["kinds"],
        subkinds=arrs["subkinds"],
        dirs=arrs["dirs"],
        ote_lo=arrs["ote_lo"],
        ote_hi=arrs["ote_hi"],
        sls=arrs["sls"],
        tps=arrs["tps"],
        impulse_start_times=arrs["impulse_start_times"],
        impulse_end_times=arrs["impulse_end_times"],
        impulse_start_prices=arrs["impulse_start_prices"],
        impulse_end_prices=arrs["impulse_end_prices"],
        out_path=out_path,
        max_markers=max_markers,
    )


def run_export_from_replay(
    *,
    symbol: str,
    tf: str | None,
    since_ms: int | None,
    kinds: tuple[str, ...],
    include_liberal: bool,
    out_path: Path,
    config_path: Path,
    limit: int,
    mode: str,
    max_markers: int,
    max_expanded_bars_per_tf: int | None,
    variant: str,
    min_rr: float | None,
) -> None:
    focus_htf = tf if tf in {"4H", "1H", "15M"} else None
    events: list[dict[str, Any]] = []
    asyncio.run(
        run_history_replay(
            symbol=symbol,
            limit=limit,
            mode=mode,
            top_reasons=0,
            config_path=config_path,
            events_out=events,
            quiet=True,
            focus_htf=focus_htf,
            progress=True,
            overlay_tfs={tf} if tf else None,
            max_expanded_bars_per_tf=max_expanded_bars_per_tf,
            variant=variant,
            min_rr=min_rr,
        )
    )
    print(f"Replay produced {len(events)} events before filters")
    arrs = _collect_arrays(
        events,
        tf=tf,
        since_ms=since_ms,
        include_liberal=include_liberal,
        kinds=set(kinds),
        symbol=symbol,
    )
    _render_pine(
        times=arrs["times"],
        times2=arrs["times2"],
        prices=arrs["prices"],
        kinds_out=arrs["kinds"],
        subkinds=arrs["subkinds"],
        dirs=arrs["dirs"],
        ote_lo=arrs["ote_lo"],
        ote_hi=arrs["ote_hi"],
        sls=arrs["sls"],
        tps=arrs["tps"],
        impulse_start_times=arrs["impulse_start_times"],
        impulse_end_times=arrs["impulse_end_times"],
        impulse_start_prices=arrs["impulse_start_prices"],
        impulse_end_prices=arrs["impulse_end_prices"],
        out_path=out_path,
        max_markers=max_markers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Export bot signals to Pine Script overlay")
    parser.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    parser.add_argument("--tf", default=None, help="Filter by TF (4H, 1H, 15M, 5M, 1M)")
    parser.add_argument("--since", default=None, help="UTC date YYYY-MM-DD")
    parser.add_argument(
        "--kinds",
        default="ENTRY,TAKE_PROFIT,STOP_LOSS",
        help="Comma-separated signal kinds (PREPARE,ENTRY,TAKE_PROFIT,STOP_LOSS)",
    )
    parser.add_argument(
        "--include-liberal",
        action="store_true",
        help="Include liberal=true setups",
    )
    parser.add_argument("--out", type=Path, default=Path("signal_bot_overlay.pine"))
    parser.add_argument(
        "--max-markers",
        type=int,
        default=400,
        help=(
            "Сколько последних Pine-лейблов оставить "
            "(Pine v5 лимит ~500 на индикатор; 0 = без среза)"
        ),
    )
    parser.add_argument(
        "--from-replay",
        action="store_true",
        help="Запустить walk-forward по истории Bybit и взять события оттуда, "
        "минуя bot.db. Нужен сетевой доступ к Bybit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Базовый лимит replay на старшем TF (для --from-replay)",
    )
    parser.add_argument(
        "--mode",
        default="both",
        choices=("reversal", "continuation", "both"),
        help="Какие сетапы симулировать (только для --from-replay)",
    )
    parser.add_argument(
        "--max-expanded-bars-per-tf",
        type=int,
        default=None,
        help="Cap для авторасширения младших TF в replay/export",
    )
    parser.add_argument(
        "--variant",
        default="configured",
        choices=("configured", "A", "B", "C", "D", "E"),
        help="Strategy variant for --from-replay",
    )
    parser.add_argument(
        "--min-rr",
        type=float,
        default=None,
        help="Override minimum RR for --from-replay",
    )
    args = parser.parse_args()

    kinds_tuple = tuple(k.strip().upper() for k in args.kinds.split(",") if k.strip())
    since_ms = _parse_since(args.since)

    if args.from_replay:
        run_export_from_replay(
            symbol=args.symbol,
            tf=args.tf,
            since_ms=since_ms,
            kinds=kinds_tuple,
            include_liberal=args.include_liberal,
            out_path=args.out,
            config_path=_find_config_path(),
            limit=args.limit,
            mode=args.mode,
            max_markers=args.max_markers,
            max_expanded_bars_per_tf=args.max_expanded_bars_per_tf,
            variant=args.variant,
            min_rr=args.min_rr,
        )
        return

    env = EnvConfig()
    run_export_from_db(
        symbol=args.symbol,
        tf=args.tf,
        since_ms=since_ms,
        kinds=kinds_tuple,
        include_liberal=args.include_liberal,
        out_path=args.out,
        db_url=env.bot_db_url,
        max_markers=args.max_markers,
    )


if __name__ == "__main__":
    main()
