from __future__ import annotations

import json
import logging
from hashlib import sha256
from typing import Any

from aiogram import Bot

from bot.storage.models import Signal, SignalKind
from bot.util.time import utcnow

logger = logging.getLogger(__name__)

TV_INTERVAL_MAP: dict[str, str] = {"1M": "1", "5M": "5", "15M": "15", "1H": "60", "4H": "240"}

Payload = dict[str, Any]


def build_tv_link(symbol: str, tf: str, exchange: str = "BYBIT") -> str:
    interval = TV_INTERVAL_MAP.get(tf, "240")
    return f"https://www.tradingview.com/chart/?symbol={exchange}:{symbol}&interval={interval}"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, paper_chat_id: str | None = None) -> None:
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        self._paper_chat_id = paper_chat_id

    def _target_chat(self, paper_mode: bool, liberal_paper_only: bool = False) -> str | None:
        if liberal_paper_only:
            return self._paper_chat_id
        return self._paper_chat_id if paper_mode and self._paper_chat_id else self._chat_id

    @staticmethod
    def build_signal_id(setup_id: str, kind: str, close_time: int) -> str:
        raw = f"{setup_id}:{kind}:{close_time}"
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
        signal_id = self.build_signal_id(setup_id, kind.value, close_time)
        liberal = bool(payload.get("liberal")) or liberal_paper_only
        message = self._format_message(kind=kind, payload=payload, liberal=liberal)

        target_chat = self._target_chat(
            paper_mode=paper_mode,
            liberal_paper_only=liberal_paper_only,
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
        target_chat = self._target_chat(paper_mode=paper_mode)
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
            return (
                f"{prefix}PREPARE\n"
                f"{symbol}\n"
                f"{direction}\n"
                f"invalidate {payload.get('invalidation_price')}\n"
                f"isReentry {str(is_reentry).lower()}\n"
                f"Score {payload.get('score', 0)}{tv_line}"
            )
        if kind == SignalKind.ENTRY:
            entry_idx = payload.get("entry_index")
            idx_num: int | None = None
            if entry_idx is not None:
                try:
                    idx_num = int(entry_idx)
                except (TypeError, ValueError):
                    idx_num = None
            is_reentry = bool(idx_num is not None and idx_num > 1)
            return (
                f"{prefix}ENTRY\n"
                f"{symbol}\n"
                f"{direction}\n"
                f"invalidate {payload.get('invalidation_price')}\n"
                f"isReentry {str(is_reentry).lower()}\n"
                f"Score {payload.get('score', 0)}{tv_line}"
            )
        if kind == SignalKind.INVALIDATED:
            return f"{prefix}INVALIDATED {payload.get('type', '')} {symbol}{tv_line}"
        return "HEARTBEAT bot is alive"

    async def send_heartbeat(self, paper_mode: bool = False) -> None:
        await self.send_event(
            SignalKind.HEARTBEAT,
            payload={"setup_id": "system"},
            close_time=0,
            paper_mode=paper_mode,
        )
