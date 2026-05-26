from __future__ import annotations

import asyncio
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from bot.analyzer.continuation import detect_continuation_prepare
from bot.analyzer.entry_ltf import (
    invalidation_tf_for_setup,
    ltf_expected_for_htf,
    prepare_since_open_ms,
)
from bot.analyzer.filters import atr_percent, close_beyond_level, finalize_entry_levels
from bot.analyzer.reversal import detect_reversal_prepare
from bot.analyzer.setup_lifecycle import decide_setup_structure_transition
from bot.analyzer.setup_machine import tick_setup
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
from bot.market.pivots import extract_structure_breaks_htf
from bot.notify.telegram import TelegramNotifier
from bot.scheduler import TimeframeScheduler
from bot.storage.models import SignalKind
from bot.storage.repo import Repository
from bot.util.logging import setup_logging
from bot.util.time import utcnow

logger = logging.getLogger(__name__)


def _enrich_prepare_payload(
    payload: dict[str, Any],
    *,
    bar_open_ms: int,
    score: int,
    liberal: bool,
) -> None:
    payload["bar_open_ms"] = bar_open_ms
    payload["score"] = score
    payload["liberal"] = liberal


class SignalBotApp:
    def __init__(self) -> None:
        self._env = EnvConfig()
        self._cfg = load_bot_config(Path("config.yaml"))
        setup_logging(self._env.bot_log_level)
        self._repo = Repository(self._env.bot_db_url)
        self._repo.create_schema()
        self._bybit = BybitClient(
            category=self._cfg.exchange.category,
            api_key=self._env.bybit_api_key,
            api_secret=self._env.bybit_api_secret,
        )
        self._notifier = TelegramNotifier(
            bot_token=self._env.tg_bot_token,
            chat_id=self._env.tg_chat_id,
            paper_chat_id=self._env.tg_paper_chat_id,
        )
        self._scheduler = TimeframeScheduler()
        self._latest_4h_series: dict[str, Any] = {}

    def _invalidate_active_setups_for_key(
        self,
        key: tuple[str, str, str, str],
        *,
        armed_keys: set[tuple[str, str, str, str]],
        active_by_key: dict[tuple[str, str, str, str], list[Any]],
    ) -> int:
        """Сбрасывает ARMED setup'ы по dedup-ключу перед созданием нового."""
        existing = active_by_key.get(key, [])
        if not existing:
            armed_keys.discard(key)
            return 0
        now = utcnow()
        for setup in existing:
            self._repo.mark_setup_state(setup.id, "INVALIDATED", now)
        active_by_key[key] = []
        armed_keys.discard(key)
        return len(existing)

    async def run(self) -> None:
        self._scheduler.add_tick_job(self.tick)
        self._scheduler.start()
        while True:
            await asyncio.sleep(3600)

    async def tick(self) -> None:
        closed_tfs = self._scheduler.closed_timeframes()
        if not closed_tfs:
            return
        symbols = await self._bybit.list_top_symbols(
            quote=self._cfg.symbols.quote,
            count=self._cfg.symbols.count,
        )
        tick_funnel: Counter[str] = Counter()
        for symbol in symbols:
            tick_funnel.update(await self._process_symbol(symbol=symbol, closed_tfs=closed_tfs))
            await asyncio.sleep(0.03)
        if tick_funnel:
            logger.info(
                "Tick funnel summary | closed_tfs=%s | %s",
                ",".join(closed_tfs),
                dict(tick_funnel),
            )
        if utcnow().hour == 0 and utcnow().minute < 2:
            await self._notifier.send_heartbeat(paper_mode=self._cfg.paper_mode.enabled)

    async def _process_symbol(self, symbol: str, closed_tfs: list[str]) -> Counter[str]:
        funnel: Counter[str] = Counter()
        series: dict[str, Any] = {}
        df_4h_for_alignment = self._latest_4h_series.get(symbol)

        active_now = [s for s in self._repo.load_active_setups() if s.symbol == symbol]
        armed_keys: set[tuple[str, str, str, str]] = {
            (s.symbol, s.type, s.htf, s.direction) for s in active_now if s.state == "ARMED"
        }
        active_by_key: dict[tuple[str, str, str, str], list[Any]] = {}
        for setup in active_now:
            if setup.state != "ARMED":
                continue
            key = (setup.symbol, setup.type, setup.htf, setup.direction)
            active_by_key.setdefault(key, []).append(setup)

        needed_tfs: set[str] = set()
        for setup in active_now:
            if setup.state != "ARMED":
                continue
            for tf in setup.ltf_expected.split("|"):
                needed_tfs.add(tf.strip())
            needed_tfs.add(setup.htf)
            inv_tf = invalidation_tf_for_setup(
                setup.htf,
                setup.ltf_expected,
                self._cfg.entry,
                {"4H", "1H", "15M", "5M"},
            )
            needed_tfs.add(inv_tf)

        tfs_to_fetch = set(closed_tfs) | needed_tfs
        for tf in ("4H", "1H", "15M", "5M"):
            if tf not in tfs_to_fetch:
                continue
            candles = await self._bybit.fetch_klines(symbol=symbol, timeframe=tf, limit=500)
            df = candles_to_df(candles)
            if df.empty:
                funnel[f"{tf.lower()}_empty_candles"] += 1
                continue
            series[tf] = df
            if tf == "4H":
                self._latest_4h_series[symbol] = df
                df_4h_for_alignment = df

        prepare_htfs = self._cfg.prepare_htfs()
        if "4H" in prepare_htfs and "4H" in series:
            await self._try_create_reversal(
                symbol,
                series["4H"],
                funnel,
                armed_keys,
                active_by_key,
            )
        for htf in prepare_htfs:
            if htf in series:
                await self._try_create_continuation(
                    symbol,
                    htf,
                    series[htf],
                    df_4h_for_alignment,
                    funnel,
                    armed_keys,
                    active_by_key,
                )
        await self._advance_active_setups(
            symbol=symbol,
            series=series,
            closed_tfs=closed_tfs,
            funnel=funnel,
        )
        return funnel

    async def _try_create_reversal(
        self,
        symbol: str,
        df: Any,
        funnel: Counter[str],
        armed_keys: set[tuple[str, str, str, str]],
        active_by_key: dict[tuple[str, str, str, str], list[Any]],
    ) -> None:
        liberal_cfg = self._cfg.paper_mode.liberal
        strict_lookback = self._cfg.reversal.choch_lookback_bars
        swing_4h = int(self._cfg.pivots.swing_size_by_tf.get("4H", 15))

        atr_v = atr_percent(df)
        if atr_v < self._cfg.filters.min_atr_pct and not (
            liberal_cfg.enabled and atr_v >= liberal_cfg.min_atr_pct
        ):
            funnel["reversal_low_atr"] += 1
            return

        rev_ltf = ltf_expected_for_htf("4H", self._cfg.entry)
        setup, event = detect_reversal_prepare(
            symbol=symbol,
            htf_df=df,
            close_time=int(df.iloc[-1]["open_time"]),
            ttl_hours=self._cfg.reversal.ttl_bars_4h * 4,
            swing_size=swing_4h,
            max_bars_ago_choch=strict_lookback,
            impulse_max_age_bars=self._cfg.pivots.impulse_max_age_bars,
            bos_use_close=self._cfg.pivots.bos_use_close,
            ltf_expected=rev_ltf,
        )
        liberal_wider_choch = False
        if (setup is None or event is None) and liberal_cfg.enabled:
            setup, event = detect_reversal_prepare(
                symbol=symbol,
                htf_df=df,
                close_time=int(df.iloc[-1]["open_time"]),
                ttl_hours=self._cfg.reversal.ttl_bars_4h * 4,
                swing_size=swing_4h,
                max_bars_ago_choch=liberal_cfg.max_bars_ago_4h,
                impulse_max_age_bars=self._cfg.pivots.impulse_max_age_bars,
                bos_use_close=self._cfg.pivots.bos_use_close,
                ltf_expected=rev_ltf,
            )
            liberal_wider_choch = setup is not None
        if setup is None or event is None:
            funnel["reversal_no_prepare_candidate"] += 1
            return

        dedup_key = (symbol, setup.type, "4H", setup.direction)
        if dedup_key in armed_keys:
            replaced = self._invalidate_active_setups_for_key(
                dedup_key,
                armed_keys=armed_keys,
                active_by_key=active_by_key,
            )
            if replaced > 0:
                funnel["reversal_prepare_replaced_by_new_structure"] += replaced

        strict_gate = evaluate_reversal_prepare_detailed(
            df=df,
            choch_direction=setup.direction,
            setup=setup,
            event=event,
            features=self._cfg.strategy_features,
        )
        bar_open_ms = int(df.iloc[-1]["open_time"])

        if strict_gate.ok and not liberal_wider_choch:
            score = strict_gate.score
            setup.score = score
            setup.is_liberal = False
            event.payload["score"] = score
            _enrich_prepare_payload(
                event.payload,
                bar_open_ms=bar_open_ms,
                score=score,
                liberal=False,
            )
            self._repo.upsert_setup(setup)
            signal_row = await self._notifier.send_event(
                kind=SignalKind.PREPARE,
                payload=event.payload,
                close_time=bar_open_ms,
                paper_mode=self._cfg.paper_mode.enabled,
            )
            if signal_row is not None:
                self._repo.save_signal(signal_row)
            armed_keys.add(dedup_key)
            active_by_key[dedup_key] = [setup]
            funnel["reversal_prepare_sent"] += 1
            return

        if liberal_cfg.enabled and self._env.tg_paper_chat_id:
            lib_gate = evaluate_reversal_prepare_liberal(
                df=df,
                choch_direction=setup.direction,
                setup=setup,
                event=event,
                features=self._cfg.strategy_features,
                liberal=liberal_cfg,
            )
            if lib_gate.ok:
                score = lib_gate.score
                setup.score = score
                setup.is_liberal = True
                event.payload["score"] = score
                _enrich_prepare_payload(
                    event.payload,
                    bar_open_ms=bar_open_ms,
                    score=score,
                    liberal=True,
                )
                self._repo.upsert_setup(setup)
                signal_row = await self._notifier.send_event(
                    kind=SignalKind.PREPARE,
                    payload=event.payload,
                    close_time=bar_open_ms,
                    paper_mode=self._cfg.paper_mode.enabled,
                    liberal_paper_only=True,
                )
                if signal_row is not None:
                    self._repo.save_signal(signal_row)
                armed_keys.add(dedup_key)
                active_by_key[dedup_key] = [setup]
                funnel["reversal_prepare_sent_liberal"] += 1
                return
            funnel[f"liberal_{lib_gate.reason}"] += 1

        funnel[strict_gate.reason] += 1

    async def _try_create_continuation(
        self,
        symbol: str,
        htf: str,
        df: Any,
        df_4h_for_alignment: Any,
        funnel: Counter[str],
        armed_keys: set[tuple[str, str, str, str]],
        active_by_key: dict[tuple[str, str, str, str], list[Any]],
    ) -> None:
        liberal_cfg = self._cfg.paper_mode.liberal
        swing_htf = int(self._cfg.pivots.swing_size_by_tf.get(htf, 15))
        setup, event = detect_continuation_prepare(
            symbol=symbol,
            htf=htf,
            htf_df=df,
            close_time=int(df.iloc[-1]["open_time"]),
            swing_size=swing_htf,
            structure_max_bars_ago=self._cfg.continuation.structure_max_bars_ago,
            fib_level=self._cfg.continuation.fib_low,
            impulse_max_age_bars=self._cfg.pivots.impulse_max_age_bars,
            bos_use_close=self._cfg.pivots.bos_use_close,
            ttl_hours=24,
            ltf_expected=ltf_expected_for_htf(htf, self._cfg.entry),
        )
        if setup is None or event is None:
            funnel[f"continuation_{htf.lower()}_no_prepare_candidate"] += 1
            return

        dedup_key = (symbol, setup.type, htf, setup.direction)
        if dedup_key in armed_keys:
            replaced = self._invalidate_active_setups_for_key(
                dedup_key,
                armed_keys=armed_keys,
                active_by_key=active_by_key,
            )
            if replaced > 0:
                funnel[f"continuation_{htf.lower()}_prepare_replaced_by_new_structure"] += replaced

        strict_gate = evaluate_continuation_prepare_detailed(
            df_htf=df,
            setup=setup,
            event=event,
            features=self._cfg.strategy_features,
            df_4h=df_4h_for_alignment,
        )
        bar_open_ms = int(df.iloc[-1]["open_time"])

        if strict_gate.ok:
            score = strict_gate.score
            setup.score = score
            setup.is_liberal = False
            event.payload["score"] = score
            _enrich_prepare_payload(
                event.payload,
                bar_open_ms=bar_open_ms,
                score=score,
                liberal=False,
            )
            self._repo.upsert_setup(setup)
            signal_row = await self._notifier.send_event(
                kind=SignalKind.PREPARE,
                payload=event.payload,
                close_time=bar_open_ms,
                paper_mode=self._cfg.paper_mode.enabled,
            )
            if signal_row is not None:
                self._repo.save_signal(signal_row)
            armed_keys.add(dedup_key)
            active_by_key[dedup_key] = [setup]
            funnel[f"continuation_{htf.lower()}_prepare_sent"] += 1
            return

        if liberal_cfg.enabled and self._env.tg_paper_chat_id:
            lib_gate = evaluate_continuation_prepare_liberal(
                df_htf=df,
                setup=setup,
                event=event,
                features=self._cfg.strategy_features,
                df_4h=df_4h_for_alignment,
                liberal=liberal_cfg,
            )
            if lib_gate.ok:
                score = lib_gate.score
                setup.score = score
                setup.is_liberal = True
                event.payload["score"] = score
                _enrich_prepare_payload(
                    event.payload,
                    bar_open_ms=bar_open_ms,
                    score=score,
                    liberal=True,
                )
                self._repo.upsert_setup(setup)
                signal_row = await self._notifier.send_event(
                    kind=SignalKind.PREPARE,
                    payload=event.payload,
                    close_time=bar_open_ms,
                    paper_mode=self._cfg.paper_mode.enabled,
                    liberal_paper_only=True,
                )
                if signal_row is not None:
                    self._repo.save_signal(signal_row)
                armed_keys.add(dedup_key)
                active_by_key[dedup_key] = [setup]
                funnel[f"continuation_{htf.lower()}_prepare_sent_liberal"] += 1
                return
            funnel[f"liberal_{lib_gate.reason}"] += 1

        funnel[strict_gate.reason] += 1

    async def _advance_active_setups(
        self,
        symbol: str,
        series: dict[str, Any],
        closed_tfs: list[str],
        funnel: Counter[str],
    ) -> None:
        active = [s for s in self._repo.load_active_setups() if s.symbol == symbol]
        liberal_cfg = self._cfg.paper_mode.liberal
        htf_breaks_cache: dict[str, list[Any]] = {}
        if active and not series:
            funnel["active_setups_waiting_no_fresh_ltf"] += len(active)

        for setup in active:
            if setup.state != "ARMED":
                continue

            htf_df = series.get(setup.htf)
            if htf_df is not None and not htf_df.empty:
                breaks = htf_breaks_cache.get(setup.htf)
                if breaks is None:
                    swing = int(self._cfg.pivots.swing_size_by_tf.get(setup.htf, 15))
                    breaks = extract_structure_breaks_htf(
                        htf_df,
                        swing_size=swing,
                        use_close=self._cfg.pivots.bos_use_close,
                        impulse_lock=True,
                    )
                    htf_breaks_cache[setup.htf] = breaks
                decision = decide_setup_structure_transition(
                    breaks=breaks,
                    df=htf_df,
                    setup_direction=setup.direction,
                    since_open_ms=prepare_since_open_ms(setup),
                )
                if decision.action == "INVALIDATE_OPPOSITE":
                    self._repo.mark_setup_state(setup.id, "INVALIDATED", utcnow())
                    funnel["setup_invalidated_by_opposite_structure"] += 1
                    funnel[f"setup_invalidated_by_opposite_structure_{setup.htf.lower()}"] += 1
                    continue
                if decision.action == "RESET_SAME_DIRECTION":
                    self._repo.mark_setup_state(setup.id, "INVALIDATED", utcnow())
                    funnel["setup_reset_by_new_structure_same_direction"] += 1
                    funnel[f"setup_reset_by_new_structure_same_direction_{setup.htf.lower()}"] += 1
                    continue

            inv_result = check_price_invalidation(
                setup=setup,
                series=series,
                entry=self._cfg.entry,
            )
            if inv_result.invalidated:
                self._repo.mark_setup_state(setup.id, "INVALIDATED", utcnow())
                funnel["setup_invalidated_on_tf"] += 1
                funnel[f"setup_invalidated_on_{inv_result.inv_tf.lower()}"] += 1
                continue

            ltf_result = resolve_ltf_confirmation(
                setup=setup,
                series=series,
                closed_tfs=closed_tfs,
                entry=self._cfg.entry,
                pivot_swing_by_tf=self._cfg.pivots.swing_size_by_tf,
                liberal_swing_override=(
                    liberal_cfg.ltf_swing_length_override if liberal_cfg.enabled else None
                ),
                use_close=self._cfg.pivots.bos_use_close,
            )
            if ltf_result.status == "NO_MATCHING_LTF":
                funnel["active_setup_no_matching_ltf"] += 1
                continue
            if ltf_result.status == "LTF_NOT_CLOSED":
                funnel["active_setup_ltf_bar_not_closed"] += 1
                continue
            if ltf_result.status == "WAITING_CONFIRM":
                suffix = ltf_result.wait_suffix or "structure"
                funnel[f"setup_waiting_ltf_{suffix}"] += 1
                continue

            used_tf = ltf_result.used_tf
            ltf_df = ltf_result.ltf_df
            row = ltf_result.row
            choch = ltf_result.choch
            if used_tf is None or ltf_df is None or row is None or choch is None:
                funnel["active_setup_ltf_bar_not_closed"] += 1
                continue
            funnel[f"entry_confirm_{choch.kind.lower()}_{used_tf.lower()}"] += 1
            state, event, phase_new = tick_setup(
                setup=setup,
                price_low=float(row["low"]),
                price_high=float(row["high"]),
                choch_direction=choch.direction if choch else None,
                check_invalidation=False,
            )
            if phase_new is not None:
                self._repo.update_setup_phase(setup.id, phase_new)
                setup.phase = phase_new
            if state != setup.state:
                self._repo.mark_setup_state(setup.id, state, utcnow())
            if event is None:
                funnel["active_setup_no_event"] += 1
                continue

            payload: dict[str, Any] = dict(event.payload)
            payload["symbol"] = symbol
            payload["type"] = setup.type
            payload["direction"] = setup.direction
            payload["htf"] = used_tf
            payload["entry_ltf"] = used_tf
            payload["setup_htf"] = setup.htf
            payload["ltf_expected"] = setup.ltf_expected
            bar_open_ms = int(row["open_time"])
            payload["bar_open_ms"] = bar_open_ms
            payload["ote_low"] = setup.ote_low
            payload["ote_high"] = setup.ote_high
            payload["liberal"] = setup.is_liberal

            if event.kind == "ENTRY":
                payload["entry"] = float(row["close"])
                if self._cfg.entry.require_close_beyond_choch:
                    level = choch.level if choch else float(row["close"])
                    if not close_beyond_level(float(row["close"]), level, setup.direction):
                        funnel["entry_rejected_close_not_beyond_level"] += 1
                        continue

                entry_price = float(row["close"])
                min_rr = (
                    liberal_cfg.min_rr
                    if setup.is_liberal and liberal_cfg.enabled
                    else self._cfg.filters.min_rr
                )
                levels, reject = finalize_entry_levels(
                    entry=entry_price,
                    direction=setup.direction,
                    invalidation_price=setup.invalidation_price,
                    compute_sl_tp=self._cfg.entry.compute_sl_tp,
                    min_rr=min_rr,
                )
                if reject == "zero_risk":
                    funnel["entry_rejected_zero_risk"] += 1
                    continue
                if reject == "rr_below_min":
                    funnel["entry_rejected_rr_below_min"] += 1
                    continue
                if levels is not None:
                    payload.update(levels)
                else:
                    funnel["entry_sent_without_sl_tp"] += 1
                signal_row = await self._notifier.send_event(
                    kind=SignalKind.ENTRY,
                    payload=payload,
                    close_time=bar_open_ms,
                    paper_mode=self._cfg.paper_mode.enabled,
                    liberal_paper_only=bool(setup.is_liberal),
                )
                if signal_row is not None:
                    self._repo.save_signal(signal_row)
                funnel["entry_sent"] += 1
            elif event.kind == "INVALIDATED":
                payload["invalidation_price"] = setup.invalidation_price
                payload["mark_price"] = float(row["close"])
                signal_row = await self._notifier.send_event(
                    kind=SignalKind.INVALIDATED,
                    payload=payload,
                    close_time=bar_open_ms,
                    paper_mode=self._cfg.paper_mode.enabled,
                    liberal_paper_only=bool(setup.is_liberal),
                )
                if signal_row is not None:
                    self._repo.save_signal(signal_row)
                funnel["invalidated_sent"] += 1


async def _main() -> None:
    app = SignalBotApp()
    await app.run()


if __name__ == "__main__":
    asyncio.run(_main())
