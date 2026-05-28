from bot.notify.telegram import TelegramNotifier
from bot.storage.models import SignalKind


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
