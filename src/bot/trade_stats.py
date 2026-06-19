from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean, median

from bot.storage.models import Trade
from bot.util.time import ensure_utc, utcnow

_RELATIVE_PERIOD_RE = re.compile(r"^(?P<count>\d+)(?P<unit>[hdwmчднм])$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RELATIVE_UNIT_ALIASES = {
    "ч": "h",
    "д": "d",
    "н": "w",
    "м": "m",
}


class TradeStatsParseError(ValueError):
    pass


@dataclass(frozen=True)
class TradeStatsPeriod:
    label: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class TradeStatsGroup:
    name: str
    count: int
    total_r: float


@dataclass(frozen=True)
class TradeStatsSummary:
    period: TradeStatsPeriod
    available_start_ms: int | None
    available_end_ms: int | None
    open_count: int
    closed_count: int
    wins: int
    losses: int
    breakeven: int
    total_r: float
    total_pnl: float
    avg_r: float
    median_r: float
    avg_win_r: float | None
    avg_loss_r: float | None
    profit_factor: float | None
    max_drawdown_r: float
    best_r: float | None
    worst_r: float | None
    avg_holding_ms: float | None
    by_direction: tuple[TradeStatsGroup, ...]
    by_setup_type: tuple[TradeStatsGroup, ...]
    top_symbols: tuple[TradeStatsGroup, ...]


def parse_trade_stats_period(args: str, now: datetime | None = None) -> TradeStatsPeriod:
    now = ensure_utc(now or utcnow())
    args = " ".join(args.strip().split())
    if not args or args.lower() in {"all", "all-time", "все", "вся", "alltime"}:
        return TradeStatsPeriod(label="вся доступная история")

    tokens = args.split()
    if len(tokens) > 2:
        raise TradeStatsParseError("Не понял период для статистики сделок.")

    if len(tokens) == 1:
        token = tokens[0].lower()
        if token in {"today", "сегодня"}:
            start = datetime.combine(now.date(), time.min, tzinfo=UTC)
            return TradeStatsPeriod(
                label="сегодня UTC",
                start_ms=_to_ms(start),
                end_ms=_to_ms(now),
            )
        if token in {"yesterday", "вчера"}:
            end = datetime.combine(now.date(), time.min, tzinfo=UTC)
            start = end - timedelta(days=1)
            return TradeStatsPeriod(
                label="вчера UTC",
                start_ms=_to_ms(start),
                end_ms=_to_ms(end),
            )
        if token in {"week", "неделя"}:
            return _relative_period(now, 7, "d", "последние 7d")
        if token in {"month", "месяц"}:
            return _relative_period(now, 30, "d", "последние 30d")

        match = _RELATIVE_PERIOD_RE.match(token)
        if match:
            count = int(match.group("count"))
            unit = _RELATIVE_UNIT_ALIASES.get(match.group("unit"), match.group("unit"))
            return _relative_period(now, count, unit, f"последние {token}")

        start = _parse_datetime(tokens[0], end_of_date=False)
        return TradeStatsPeriod(
            label=f"с {_format_dt(start)}",
            start_ms=_to_ms(start),
            end_ms=_to_ms(now),
        )

    start = _parse_datetime(tokens[0], end_of_date=False)
    end = _parse_datetime(tokens[1], end_of_date=True)
    if end <= start:
        raise TradeStatsParseError("Конец периода должен быть позже начала.")
    return TradeStatsPeriod(
        label=f"{_format_dt(start)} - {_format_dt(end)}",
        start_ms=_to_ms(start),
        end_ms=_to_ms(end),
    )


def trade_stats_usage() -> str:
    return "\n".join(
        [
            "Команда:",
            "/trade_stats",
            "/trade_stats 7d",
            "/trade_stats today",
            "/trade_stats 2026-06-01",
            "/trade_stats 2026-06-01 2026-06-19",
            "Период считается по времени закрытия сделки, даты трактуются как UTC.",
        ]
    )


def summarize_trade_stats(
    trades: Iterable[Trade],
    *,
    period: TradeStatsPeriod,
    available_start_ms: int | None,
    available_end_ms: int | None,
    open_count: int,
) -> TradeStatsSummary:
    rows = sorted(
        [trade for trade in trades if trade.status == "CLOSED" and trade.exit_time is not None],
        key=lambda trade: (int(trade.exit_time or 0), str(trade.id)),
    )
    r_values = [_realized_r(row) for row in rows]
    pnl_values = [float(row.realized_pnl or 0.0) for row in rows]
    wins = sum(1 for value in r_values if value > 1e-12)
    losses = sum(1 for value in r_values if value < -1e-12)
    breakeven = len(r_values) - wins - losses
    win_r = [value for value in r_values if value > 1e-12]
    loss_r = [value for value in r_values if value < -1e-12]
    gross_win = sum(win_r)
    gross_loss = abs(sum(loss_r))
    profit_factor = None
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")

    holding_periods = [
        float(row.exit_time - row.entry_time)
        for row in rows
        if row.exit_time is not None and row.exit_time >= row.entry_time
    ]

    return TradeStatsSummary(
        period=period,
        available_start_ms=available_start_ms,
        available_end_ms=available_end_ms,
        open_count=open_count,
        closed_count=len(rows),
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        total_r=sum(r_values),
        total_pnl=sum(pnl_values),
        avg_r=mean(r_values) if r_values else 0.0,
        median_r=median(r_values) if r_values else 0.0,
        avg_win_r=mean(win_r) if win_r else None,
        avg_loss_r=mean(loss_r) if loss_r else None,
        profit_factor=profit_factor,
        max_drawdown_r=_max_drawdown(r_values),
        best_r=max(r_values) if r_values else None,
        worst_r=min(r_values) if r_values else None,
        avg_holding_ms=mean(holding_periods) if holding_periods else None,
        by_direction=_group_trades(rows, lambda row: str(row.direction)),
        by_setup_type=_group_trades(rows, lambda row: str(row.setup_type)),
        top_symbols=_group_trades(rows, lambda row: str(row.symbol), limit=5),
    )


def format_trade_stats_report(summary: TradeStatsSummary) -> str:
    lines = [
        "TRADE STATS",
        f"Период: {summary.period.label}",
        f"Данные в БД: {_format_available_range(summary)}",
        f"Закрытых сделок: {summary.closed_count} | открыто сейчас: {summary.open_count}",
    ]
    if summary.closed_count == 0:
        lines.append("Нет закрытых сделок по времени закрытия в выбранном периоде.")
        return "\n".join(lines)

    win_rate = summary.wins / summary.closed_count * 100.0
    lines.extend(
        [
            (
                f"Win/Loss/BE: {summary.wins}/{summary.losses}/{summary.breakeven} "
                f"| Win rate: {win_rate:.1f}%"
            ),
            (
                f"Итог: {_fmt_signed(summary.total_r)}R "
                f"| PnL: {_fmt_signed(summary.total_pnl)}"
            ),
            (
                f"Avg: {_fmt_signed(summary.avg_r)}R "
                f"| Median: {_fmt_signed(summary.median_r)}R "
                f"| PF: {_format_profit_factor(summary.profit_factor)}"
            ),
            (
                f"Best/Worst: {_format_optional_r(summary.best_r)} / "
                f"{_format_optional_r(summary.worst_r)} "
                f"| Max DD: {_fmt_signed(summary.max_drawdown_r)}R"
            ),
        ]
    )
    if summary.avg_win_r is not None or summary.avg_loss_r is not None:
        lines.append(
            "Avg win/loss: "
            f"{_format_optional_r(summary.avg_win_r)} / {_format_optional_r(summary.avg_loss_r)}"
        )
    if summary.avg_holding_ms is not None:
        lines.append(f"Среднее удержание: {_format_duration_ms(summary.avg_holding_ms)}")
    if summary.by_direction:
        lines.append(f"По направлениям: {_format_groups(summary.by_direction)}")
    if summary.by_setup_type:
        lines.append(f"По типам: {_format_groups(summary.by_setup_type)}")
    if summary.top_symbols:
        lines.append(f"Top symbols: {_format_groups(summary.top_symbols)}")
    return "\n".join(lines)


def _relative_period(
    now: datetime,
    count: int,
    unit: str,
    label: str,
) -> TradeStatsPeriod:
    if count <= 0:
        raise TradeStatsParseError("Период должен быть положительным.")
    multipliers = {
        "h": timedelta(hours=count),
        "d": timedelta(days=count),
        "w": timedelta(weeks=count),
        "m": timedelta(days=30 * count),
    }
    delta = multipliers[unit]
    return TradeStatsPeriod(
        label=label,
        start_ms=_to_ms(now - delta),
        end_ms=_to_ms(now),
    )


def _parse_datetime(value: str, *, end_of_date: bool) -> datetime:
    try:
        if _DATE_ONLY_RE.match(value):
            parsed_date = date.fromisoformat(value)
            parsed = datetime.combine(parsed_date, time.min, tzinfo=UTC)
            return parsed + timedelta(days=1) if end_of_date else parsed
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradeStatsParseError(f"Не понял дату `{value}`.") from exc
    return ensure_utc(parsed)


def _to_ms(value: datetime) -> int:
    return int(ensure_utc(value).timestamp() * 1000)


def _realized_r(trade: Trade) -> float:
    if trade.realized_r is not None:
        return float(trade.realized_r)
    if trade.realized_pnl is None or not trade.risk_usd:
        return 0.0
    return float(trade.realized_pnl) / float(trade.risk_usd)


def _max_drawdown(r_values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in r_values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return max_drawdown


def _group_trades(
    trades: list[Trade],
    key_fn: Callable[[Trade], str],
    *,
    limit: int | None = None,
) -> tuple[TradeStatsGroup, ...]:
    values: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        values[key_fn(trade)].append(_realized_r(trade))
    groups = [
        TradeStatsGroup(name=name, count=len(items), total_r=sum(items))
        for name, items in values.items()
    ]
    groups.sort(key=lambda group: (-group.count, group.name))
    if limit is not None:
        groups = groups[:limit]
    return tuple(groups)


def _format_available_range(summary: TradeStatsSummary) -> str:
    if summary.available_start_ms is None or summary.available_end_ms is None:
        return "закрытых сделок пока нет"
    return (
        f"{_format_ms(summary.available_start_ms)} - "
        f"{_format_ms(summary.available_end_ms)} UTC"
    )


def _format_dt(value: datetime) -> str:
    return ensure_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def _format_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _fmt_signed(value: float) -> str:
    return f"{value:+.2f}"


def _format_optional_r(value: float | None) -> str:
    return "n/a" if value is None else f"{_fmt_signed(value)}R"


def _format_profit_factor(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value == float("inf"):
        return "inf"
    return f"{value:.2f}"


def _format_duration_ms(value: float) -> str:
    total_minutes = max(0, int(round(value / 60000)))
    days, day_remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(day_remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_groups(groups: tuple[TradeStatsGroup, ...]) -> str:
    return ", ".join(
        f"{group.name} {group.count} ({_fmt_signed(group.total_r)}R)" for group in groups
    )
