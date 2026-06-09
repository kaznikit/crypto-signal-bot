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


def test_prepare_message_lists_fib_dca_plan() -> None:
    notifier = TelegramNotifier(bot_token="123456:TEST_TOKEN", chat_id="1")
    msg = notifier._format_message(
        kind=SignalKind.PREPARE,
        payload={
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "invalidation_price": 90,
            "entry_mode": "fib_dca",
            "fib_dca_levels": [
                {"fib": 0.5, "weight_pct": 40, "price": 100},
                {"fib": 0.618, "weight_pct": 30, "price": 97.64},
            ],
        },
    )

    assert "fib 0.5 | weight 40% | price 100" in msg
    assert "fib 0.618 | weight 30% | price 97.64" in msg


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
            "recommended_stop": 0.0071,
            "entry_mode": "advanced",
        },
    )
    assert msg.splitlines()[:8] == [
        "ENTRY",
        "ALTUSDT",
        "LONG",
        "entry 0.0074",
        "recommendedStop 0.0071",
        "invalidate 0.0069",
        "mode advanced",
        "isReentry true",
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
            "recommended_stop": 106850,
            "entry_mode": "simple",
        },
    )
    assert msg.splitlines()[:8] == [
        "ENTRY",
        "BTCUSDT",
        "SHORT",
        "entry 106500",
        "recommendedStop 106850",
        "invalidate 107000",
        "mode simple",
        "isReentry false",
    ]


def test_signal_id_stays_backward_compatible_without_discriminator() -> None:
    legacy = TelegramNotifier.build_signal_id("setup", "ENTRY", 1_000)
    fib_a = TelegramNotifier.build_signal_id("setup", "ENTRY", 1_000, "fib:0.5")
    fib_b = TelegramNotifier.build_signal_id("setup", "ENTRY", 1_000, "fib:0.618")

    assert legacy == TelegramNotifier.build_signal_id("setup", "ENTRY", 1_000)
    assert len({legacy, fib_a, fib_b}) == 3
