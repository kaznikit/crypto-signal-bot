"""
Прогон «на истории»: walk-forward по закрытым барам 4H.
На каждом баре смотрим, появилось ли НА этом баре новое CHoCH событие
(Pine-style — пересечение активного prevHigh/Low close'ом), и сколько из них
прошли те же гейты, что у живого бота для PREPARE-разворота.

Запуск из каталога crypto-signal-bot:
  python -m bot.history_backtest --symbol BTCUSDT --limit 500

Нужен сетевой доступ к Bybit (публичные klines, ключи не обязательны).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from bot.analyzer.filters import atr_percent
from bot.analyzer.entry_ltf import ltf_expected_for_htf
from bot.analyzer.reversal import detect_reversal_prepare
from bot.analyzer.strategy_gates import evaluate_reversal_prepare
from bot.config import EnvConfig, load_bot_config
from bot.exchange.bybit_client import BybitClient
from bot.market.candles import candles_to_df
from bot.market.pivots import extract_structure_breaks

logger = logging.getLogger(__name__)


def _find_config_path() -> Path:
    cwd = Path.cwd()
    for candidate in (cwd / "config.yaml", Path(__file__).resolve().parents[2] / "config.yaml"):
        if candidate.exists():
            return candidate
    msg = "Не найден config.yaml (запускайте из каталога crypto-signal-bot)."
    raise SystemExit(msg)


def _tv_link(symbol: str, tf: str, exchange: str = "BYBIT") -> str:
    interval_map = {"5M": "5", "15M": "15", "1H": "60", "4H": "240"}
    interval = interval_map.get(tf, "240")
    return f"https://www.tradingview.com/chart/?symbol={exchange}:{symbol}&interval={interval}"


async def run_reversal_probe(
    *,
    symbol: str,
    limit: int,
    swing_size: int,
    max_bars_ago: int | None,
    config_path: Path,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_bot_config(config_path)
    env = EnvConfig()
    mb = cfg.reversal.choch_lookback_bars if max_bars_ago is None else max_bars_ago
    bos_use_close = cfg.pivots.bos_use_close
    client = BybitClient(
        category=cfg.exchange.category,
        api_key=env.bybit_api_key,
        api_secret=env.bybit_api_secret,
    )
    candles = await client.fetch_klines(symbol=symbol, timeframe="4H", limit=limit)
    df = candles_to_df(candles)
    if df.empty:
        raise SystemExit("Пустые свечи")

    min_bars = max(swing_size * 3, 150)
    new_choch = 0
    passed_atr = 0
    prepare_raw = 0
    prepare_after_gates = 0
    fired_samples: list[str] = []

    def _count_chochs(df_slice) -> int:
        breaks = extract_structure_breaks(df_slice, swing_size=swing_size, use_close=bos_use_close)
        return sum(1 for b in breaks if b.kind == "CHOCH")

    prev_choch_count = _count_chochs(df.iloc[:min_bars])

    for i in range(min_bars, len(df)):
        sub = df.iloc[: i + 1]
        cur_choch_count = _count_chochs(sub)
        is_new_event = cur_choch_count > prev_choch_count
        prev_choch_count = cur_choch_count
        if not is_new_event:
            continue
        new_choch += 1

        if atr_percent(sub) < cfg.filters.min_atr_pct:
            continue
        passed_atr += 1
        close_time = int(sub.iloc[-1]["open_time"])
        ttl_hours = cfg.reversal.ttl_bars_4h * 4
        setup, event = detect_reversal_prepare(
            symbol=symbol,
            htf_df=sub,
            close_time=close_time,
            ttl_hours=ttl_hours,
            swing_size=swing_size,
            max_bars_ago_choch=mb,
            impulse_max_age_bars=cfg.pivots.impulse_max_age_bars,
            bos_use_close=bos_use_close,
            ltf_expected=ltf_expected_for_htf("4H", cfg.entry),
        )
        if setup is None or event is None:
            continue
        prepare_raw += 1
        ok, score = evaluate_reversal_prepare(
            df=sub,
            choch_direction=setup.direction,
            setup=setup,
            event=event,
            features=cfg.strategy_features,
        )
        if ok:
            prepare_after_gates += 1
            fired_samples.append(
                f"#{i} bar_open={int(sub.iloc[-1]['open_time'])} dir={setup.direction} "
                f"level={event.payload.get('prepare_trigger_level'):.4f} score={score}"
            )

    print("=== Reversal 4H walk-forward (probe) ===")
    print(f"Symbol: {symbol}, bars: {len(df)}, swing_size: {swing_size}")
    print(f"1) New CHoCH events (fresh structural flips): {new_choch}")
    print(f"2) Passed ATR + fresh-CHoCH window: {passed_atr}")
    print(f"3) PREPARE candidates (impulse + OTE built): {prepare_raw}")
    print(f"4) PREPARE after strategy_features gates:  {prepare_after_gates}")
    if fired_samples:
        print("\nПримеры (макс. 10):")
        for line in fired_samples[:10]:
            print(f"  {line}")
    print(f"\nTradingView chart: {_tv_link(symbol, '4H')}")
    print("(ENTRY / FSM здесь не симулируются — только верхняя воронка PREPARE.)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward probe: reversal PREPARE on 4H history",
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="Bybit linear symbol, e.g. BTCUSDT")
    parser.add_argument("--limit", type=int, default=500, help="Number of 4H candles")
    parser.add_argument(
        "--swing-size",
        type=int,
        default=15,
        help="Pivot swing size (left/right bars) — Pine ta.pivothigh/pivotlow length",
    )
    parser.add_argument(
        "--max-bars-ago",
        type=int,
        default=None,
        help="Макс. баров назад для CHoCH (по умолчанию reversal.choch_lookback_bars из config.yaml)",
    )
    args = parser.parse_args()
    config_path = _find_config_path()
    asyncio.run(
        run_reversal_probe(
            symbol=args.symbol,
            limit=args.limit,
            swing_size=args.swing_size,
            max_bars_ago=args.max_bars_ago,
            config_path=config_path,
        )
    )


if __name__ == "__main__":
    main()
