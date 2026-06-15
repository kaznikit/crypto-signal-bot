import asyncio

from bot.notify.telegram import TelegramNotifier
from bot.storage.models import SignalKind


class _BotStub:
    def __init__(self) -> None:
        self.chat_ids: list[str] = []

    async def send_message(self, *, chat_id, text, disable_web_page_preview) -> None:
        self.chat_ids.append(str(chat_id))


def test_entry_message_contains_confirm_and_entry_index() -> None:
    notifier = TelegramNotifier(bot_token="123456:TEST_TOKEN", chat_id="1")
    msg = notifier._format_message(
        kind=SignalKind.ENTRY,
        payload={
            "type": "CONTINUATION",
            "symbol": "ALTUSDT",
            "direction": "LONG",
            "entry": 0.0074,
            "entry_ltf": "5M",
            "setup_htf": "1H",
            "entry_index": 2,
            "entries_max": 2,
            "confirm_kind": "BOS",
            "confirm_level": 0.00735,
        },
    )
    assert "ENTRY-2 [RE-ENTRY]" in msg
    assert "entry#2/2" in msg
    assert "BOS@0.00735" in msg


def test_entry_message_marks_primary_entry() -> None:
    notifier = TelegramNotifier(bot_token="123456:TEST_TOKEN", chat_id="1")
    msg = notifier._format_message(
        kind=SignalKind.ENTRY,
        payload={
            "type": "REVERSAL",
            "symbol": "BTCUSDT",
            "direction": "SHORT",
            "entry": 106500,
            "entry_ltf": "15M",
            "setup_htf": "4H",
            "entry_index": 1,
            "entries_max": 2,
            "confirm_kind": "CHOCH",
            "confirm_level": 106700,
        },
    )
    assert "ENTRY-1 [PRIMARY]" in msg
    assert "entry#1/2" in msg
    assert "CHOCH@106700" in msg


def test_notifier_routes_prepare_and_entry_to_separate_chats() -> None:
    notifier = TelegramNotifier(
        bot_token="123456:TEST_TOKEN",
        prepare_chat_id="prepare",
        entry_chat_id="entry",
    )
    stub = _BotStub()
    notifier._bot = stub

    async def send_events() -> None:
        await notifier.send_event(
            kind=SignalKind.PREPARE,
            payload={"setup_id": "setup", "symbol": "BTCUSDT"},
            close_time=1,
        )
        await notifier.send_event(
            kind=SignalKind.ENTRY,
            payload={"setup_id": "setup", "symbol": "BTCUSDT"},
            close_time=2,
        )

    asyncio.run(send_events())
    assert stub.chat_ids == ["prepare", "entry"]


def test_notifier_routes_paper_events_to_paper_chat() -> None:
    notifier = TelegramNotifier(
        bot_token="123456:TEST_TOKEN",
        prepare_chat_id="prepare",
        entry_chat_id="entry",
        paper_chat_id="paper",
    )
    stub = _BotStub()
    notifier._bot = stub

    asyncio.run(
        notifier.send_event(
            kind=SignalKind.ENTRY,
            payload={"setup_id": "setup", "symbol": "BTCUSDT"},
            close_time=2,
            paper_mode=True,
        )
    )

    assert stub.chat_ids == ["paper"]
