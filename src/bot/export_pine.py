"""CLI: выгрузка сигналов в Pine Script для TradingView.

Два источника данных:
  - `--from-replay` — прогоняет историю Bybit через ту же логику, что и live-бот,
    собирает PREPARE/ENTRY/INVALIDATED и сразу выгружает их в Pine
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

from bot.config import EnvConfig
from bot.history_replay import run_history_replay
from bot.storage.repo import Repository


def _find_config_dir() -> Path:
    cwd = Path.cwd()
    for candidate in (cwd, Path(__file__).resolve().parents[2]):
        if (candidate / "config.yaml").exists():
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
    p = _find_config_dir() / "config.yaml"
    if not p.exists():
        msg = "Не найден config.yaml (запускайте из каталога crypto-signal-bot)."
        raise SystemExit(msg)
    return p


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
    out_path: Path,
    max_markers: int,
) -> None:
    if not times:
        msg = "No signals matched filters; nothing written."
        raise SystemExit(msg)
    total = len(times)
    if max_markers > 0 and total > max_markers:
        keep = max_markers
        times = times[-keep:]
        times2 = times2[-keep:]
        prices = prices[-keep:]
        kinds_out = kinds_out[-keep:]
        subkinds = subkinds[-keep:]
        dirs = dirs[-keep:]
        ote_lo = ote_lo[-keep:]
        ote_hi = ote_hi[-keep:]
        sls = sls[-keep:]
        tps = tps[-keep:]
        print(f"Capped to last {keep}/{total} markers (TradingView Pine limit ~500 per indicator).")
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

    for ev in events:
        kind = str(ev.get("kind", ""))
        if kind not in kinds:
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
            if kind == "ENTRY":
                if row_tf != tf and setup_tf != tf:
                    continue
            elif row_tf != tf:
                continue
        if not bar_ms:
            continue

        times.append(bar_ms)
        kinds_out.append(kind)
        subkinds.append(str(ev.get("subkind") or ""))
        dirs.append(str(ev.get("direction", "NA")))
        ote_lo.append(float(ev.get("ote_low") or 0))
        ote_hi.append(float(ev.get("ote_high") or 0))

        if kind == "PREPARE":
            prices.append(float(ev.get("origin_price") or 0))
            sls.append(None)
            tps.append(None)
            anchor_ms = int(ev.get("structure_broken_open_ms") or 0)
            times2.append(anchor_ms)
        elif kind == "ENTRY":
            prices.append(float(ev.get("entry") or 0))
            sl = ev.get("sl")
            sls.append(float(sl) if sl is not None else None)
            tp = ev.get("tp1")
            if tp is None:
                tp = ev.get("tp")
            tps.append(float(tp) if tp is not None else None)
            times2.append(0)
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
    rows = repo.load_signals_for_export(kinds=kinds)

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
        default="PREPARE,ENTRY,INVALIDATED,STRUCTURE,IMPULSE,PIVOT",
        help="Comma-separated signal kinds (PREPARE,ENTRY,INVALIDATED,STRUCTURE,IMPULSE,PIVOT)",
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
        help="Сколько последних маркеров оставить (Pine v5 лимит ~500 на индикатор; 0 = без среза)",
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
    args = parser.parse_args()

    kinds_tuple = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
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
