from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from bot.analyzer.continuation import detect_continuation_prepare
from bot.analyzer.entry_ltf import (
    cascade_sequence_for_htf,
    invalidation_tf_for_setup,
    ltf_expected_for_htf,
    prepare_since_open_ms,
)
from bot.analyzer.fib_dca import (
    deserialize_filled_fibs,
    deserialize_plan,
    filled_weight_pct,
    initial_trigger_fills,
    initialize_fib_dca_setup,
    new_fib_dca_fills,
    serialize_filled_fibs,
    target_reached,
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
from bot.analyzer.setup_machine import tick_setup
from bot.analyzer.setup_runtime import check_price_invalidation, resolve_ltf_confirmation
from bot.analyzer.strategy_gates import (
    evaluate_continuation_prepare_detailed,
    evaluate_continuation_prepare_liberal,
    evaluate_reversal_prepare_detailed,
    evaluate_reversal_prepare_liberal,
)
from bot.config import EnvConfig, find_bot_config_source, load_bot_config
from bot.entry_stats import (
    build_entry_stats_candidates,
    evaluate_entry_stats_candidate,
    format_entry_stats_messages,
    prepare_payloads_by_setup,
)
from bot.exchange.bybit_client import INTERVAL_MS_MAP, BybitClient
from bot.market.candles import candles_to_df
from bot.market.pivots import extract_structure_breaks_htf
from bot.notify.telegram import TelegramNotifier
from bot.prepare_stats import (
    build_prepare_stats_candidates,
    evaluate_prepare_stats_candidate,
    format_prepare_stats_messages,
)
from bot.scheduler import TimeframeScheduler
from bot.storage.models import Signal, SignalKind
from bot.storage.repo import Repository
from bot.util.logging import setup_logging
from bot.util.time import ensure_utc, utcnow

logger = logging.getLogger(__name__)

ENTRY_STATS_LAST_RUN_KEY = "entry_stats_last_run"
ENTRY_STATS_PROCESSED_KEY = "entry_stats_processed"
PREPARE_STATS_LAST_RUN_KEY = "prepare_stats_last_run"
PREPARE_STATS_PROCESSED_KEY = "prepare_stats_processed"


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


def _apply_entry_cascade_update(setup: Any, update: Any) -> None:
    setup.entry_cascade_stage = int(update.stage)
    setup.entry_cascade_since_ms = update.since_ms
    setup.entry_cascade_touch_ms = update.touch_ms
    setup.entry_cascade_retrace_level = update.retrace_level


def _reset_entry_cascade(setup: Any) -> None:
    setup.entry_cascade_stage = 0
    setup.entry_cascade_since_ms = None
    setup.entry_cascade_touch_ms = None
    setup.entry_cascade_retrace_level = None


def _apply_advanced_entry_update(setup: Any, update: Any) -> None:
    setup.entry_advanced_stage = str(update.stage)
    setup.entry_sweep_level = update.sweep_level
    setup.entry_sweep_extreme = update.sweep_extreme
    setup.entry_sweep_ms = update.sweep_ms
    setup.entry_reclaim_ms = update.reclaim_ms
    setup.entry_confirm_level = update.confirm_level
    setup.entry_confirm_ms = update.confirm_ms


def _reset_advanced_entry(setup: Any) -> None:
    setup.entry_advanced_stage = "WAIT_SWEEP"
    setup.entry_sweep_level = None
    setup.entry_sweep_extreme = None
    setup.entry_sweep_ms = None
    setup.entry_reclaim_ms = None
    setup.entry_confirm_level = None
    setup.entry_confirm_ms = None


def _entry_tfs_for_setup(setup: Any) -> set[str]:
    configured = {
        tf.strip()
        for tf in str(getattr(setup, "ltf_expected", "")).split("|")
        if tf.strip()
    }
    return configured


def _current_entry_tfs_for_setup(setup: Any, entry_cfg: Any) -> set[str]:
    active_trade_tf = getattr(setup, "active_trade_tf", None)
    if active_trade_tf:
        return {str(active_trade_tf)}
    if str(getattr(setup, "entry_mode", "simple")).lower() == "fib_dca":
        return {
            str(entry_cfg.fib_dca.monitoring_tf_by_htf.get(str(setup.htf), str(setup.htf)))
        }
    if str(getattr(setup, "entry_mode", "simple")).lower() in {"advanced", "sweep_reclaim"}:
        return _entry_tfs_for_setup(setup)
    sequence = cascade_sequence_for_htf(str(setup.htf), entry_cfg)
    if not sequence:
        return _entry_tfs_for_setup(setup)
    stage = int(getattr(setup, "entry_cascade_stage", 0) or 0)
    if stage < 0 or stage >= len(sequence):
        stage = 0
    return {sequence[stage]}


class SignalBotApp:
    def __init__(self) -> None:
        self._env = EnvConfig()
        self._cfg = load_bot_config(find_bot_config_source())
        setup_logging(self._env.bot_log_level)
        self._repo = Repository(self._env.bot_db_url)
        self._repo.create_schema()
        self._bybit = BybitClient(
            category=self._cfg.exchange.category,
            api_key=self._env.bybit_api_key,
            api_secret=self._env.bybit_api_secret,
            domain=self._cfg.exchange.domain,
            tld=self._cfg.exchange.tld,
        )
        self._paper_chat_id = self._env.telegram_chat_id(
            self._cfg.telegram.paper_chat_id_env
        )
        self._notifier = TelegramNotifier(
            bot_token=self._env.tg_bot_token,
            chat_id=self._env.telegram_chat_id(self._cfg.telegram.fallback_chat_id_env),
            prepare_chat_id=self._env.telegram_chat_id(
                self._cfg.telegram.prepare_chat_id_env
            ),
            entry_chat_id=self._env.telegram_chat_id(self._cfg.telegram.entry_chat_id_env),
            paper_chat_id=self._paper_chat_id,
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
        await self._maybe_process_entry_stats()
        await self._maybe_process_prepare_stats()
        closed_tfs = self._scheduler.closed_timeframes()
        if not closed_tfs:
            return
        if set(closed_tfs) == {"1M"}:
            active = self._repo.load_active_setups()
            if not any(
                "1M" in _current_entry_tfs_for_setup(setup, self._cfg.entry)
                for setup in active
            ):
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

    async def _send_prepare_event(
        self,
        *,
        payload: dict[str, Any],
        close_time: int,
        liberal_paper_only: bool = False,
    ) -> Any:
        if not self._cfg.telegram.send_prepare_signals:
            setup_id = str(payload.get("setup_id", "system"))
            return Signal(
                id=TelegramNotifier.build_signal_id(setup_id, SignalKind.PREPARE.value, close_time),
                setup_id=setup_id,
                kind=SignalKind.PREPARE.value,
                payload_json=json.dumps(payload, ensure_ascii=True),
                sent_at=utcnow(),
            )
        return await self._notifier.send_event(
            kind=SignalKind.PREPARE,
            payload=payload,
            close_time=close_time,
            paper_mode=self._cfg.paper_mode.enabled,
            liberal_paper_only=liberal_paper_only,
        )

    async def _maybe_process_entry_stats(self) -> None:
        stats_cfg = self._cfg.entry_stats
        if not stats_cfg.enabled:
            return
        interval = timedelta(hours=max(1, int(stats_cfg.check_interval_hours)))
        state = self._repo.get_state_value(ENTRY_STATS_LAST_RUN_KEY)
        now = utcnow()
        if state and state.get("last_run"):
            last_run = now
            try:
                last_run = ensure_utc(datetime.fromisoformat(state["last_run"]))
            except ValueError:
                last_run = now - interval
            if now - last_run < interval:
                return

        try:
            results = await self._collect_entry_stats()
        except Exception:
            logger.exception("Entry stats processing failed")
            self._repo.set_state_value(ENTRY_STATS_LAST_RUN_KEY, {"last_run": now.isoformat()})
            return
        if results:
            try:
                for message in format_entry_stats_messages(results):
                    await self._notifier.send_entry_stats(
                        message,
                        paper_mode=self._cfg.paper_mode.enabled,
                    )
            except Exception:
                logger.exception("Entry stats notification failed")
                self._repo.set_state_value(
                    ENTRY_STATS_LAST_RUN_KEY,
                    {"last_run": now.isoformat()},
                )
                return
            processed = self._repo.get_state_value(ENTRY_STATS_PROCESSED_KEY) or {}
            for result in results:
                processed[result.signal_id] = result.status
            self._repo.set_state_value(ENTRY_STATS_PROCESSED_KEY, processed)
            logger.info("Entry stats sent | count=%s", len(results))
        self._repo.set_state_value(ENTRY_STATS_LAST_RUN_KEY, {"last_run": now.isoformat()})

    async def _collect_entry_stats(self) -> list[Any]:
        all_signals = self._repo.load_signals_by_kind(("PREPARE", "ENTRY"))
        processed = self._repo.get_state_value(ENTRY_STATS_PROCESSED_KEY) or {}
        prepares = prepare_payloads_by_setup(all_signals)
        entries = [signal for signal in all_signals if signal.kind == "ENTRY"]
        candidates = build_entry_stats_candidates(
            entries,
            prepares,
            set(processed.keys()),
        )
        candidates = candidates[: max(1, int(self._cfg.entry_stats.max_candidates_per_run))]
        results: list[Any] = []
        now_ms = int(utcnow().timestamp() * 1000)
        for candidate in candidates:
            timeframe = candidate.timeframe if candidate.timeframe in INTERVAL_MS_MAP else "5M"
            tf_ms = INTERVAL_MS_MAP[timeframe]
            limit = max(2, int((now_ms - candidate.entry_open_ms) / tf_ms) + 5)
            candles = await self._bybit.fetch_klines(
                symbol=candidate.symbol,
                timeframe=timeframe,
                limit=limit,
            )
            result = evaluate_entry_stats_candidate(candidate, candles)
            if result is not None:
                results.append(result)
            await asyncio.sleep(0.03)
        results.sort(key=lambda result: result.outcome_open_ms)
        return results

    async def _maybe_process_prepare_stats(self) -> None:
        stats_cfg = self._cfg.prepare_stats
        if not stats_cfg.enabled:
            return
        interval = timedelta(hours=max(1, int(stats_cfg.check_interval_hours)))
        state = self._repo.get_state_value(PREPARE_STATS_LAST_RUN_KEY)
        now = utcnow()
        if state and state.get("last_run"):
            try:
                last_run = ensure_utc(datetime.fromisoformat(state["last_run"]))
            except ValueError:
                last_run = now - interval
            if now - last_run < interval:
                return

        try:
            results, fib_levels = await self._collect_prepare_stats()
        except Exception:
            logger.exception("Prepare stats processing failed")
            self._repo.set_state_value(PREPARE_STATS_LAST_RUN_KEY, {"last_run": now.isoformat()})
            return
        if results:
            try:
                for message in format_prepare_stats_messages(results, fib_levels=fib_levels):
                    await self._notifier.send_entry_stats(
                        message,
                        paper_mode=self._cfg.paper_mode.enabled,
                    )
            except Exception:
                logger.exception("Prepare stats notification failed")
                self._repo.set_state_value(
                    PREPARE_STATS_LAST_RUN_KEY,
                    {"last_run": now.isoformat()},
                )
                return
            processed = self._repo.get_state_value(PREPARE_STATS_PROCESSED_KEY) or {}
            for result in results:
                processed[result.signal_id] = result.status
            self._repo.set_state_value(PREPARE_STATS_PROCESSED_KEY, processed)
            logger.info("Prepare stats sent | count=%s", len(results))
        self._repo.set_state_value(PREPARE_STATS_LAST_RUN_KEY, {"last_run": now.isoformat()})

    async def _collect_prepare_stats(self) -> tuple[list[Any], list[float]]:
        stats_cfg = self._cfg.prepare_stats
        fib_levels = (
            [float(fib) for fib in stats_cfg.fib_levels]
            if stats_cfg.fib_levels
            else [float(level.fib) for level in self._cfg.entry.fib_dca.levels]
        )
        signals = self._repo.load_signals_by_kind(("PREPARE",))
        processed = self._repo.get_state_value(PREPARE_STATS_PROCESSED_KEY) or {}
        candidates = build_prepare_stats_candidates(
            signals,
            processed_signal_ids=set(processed.keys()),
            fib_levels=fib_levels,
            evaluation_tf_by_htf=stats_cfg.evaluation_tf_by_htf,
        )
        candidates = candidates[: max(1, int(stats_cfg.max_candidates_per_run))]
        results: list[Any] = []
        now_ms = int(utcnow().timestamp() * 1000)
        for candidate in candidates:
            timeframe = (
                candidate.timeframe if candidate.timeframe in INTERVAL_MS_MAP else candidate.htf
            )
            tf_ms = INTERVAL_MS_MAP.get(timeframe, INTERVAL_MS_MAP["5M"])
            limit = max(2, int((now_ms - candidate.prepare_open_ms) / tf_ms) + 5)
            candles = await self._bybit.fetch_klines(
                symbol=candidate.symbol,
                timeframe=timeframe,
                limit=limit,
            )
            result = evaluate_prepare_stats_candidate(candidate, candles)
            if result is not None:
                results.append(result)
            await asyncio.sleep(0.03)
        results.sort(key=lambda result: result.outcome_open_ms)
        return results, fib_levels

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
            needed_tfs.update(_current_entry_tfs_for_setup(setup, self._cfg.entry))
            needed_tfs.add(setup.htf)
            inv_tf = invalidation_tf_for_setup(
                setup.htf,
                setup.ltf_expected,
                self._cfg.entry,
                {"4H", "1H", "15M", "5M", "1M"},
            )
            needed_tfs.add(inv_tf)

        prepare_htfs = self._cfg.prepare_htfs()
        tfs_to_fetch = (set(closed_tfs) & set(prepare_htfs)) | needed_tfs
        for tf in ("4H", "1H", "15M", "5M", "1M"):
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

        # На символ допускаем только один активный PREPARE: пока setup ARMED,
        # новые PREPARE не создаём и ждём ENTRY/INVALIDATED/EXPIRED.
        if armed_keys:
            funnel["prepare_creation_skipped_active_setup"] += len(armed_keys)
        else:
            if "4H" in prepare_htfs and "4H" in series:
                await self._try_create_reversal(
                    symbol,
                    series["4H"],
                    funnel,
                    armed_keys,
                    active_by_key,
                )
            for htf in prepare_htfs:
                if armed_keys:
                    break
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
            entry_mode=self._cfg.entry.mode,
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
                entry_mode=self._cfg.entry.mode,
            )
            liberal_wider_choch = setup is not None
        if setup is None or event is None:
            funnel["reversal_no_prepare_candidate"] += 1
            return
        initialize_fib_dca_setup(
            setup=setup,
            prepare_payload=event.payload,
            config=self._cfg.entry.fib_dca,
        )

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
            signal_row = await self._send_prepare_event(
                payload=event.payload,
                close_time=bar_open_ms,
            )
            if signal_row is not None:
                self._repo.save_signal(signal_row)
            armed_keys.add(dedup_key)
            active_by_key[dedup_key] = [setup]
            funnel["reversal_prepare_sent"] += 1
            return

        if liberal_cfg.enabled and self._paper_chat_id:
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
                signal_row = await self._send_prepare_event(
                    payload=event.payload,
                    close_time=bar_open_ms,
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
            entry_mode=self._cfg.entry.mode,
        )
        if setup is None or event is None:
            funnel[f"continuation_{htf.lower()}_no_prepare_candidate"] += 1
            return
        initialize_fib_dca_setup(
            setup=setup,
            prepare_payload=event.payload,
            config=self._cfg.entry.fib_dca,
        )

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
            signal_row = await self._send_prepare_event(
                payload=event.payload,
                close_time=bar_open_ms,
            )
            if signal_row is not None:
                self._repo.save_signal(signal_row)
            armed_keys.add(dedup_key)
            active_by_key[dedup_key] = [setup]
            funnel[f"continuation_{htf.lower()}_prepare_sent"] += 1
            return

        if liberal_cfg.enabled and self._paper_chat_id:
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
                signal_row = await self._send_prepare_event(
                    payload=event.payload,
                    close_time=bar_open_ms,
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
        max_entries_per_setup = max(1, int(self._cfg.entry.max_entries_per_setup))
        htf_breaks_cache: dict[str, list[Any]] = {}
        position_setup_id = next(
            (
                setup.id
                for setup in active
                if getattr(setup, "active_trade_stop_price", None) is not None
                or (
                    str(getattr(setup, "entry_mode", "simple")).lower() == "fib_dca"
                    and bool(
                        deserialize_filled_fibs(getattr(setup, "fib_dca_filled_json", None))
                    )
                )
            ),
            None,
        )
        if active and not series:
            funnel["active_setups_waiting_no_fresh_ltf"] += len(active)

        for setup in active:
            if setup.state != "ARMED":
                continue
            if position_setup_id is not None and setup.id != position_setup_id:
                funnel["entry_skipped_open_position"] += 1
                continue

            if getattr(setup, "active_trade_stop_price", None) is not None:
                await self._advance_open_trade(
                    setup=setup,
                    symbol=symbol,
                    series=series,
                    closed_tfs=closed_tfs,
                    funnel=funnel,
                )
                continue

            setup_entry_mode = str(getattr(setup, "entry_mode", "simple")).lower()
            if setup_entry_mode == "fib_dca" and deserialize_filled_fibs(
                getattr(setup, "fib_dca_filled_json", None)
            ):
                # Filled DCA levels are one open position. Keep monitoring and
                # filling that position until its actual target or stop.
                await self._advance_fib_dca_setup(
                    setup=setup,
                    symbol=symbol,
                    series=series,
                    closed_tfs=closed_tfs,
                    funnel=funnel,
                )
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
                raw_action = decision.action
                decision = apply_reset_after_first_entry_policy(
                    decision=decision,
                    entry_count=int(setup.entry_count or 0),
                )
                if raw_action == "RESET_SAME_DIRECTION" and decision.action == "KEEP":
                    funnel["setup_reset_same_direction_skipped_before_first_entry"] += 1
                if decision.action == "INVALIDATE_OPPOSITE":
                    keep_fib_dca = (
                        str(getattr(setup, "entry_mode", "simple")).lower() == "fib_dca"
                        and not self._cfg.entry.fib_dca.cancel_remaining_on_opposite_structure
                    )
                    if keep_fib_dca:
                        funnel["fib_dca_kept_after_opposite_structure"] += 1
                    else:
                        self._repo.mark_setup_state(setup.id, "INVALIDATED", utcnow())
                        funnel["setup_invalidated_by_opposite_structure"] += 1
                        funnel[
                            f"setup_invalidated_by_opposite_structure_{setup.htf.lower()}"
                        ] += 1
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
                row = inv_result.row
                if row is not None:
                    await self._send_invalidated_event(
                        setup=setup,
                        symbol=symbol,
                        timeframe=inv_result.inv_tf,
                        bar_open_ms=int(row["open_time"]),
                        mark_price=float(setup.invalidation_price),
                    )
                self._repo.mark_setup_state(setup.id, "INVALIDATED", utcnow())
                funnel["setup_invalidated_on_tf"] += 1
                funnel[f"setup_invalidated_on_{inv_result.inv_tf.lower()}"] += 1
                continue

            if setup_entry_mode == "fib_dca":
                await self._advance_fib_dca_setup(
                    setup=setup,
                    symbol=symbol,
                    series=series,
                    closed_tfs=closed_tfs,
                    funnel=funnel,
                )
                if deserialize_filled_fibs(getattr(setup, "fib_dca_filled_json", None)):
                    position_setup_id = setup.id
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
            if ltf_result.cascade_update is not None:
                _apply_entry_cascade_update(setup, ltf_result.cascade_update)
                setup.updated_at = utcnow()
                self._repo.upsert_setup(setup)
                funnel["entry_cascade_state_updated"] += 1
            if ltf_result.advanced_update is not None:
                _apply_advanced_entry_update(setup, ltf_result.advanced_update)
                setup.updated_at = utcnow()
                self._repo.upsert_setup(setup)
                funnel["entry_advanced_state_updated"] += 1
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
            if ltf_result.status == "CASCADE_ADVANCED":
                used_tf = ltf_result.used_tf or "unknown"
                choch = ltf_result.choch
                if choch is not None:
                    funnel[f"entry_cascade_{choch.kind.lower()}_{used_tf.lower()}"] += 1
                funnel[f"entry_cascade_advanced_{used_tf.lower()}"] += 1
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
            if event is None:
                if state != setup.state:
                    self._repo.mark_setup_state(setup.id, state, utcnow())
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
            payload["invalidation_price"] = setup.invalidation_price
            payload["score"] = setup.score
            payload["liberal"] = setup.is_liberal
            payload["confirm_kind"] = str(choch.kind)
            payload["confirm_level"] = float(choch.level)
            payload["confirm_bars_ago"] = int(choch.bars_ago)
            payload["confirm_broken_open_ms"] = choch.broken_open_ms
            payload["confirm_reset_level"] = choch.reset_level

            if event.kind == "ENTRY":
                payload["entry"] = float(row["close"])
                if (
                    setup.last_entry_bar_ms is not None
                    and int(setup.last_entry_bar_ms) == bar_open_ms
                ):
                    funnel["entry_skipped_duplicate_bar"] += 1
                    continue
                if int(setup.entry_count or 0) >= max_entries_per_setup:
                    self._repo.mark_setup_state(setup.id, "CONFIRMED", utcnow())
                    setup.state = "CONFIRMED"
                    funnel["entry_limit_reached"] += 1
                    continue
                payload["entry_index"] = int(setup.entry_count or 0) + 1
                payload["entries_max"] = max_entries_per_setup
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
                if self._cfg.entry.require_close_beyond_choch:
                    level = choch.level if choch else float(row["close"])
                    if not close_beyond_level(float(row["close"]), level, setup.direction):
                        funnel["entry_rejected_close_not_beyond_level"] += 1
                        continue

                entry_price = float(row["close"])
                simple_stop, simple_stop_source = recommended_entry_stop(
                    entry=entry_price,
                    direction=setup.direction,
                    reset_level=choch.reset_level,
                    invalidation_price=setup.invalidation_price,
                )
                recommended_stop = (
                    float(ltf_result.recommended_stop)
                    if ltf_result.recommended_stop is not None
                    else simple_stop
                )
                payload["entry_mode"] = str(getattr(setup, "entry_mode", "simple"))
                payload["recommended_stop"] = recommended_stop
                payload["recommended_stop_source"] = (
                    str(ltf_result.recommended_stop_source or "advanced")
                    if ltf_result.recommended_stop is not None
                    else simple_stop_source
                )
                if ltf_result.target_price is not None:
                    payload["target_price"] = float(ltf_result.target_price)
                elif setup.entry_target_price is not None:
                    payload["target_price"] = float(setup.entry_target_price)
                if ltf_result.rr_to_target is not None:
                    payload["rr_to_target"] = float(ltf_result.rr_to_target)
                if int(setup.entry_count or 0) > 0 and not reentry_price_improved(
                    direction=setup.direction,
                    entry_price=entry_price,
                    last_entry_price=setup.last_entry_price,
                ):
                    funnel["entry_reentry_wait_better_price"] += 1
                    continue
                min_rr = (
                    liberal_cfg.min_rr
                    if setup.is_liberal and liberal_cfg.enabled
                    else self._cfg.filters.min_rr
                )
                if ltf_result.recommended_stop is not None and ltf_result.target_price is not None:
                    levels = (
                        {
                            "sl": recommended_stop,
                            "tp": float(ltf_result.target_price),
                            "tp1": float(ltf_result.target_price),
                        }
                        if self._cfg.entry.compute_sl_tp
                        else None
                    )
                    reject = None
                else:
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
                if signal_row is None:
                    funnel["entry_send_skipped"] += 1
                    continue
                self._repo.save_signal(signal_row)
                setup.entry_count = int(setup.entry_count or 0) + 1
                setup.last_entry_bar_ms = bar_open_ms
                setup.last_entry_price = entry_price
                setup.last_entry_swing_level = (
                    float(choch.reset_level) if choch.reset_level is not None else None
                )
                setup.active_trade_stop_price = recommended_stop
                setup.active_trade_target_price = float(
                    payload.get("target_price")
                    or payload.get("tp")
                    or setup.entry_target_price
                )
                setup.active_trade_tf = used_tf
                setup.state = "ARMED"
                position_setup_id = setup.id
                setup.updated_at = utcnow()
                self._repo.upsert_setup(setup)
                funnel["entry_sent_keep_setup_armed"] += 1
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
            elif state != setup.state:
                self._repo.mark_setup_state(setup.id, state, utcnow())

    async def _send_invalidated_event(
        self,
        *,
        setup: Any,
        symbol: str,
        timeframe: str,
        bar_open_ms: int,
        mark_price: float,
    ) -> None:
        payload = {
            "setup_id": setup.id,
            "symbol": symbol,
            "type": setup.type,
            "direction": setup.direction,
            "htf": timeframe,
            "setup_htf": setup.htf,
            "bar_open_ms": bar_open_ms,
            "invalidation_price": float(setup.invalidation_price),
            "mark_price": mark_price,
            "liberal": setup.is_liberal,
        }
        signal_row = await self._notifier.send_event(
            kind=SignalKind.INVALIDATED,
            payload=payload,
            close_time=bar_open_ms,
            paper_mode=self._cfg.paper_mode.enabled,
            liberal_paper_only=bool(setup.is_liberal),
        )
        if signal_row is not None:
            self._repo.save_signal(signal_row)

    async def _advance_open_trade(
        self,
        *,
        setup: Any,
        symbol: str,
        series: dict[str, Any],
        closed_tfs: list[str],
        funnel: Counter[str],
    ) -> None:
        trade_tf = str(getattr(setup, "active_trade_tf", None) or setup.htf)
        df = series.get(trade_tf)
        if trade_tf not in set(closed_tfs) or df is None or df.empty:
            funnel["active_trade_tf_not_closed"] += 1
            return
        row = df.iloc[-1]
        low = float(row["low"])
        high = float(row["high"])
        stop = float(setup.active_trade_stop_price)
        target = float(setup.active_trade_target_price)
        hit_stop = low <= stop if setup.direction == "LONG" else high >= stop
        hit_target = high >= target if setup.direction == "LONG" else low <= target
        if hit_stop:
            bar_open_ms = int(row["open_time"])
            await self._send_invalidated_event(
                setup=setup,
                symbol=symbol,
                timeframe=trade_tf,
                bar_open_ms=bar_open_ms,
                mark_price=stop,
            )
            self._repo.mark_setup_state(setup.id, "INVALIDATED", utcnow())
            funnel["active_trade_stopped"] += 1
            return
        if hit_target:
            self._repo.mark_setup_state(setup.id, "CONFIRMED", utcnow())
            funnel["active_trade_target_reached"] += 1
            return
        funnel["active_trade_open"] += 1

    async def _advance_fib_dca_setup(
        self,
        *,
        setup: Any,
        symbol: str,
        series: dict[str, Any],
        closed_tfs: list[str],
        funnel: Counter[str],
    ) -> None:
        plan = deserialize_plan(getattr(setup, "fib_dca_plan_json", None))
        if not plan:
            funnel["fib_dca_missing_plan"] += 1
            return

        monitor_tf = str(
            self._cfg.entry.fib_dca.monitoring_tf_by_htf.get(str(setup.htf), str(setup.htf))
        )
        used_tf = monitor_tf
        df = series.get(monitor_tf) if monitor_tf in set(closed_tfs) else None
        if df is None or df.empty:
            htf_df = series.get(setup.htf)
            if htf_df is None or htf_df.empty or setup.htf not in set(closed_tfs):
                funnel["fib_dca_monitor_not_closed"] += 1
                return
            # The PREPARE bar itself is sufficient to fill the trigger level.
            if int(htf_df.iloc[-1]["open_time"]) != int(prepare_since_open_ms(setup)):
                funnel["fib_dca_monitor_not_closed"] += 1
                return
            df = htf_df
            used_tf = str(setup.htf)

        row = df.iloc[-1]
        bar_open_ms = int(row["open_time"])
        low = float(row["low"])
        high = float(row["high"])
        filled = deserialize_filled_fibs(getattr(setup, "fib_dca_filled_json", None))
        fills = new_fib_dca_fills(
            direction=str(setup.direction),
            plan=plan,
            filled_fibs=filled,
            price_low=low,
            price_high=high,
        )
        initial_fills = initial_trigger_fills(
            plan=plan,
            filled_fibs=filled,
            trigger_price=float(setup.origin_price),
        )
        fills = list({level.fib: level for level in [*initial_fills, *fills]}.values())

        invalidated = (
            low <= float(setup.invalidation_price)
            if str(setup.direction) == "LONG"
            else high >= float(setup.invalidation_price)
        )
        if invalidated:
            await self._send_invalidated_event(
                setup=setup,
                symbol=symbol,
                timeframe=used_tf,
                bar_open_ms=bar_open_ms,
                mark_price=float(setup.invalidation_price),
            )
            self._repo.mark_setup_state(setup.id, "INVALIDATED", utcnow())
            funnel["fib_dca_invalidated_on_monitor_tf"] += 1
            return

        changed = False
        for level in fills:
            next_filled = {*filled, level.fib}
            average_entry = weighted_average_entry(plan, next_filled)
            total_weight = filled_weight_pct(plan, next_filled)
            payload: dict[str, Any] = {
                "setup_id": setup.id,
                "symbol": symbol,
                "type": setup.type,
                "direction": setup.direction,
                "htf": used_tf,
                "entry_ltf": used_tf,
                "setup_htf": setup.htf,
                "bar_open_ms": bar_open_ms,
                "entry": level.price,
                "entry_mode": "fib_dca",
                "fib": level.fib,
                "weight_pct": level.weight_pct,
                "filled_weight_pct": total_weight,
                "average_entry": average_entry,
                "entry_index": len(next_filled),
                "entries_max": len(plan),
                "recommended_stop": float(setup.invalidation_price),
                "recommended_stop_source": "htf_invalidation",
                "invalidation_price": float(setup.invalidation_price),
                "target_price": float(setup.entry_target_price),
                "score": setup.score,
                "liberal": setup.is_liberal,
                "signal_discriminator": f"fib:{level.fib}",
            }
            signal_row = await self._notifier.send_event(
                kind=SignalKind.ENTRY,
                payload=payload,
                close_time=bar_open_ms,
                paper_mode=self._cfg.paper_mode.enabled,
                liberal_paper_only=bool(setup.is_liberal),
            )
            if signal_row is None:
                funnel["fib_dca_entry_send_skipped"] += 1
                continue
            self._repo.save_signal(signal_row)
            filled = next_filled
            setup.fib_dca_filled_json = serialize_filled_fibs(filled)
            setup.fib_dca_average_entry = average_entry
            setup.fib_dca_filled_weight_pct = total_weight
            setup.fib_dca_last_fill_ms = bar_open_ms
            setup.entry_count = len(filled)
            setup.last_entry_bar_ms = bar_open_ms
            setup.last_entry_price = level.price
            changed = True
            funnel["fib_dca_entry_sent"] += 1
            funnel[f"fib_dca_entry_{level.fib:g}"] += 1

        target = getattr(setup, "entry_target_price", None)
        if (
            self._cfg.entry.fib_dca.cancel_remaining_on_target
            and target is not None
            and bar_open_ms > int(prepare_since_open_ms(setup))
            and target_reached(
                direction=str(setup.direction),
                target_price=float(target),
                price_low=low,
                price_high=high,
            )
        ):
            setup.state = "CONFIRMED"
            changed = True
            funnel["fib_dca_target_reached"] += 1

        if changed:
            setup.updated_at = utcnow()
            self._repo.upsert_setup(setup)


async def _main() -> None:
    app = SignalBotApp()
    await app.run()


if __name__ == "__main__":
    asyncio.run(_main())
