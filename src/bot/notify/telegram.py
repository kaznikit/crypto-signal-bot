from __future__ import annotations

import json
import logging
from hashlib import sha256
from typing import Any

from aiogram import Bot

from bot.entry_identity import entry_point_label
from bot.storage.models import Signal, SignalKind
from bot.util.time import utcnow

logger = logging.getLogger(__name__)

TV_INTERVAL_MAP: dict[str, str] = {"1M": "1", "5M": "5", "15M": "15", "1H": "60", "4H": "240"}

Payload = dict[str, Any]


def build_tv_link(symbol: str, tf: str, exchange: str = "BYBIT") -> str:
    interval = TV_INTERVAL_MAP.get(tf, "240")
    return f"https://www.tradingview.com/chart/?symbol={exchange}:{symbol}&interval={interval}"


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str | None = None,
        prepare_chat_id: str | None = None,
        entry_chat_id: str | None = None,
        paper_chat_id: str | None = None,
        route_paper_mode_to_paper_chat: bool = False,
    ) -> None:
        self._bot = Bot(token=bot_token)
        resolved_prepare_chat_id = prepare_chat_id or chat_id
        resolved_entry_chat_id = entry_chat_id or chat_id
        if not resolved_prepare_chat_id:
            raise ValueError("TG_PREPARE_CHAT_ID or TG_CHAT_ID is required")
        if not resolved_entry_chat_id:
            raise ValueError("TG_ENTRY_CHAT_ID or TG_CHAT_ID is required")
        self._prepare_chat_id = resolved_prepare_chat_id
        self._entry_chat_id = resolved_entry_chat_id
        self._paper_chat_id = paper_chat_id
        self._route_paper_mode_to_paper_chat = route_paper_mode_to_paper_chat

    def _target_chat(
        self,
        paper_mode: bool,
        liberal_paper_only: bool = False,
        kind: SignalKind | None = None,
        payload: Payload | None = None,
    ) -> str | None:
        if liberal_paper_only:
            return self._paper_chat_id
        if paper_mode and self._paper_chat_id and self._route_paper_mode_to_paper_chat:
            return self._paper_chat_id
        if kind == SignalKind.ENTRY or (
            kind == SignalKind.INVALIDATED and bool((payload or {}).get("after_entry"))
        ):
            return self._entry_chat_id
        return self._prepare_chat_id

    @staticmethod
    def build_signal_id(
        setup_id: str,
        kind: str,
        close_time: int,
        discriminator: str | None = None,
    ) -> str:
        raw = f"{setup_id}:{kind}:{close_time}"
        if discriminator is not None:
            raw = f"{raw}:{discriminator}"
        return sha256(raw.encode("ascii")).hexdigest()

    async def send_event(
        self,
        kind: SignalKind,
        payload: Payload,
        close_time: int,
        paper_mode: bool = False,
        liberal_paper_only: bool = False,
    ) -> Signal | None:
        setup_id = str(payload.get("setup_id", "system"))
        discriminator = payload.get("signal_discriminator")
        signal_id = self.build_signal_id(
            setup_id,
            kind.value,
            close_time,
            str(discriminator) if discriminator is not None else None,
        )
        liberal = bool(payload.get("liberal")) or liberal_paper_only
        message = self._format_message(kind=kind, payload=payload, liberal=liberal)

        target_chat = self._target_chat(
            paper_mode=paper_mode,
            liberal_paper_only=liberal_paper_only,
            kind=kind,
            payload=payload,
        )
        if target_chat is None:
            logger.warning("Liberal-only signal skipped: TG_PAPER_CHAT_ID is not set")
            return None

        await self._bot.send_message(
            chat_id=target_chat,
            text=message,
            disable_web_page_preview=True,
        )
        return Signal(
            id=signal_id,
            setup_id=setup_id,
            kind=kind.value,
            payload_json=json.dumps(payload, ensure_ascii=True),
            sent_at=utcnow(),
        )

    async def send_entry_stats(self, text: str, paper_mode: bool = False) -> None:
        target_chat = self._target_chat(paper_mode=paper_mode, kind=SignalKind.ENTRY)
        assert target_chat is not None
        await self._bot.send_message(
            chat_id=target_chat,
            text=text,
            disable_web_page_preview=True,
        )

    async def send_prepare_stats(self, text: str, paper_mode: bool = False) -> None:
        target_chat = self._target_chat(paper_mode=paper_mode, kind=SignalKind.PREPARE)
        assert target_chat is not None
        await self._bot.send_message(
            chat_id=target_chat,
            text=text,
            disable_web_page_preview=True,
        )

    def _tv_link_for_payload(self, payload: Payload) -> str | None:
        symbol = payload.get("symbol")
        tf = payload.get("htf") or payload.get("tf")
        if not symbol or not tf:
            return None
        return build_tv_link(str(symbol), str(tf))

    def _format_message(self, kind: SignalKind, payload: Payload, liberal: bool = False) -> str:
        prefix = "[LIBERAL] " if liberal else ""
        symbol = payload.get("symbol", "N/A")
        direction = payload.get("direction", "N/A")
        tv = self._tv_link_for_payload(payload)
        tv_line = f"\nTV: {tv}" if tv else ""
        if kind == SignalKind.PREPARE:
            is_reentry = bool(payload.get("is_reentry", False))
            lines = [
                f"{prefix}PREPARE",
                str(symbol),
                str(direction),
                f"invalidate {payload.get('invalidation_price')}",
            ]
            if payload.get("entry_mode") == "fib_dca":
                for level in payload.get("fib_dca_levels") or []:
                    lines.append(
                        f"fib {level.get('fib')} | weight {level.get('weight_pct')}% "
                        f"| price {level.get('price')}"
                    )
            lines.extend(
                [
                    f"isReentry {str(is_reentry).lower()}",
                    f"Score {payload.get('score', 0)}{tv_line}",
                ]
            )
            if payload.get("entry_modes"):
                lines.append(f"entryModes {', '.join(payload['entry_modes'])}")
            return "\n".join(lines)
        if kind == SignalKind.ENTRY:
            entry_idx = payload.get("entry_index")
            idx_num: int | None = None
            if entry_idx is not None:
                try:
                    idx_num = int(entry_idx)
                except (TypeError, ValueError):
                    idx_num = None
            is_reentry = bool(idx_num is not None and idx_num > 1)
            point = entry_point_label(payload)
            lines = [
                f"{prefix}ENTRY [{point}]",
                str(symbol),
                str(direction),
                f"entry {payload.get('entry')}",
            ]
            if payload.get("recommended_stop") is not None:
                lines.append(f"stop {payload.get('recommended_stop')}")
            if payload.get("recommended_stop_source") is not None:
                lines.append(f"stopSource {payload.get('recommended_stop_source')}")
            lines.append(f"setupInvalidation {payload.get('invalidation_price')}")
            if payload.get("fib") is not None:
                lines.append(f"fib {payload.get('fib')}")
            if payload.get("weight_pct") is not None:
                lines.append(f"weight {payload.get('weight_pct')}%")
            if payload.get("filled_weight_pct") is not None:
                lines.append(f"filled {payload.get('filled_weight_pct')}%")
            if payload.get("average_entry") is not None:
                lines.append(f"averageEntry {payload.get('average_entry')}")
            target = payload.get("target_price", payload.get("tp"))
            if target is not None:
                lines.append(f"target {target}")
            lines.extend(
                [
                    f"mode {payload.get('entry_mode', 'simple')}",
                    f"variant {payload.get('entry_variant', payload.get('entry_mode', 'simple'))}",
                    f"isReentry {str(is_reentry).lower()}",
                    f"Score {payload.get('score', 0)}{tv_line}",
                ]
            )
            return "\n".join(lines)
        if kind == SignalKind.INVALIDATED:
            if payload.get("after_entry"):
                return f"{prefix}STOP [{entry_point_label(payload)}] {symbol}{tv_line}"
            return f"{prefix}INVALIDATED {payload.get('type', '')} {symbol}{tv_line}"
        return "HEARTBEAT bot is alive"

    async def send_heartbeat(self, paper_mode: bool = False) -> None:
        await self.send_event(
            SignalKind.HEARTBEAT,
            payload={"setup_id": "system"},
            close_time=0,
            paper_mode=paper_mode,
        )
