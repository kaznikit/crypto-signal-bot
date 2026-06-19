from __future__ import annotations

from datetime import UTC, datetime

from bot.storage.models import Trade
from bot.trade_stats import (
    TradeStatsPeriod,
    format_trade_stats_report,
    parse_trade_stats_period,
    summarize_trade_stats,
)


def _trade(
    *,
    trade_id: str,
    symbol: str,
    direction: str,
    setup_type: str,
    entry_time: int,
    exit_time: int,
    realized_r: float,
) -> Trade:
    now = datetime.now(UTC)
    return Trade(
        id=trade_id,
        setup_id=trade_id,
        symbol=symbol,
        direction=direction,
        setup_type=setup_type,
        entry_type="first_entry",
        status="CLOSED",
        entry_time=entry_time,
        entry_price=100.0,
        position_size=1.0,
        stop_price=90.0,
        tp_price=120.0,
        risk_usd=1.0,
        risk_r=1.0,
        exit_time=exit_time,
        exit_price=120.0 if realized_r > 0 else 90.0,
        exit_reason="TP" if realized_r > 0 else "SL",
        realized_pnl=realized_r,
        realized_r=realized_r,
        fees=0.0,
        slippage=0.0,
        funding=0.0,
        mae_r=0.0,
        mfe_r=max(0.0, realized_r),
        entries_json="[]",
        features_json="{}",
        created_at=now,
        updated_at=now,
    )


def test_parse_trade_stats_period_defaults_to_all_history() -> None:
    period = parse_trade_stats_period("")

    assert period.label == "вся доступная история"
    assert period.start_ms is None
    assert period.end_ms is None


def test_parse_trade_stats_period_supports_relative_window() -> None:
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)

    period = parse_trade_stats_period("7d", now=now)

    assert period.label == "последние 7d"
    assert period.start_ms == int(datetime(2026, 6, 12, 12, 0, tzinfo=UTC).timestamp() * 1000)
    assert period.end_ms == int(now.timestamp() * 1000)


def test_parse_trade_stats_period_supports_cyrillic_relative_window() -> None:
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)

    period = parse_trade_stats_period("7д", now=now)

    assert period.label == "последние 7д"
    assert period.start_ms == int(datetime(2026, 6, 12, 12, 0, tzinfo=UTC).timestamp() * 1000)
    assert period.end_ms == int(now.timestamp() * 1000)


def test_parse_trade_stats_period_makes_end_date_inclusive() -> None:
    period = parse_trade_stats_period("2026-06-01 2026-06-19")

    assert period.start_ms == int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000)
    assert period.end_ms == int(datetime(2026, 6, 20, tzinfo=UTC).timestamp() * 1000)


def test_format_trade_stats_report_summarizes_closed_trades() -> None:
    trades = [
        _trade(
            trade_id="1",
            symbol="BTCUSDT",
            direction="LONG",
            setup_type="CONTINUATION",
            entry_time=1_000,
            exit_time=3_601_000,
            realized_r=2.0,
        ),
        _trade(
            trade_id="2",
            symbol="ETHUSDT",
            direction="SHORT",
            setup_type="REVERSAL",
            entry_time=3_602_000,
            exit_time=7_202_000,
            realized_r=-1.0,
        ),
    ]
    summary = summarize_trade_stats(
        trades,
        period=TradeStatsPeriod("test"),
        available_start_ms=3_601_000,
        available_end_ms=7_202_000,
        open_count=1,
    )

    report = format_trade_stats_report(summary)

    assert "Закрытых сделок: 2 | открыто сейчас: 1" in report
    assert "Win/Loss/BE: 1/1/0 | Win rate: 50.0%" in report
    assert "Итог: +1.00R | PnL: +1.00" in report
    assert "По направлениям: LONG 1 (+2.00R), SHORT 1 (-1.00R)" in report


def test_format_trade_stats_report_handles_empty_period() -> None:
    summary = summarize_trade_stats(
        [],
        period=TradeStatsPeriod("test"),
        available_start_ms=None,
        available_end_ms=None,
        open_count=0,
    )

    report = format_trade_stats_report(summary)

    assert "закрытых сделок пока нет" in report
    assert "Нет закрытых сделок" in report
