from __future__ import annotations

import json
import logging
from hashlib import sha256
from typing import Any

from aiogram import Bot

from bot.storage.models import Signal, SignalKind
from bot.util.time import utcnow

logger = logging.getLogger(__name__)

TV_INTERVAL_MAP: dict[str, str] = {"5M": "5", "15M": "15", "1H": "60", "4H": "240"}

Payload = dict[str, Any]


def build_tv_link(symbol: str, tf: str, exchange: str = "BYBIT") -> str:
    interval = TV_INTERVAL_MAP.get(tf, "240")
    return f"https://www.tradingview.com/chart/?symbol={exchange}:{symbol}&interval={interval}"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, paper_chat_id: str | None = None) -> None:
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        self._paper_chat_id = paper_chat_id

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

        if liberal_paper_only:
            if not self._paper_chat_id:
                logger.warning("Liberal-only signal skipped: TG_PAPER_CHAT_ID is not set")
                return None
            target_chat = self._paper_chat_id
        else:
            target_chat = (
                self._paper_chat_id if paper_mode and self._paper_chat_id else self._chat_id
            )

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
            ote = f"{payload.get('ote_low')}-{payload.get('ote_high')}"
            return (
                f"{prefix}PREPARE {payload.get('type', '')} {symbol} {direction}\n"
                f"origin={payload.get('origin_price')} ote={ote}\n"
                f"invalid={payload.get('invalidation_price')} "
                f"score={payload.get('score', 0)}{tv_line}"
            )
        if kind == SignalKind.ENTRY:
            entry = payload.get("entry", payload.get("origin_price"))
            sl = payload.get("sl")
            tp = payload.get("tp1")
            extra = f" SL={sl} TP1={tp}" if sl is not None and tp is not None else ""
            entry_tf = payload.get("entry_ltf") or payload.get("htf")
            setup_tf = payload.get("setup_htf", "")
            tf_note = f" confirm={entry_tf} setup={setup_tf}" if entry_tf else ""
            return (
                f"{prefix}ENTRY {payload.get('type', '')} {symbol} {direction} @ {entry}"
                f"{extra}{tf_note}{tv_line}"
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
