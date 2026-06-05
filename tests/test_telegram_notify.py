from bot.notify.telegram import TelegramNotifier
from bot.storage.models import SignalKind


def test_prepare_message_uses_vertical_format() -> None:
    notifier = TelegramNotifier(bot_token="123456:TEST_TOKEN", chat_id="1")
    msg = notifier._format_message(
        kind=SignalKind.PREPARE,
        payload={
            "type": "CONTINUATION",
            "symbol": "ALTUSDT",
            "direction": "LONG",
            "invalidation_price": 0.0069,
            "score": 80,
        },
    )

    assert msg.splitlines()[:6] == [
        "PREPARE",
        "ALTUSDT",
        "LONG",
        "invalidate 0.0069",
        "isReentry false",
        "Score 80",
    ]


def test_entry_message_marks_reentry_vertically() -> None:
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
            "invalidation_price": 0.0069,
            "score": 70,
            "confirm_kind": "BOS",
            "confirm_level": 0.00735,
        },
    )
    assert msg.splitlines()[:6] == [
        "ENTRY",
        "ALTUSDT",
        "LONG",
        "invalidate 0.0069",
        "isReentry true",
        "Score 70",
    ]


def test_entry_message_marks_primary_entry_vertically() -> None:
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
            "invalidation_price": 107000,
            "score": 60,
            "confirm_kind": "CHOCH",
            "confirm_level": 106700,
        },
    )
    assert msg.splitlines()[:6] == [
        "ENTRY",
        "BTCUSDT",
        "SHORT",
        "invalidate 107000",
        "isReentry false",
        "Score 60",
    ]
