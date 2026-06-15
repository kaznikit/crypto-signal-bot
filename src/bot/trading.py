from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _direction_sign(direction: str) -> float:
    return 1.0 if direction == "LONG" else -1.0


def _adverse_price(price: float, *, direction: str, rate: float, is_entry: bool) -> float:
    sign = _direction_sign(direction)
    adjustment = float(price) * max(float(rate), 0.0)
    return float(price) + (sign * adjustment if is_entry else -sign * adjustment)


@dataclass(slots=True, frozen=True)
class TradeEntry:
    entry_type: str
    entry_time: int
    signal_price: float
    entry_price: float
    position_size: float
    risk_fraction: float
    fees: float
    slippage: float


@dataclass(slots=True, frozen=True)
class Exit:
    exit_time: int
    exit_price: float
    exit_reason: str


@dataclass(slots=True, frozen=True)
class TradeResult:
    setup_id: str
    symbol: str
    setup_type: str
    direction: str
    tf: str
    entry_type: str
    entry_time: int
    entry_price: float
    position_size: float
    stop_price: float
    tp_price: float
    risk_usd: float
    exit_time: int
    exit_price: float
    exit_reason: str
    realized_pnl: float
    realized_r: float
    gross_r: float
    fees: float
    slippage: float
    funding: float
    mae_r: float
    mfe_r: float
    fib_depth: float | None = None
    bos_or_choch_type: str | None = None
    market_regime: str | None = None
    btc_context: str | None = None
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    setup_id: str
    symbol: str
    setup_type: str
    direction: str
    tf: str
    stop_price: float
    tp_price: float
    risk_usd: float = 1.0
    fee_rate: float = 0.0
    slippage_rate: float = 0.0
    spread_rate: float = 0.0
    funding_rate: float = 0.0
    funding: float = 0.0
    fib_depth: float | None = None
    bos_or_choch_type: str | None = None
    market_regime: str | None = None
    btc_context: str | None = None
    features: dict[str, Any] = field(default_factory=dict)
    entries: list[TradeEntry] = field(default_factory=list)
    mae_r: float = 0.0
    mfe_r: float = 0.0

    @property
    def entry_time(self) -> int:
        return min(entry.entry_time for entry in self.entries)

    @property
    def entry_type(self) -> str:
        return self.entries[-1].entry_type

    @property
    def position_size(self) -> float:
        return sum(entry.position_size for entry in self.entries)

    @property
    def allocated_risk_fraction(self) -> float:
        return sum(entry.risk_fraction for entry in self.entries)

    @property
    def average_entry(self) -> float:
        size = self.position_size
        if size <= 0:
            return 0.0
        return sum(entry.entry_price * entry.position_size for entry in self.entries) / size

    @property
    def entry_fees(self) -> float:
        return sum(entry.fees for entry in self.entries)

    @property
    def entry_slippage(self) -> float:
        return sum(entry.slippage for entry in self.entries)

    def add_entry(
        self,
        *,
        entry_type: str,
        entry_time: int,
        entry_price: float,
        risk_fraction: float,
    ) -> TradeEntry:
        fraction = float(risk_fraction)
        if fraction <= 0:
            raise ValueError("risk_fraction must be positive")
        if self.allocated_risk_fraction + fraction > 1.0 + 1e-9:
            raise ValueError("position risk cannot exceed 1R")

        execution_rate = max(self.slippage_rate, 0.0) + max(self.spread_rate, 0.0) / 2.0
        execution_price = _adverse_price(
            entry_price,
            direction=self.direction,
            rate=execution_rate,
            is_entry=True,
        )
        risk_per_unit = abs(execution_price - self.stop_price)
        if risk_per_unit <= 0:
            raise ValueError("entry and stop must define positive risk")
        size = (self.risk_usd * fraction) / risk_per_unit
        entry = TradeEntry(
            entry_type=entry_type,
            entry_time=int(entry_time),
            signal_price=float(entry_price),
            entry_price=execution_price,
            position_size=size,
            risk_fraction=fraction,
            fees=abs(size * execution_price) * max(self.fee_rate, 0.0),
            slippage=abs(size * (execution_price - float(entry_price))),
        )
        self.entries.append(entry)
        return entry

    def _gross_pnl(self, price: float) -> float:
        sign = _direction_sign(self.direction)
        return sum(
            entry.position_size * sign * (float(price) - entry.entry_price)
            for entry in self.entries
        )

    def update_excursions(self, *, high: float, low: float) -> None:
        if not self.entries or self.risk_usd <= 0:
            return
        favorable = float(high) if self.direction == "LONG" else float(low)
        adverse = float(low) if self.direction == "LONG" else float(high)
        self.mfe_r = max(self.mfe_r, self._gross_pnl(favorable) / self.risk_usd)
        self.mae_r = min(self.mae_r, self._gross_pnl(adverse) / self.risk_usd)

    def resolve_exit(
        self,
        *,
        high: float,
        low: float,
        intrabar_policy: str,
    ) -> tuple[float, str] | None:
        if self.direction == "LONG":
            hit_sl = float(low) <= self.stop_price
            hit_tp = float(high) >= self.tp_price
        else:
            hit_sl = float(high) >= self.stop_price
            hit_tp = float(low) <= self.tp_price
        if not hit_sl and not hit_tp:
            return None
        if hit_sl and hit_tp:
            if intrabar_policy != "tp_first":
                return self.stop_price, "both_hit_same_bar_sl_first"
            return self.tp_price, "both_hit_same_bar_tp_first"
        if hit_sl:
            return self.stop_price, "sl"
        return self.tp_price, "tp"

    def close(self, *, exit_time: int, exit_price: float, exit_reason: str) -> TradeResult:
        if not self.entries:
            raise ValueError("cannot close an empty position")
        trade_exit = Exit(
            exit_time=int(exit_time),
            exit_price=float(exit_price),
            exit_reason=exit_reason,
        )
        execution_rate = max(self.slippage_rate, 0.0) + max(self.spread_rate, 0.0) / 2.0
        execution_price = _adverse_price(
            trade_exit.exit_price,
            direction=self.direction,
            rate=execution_rate,
            is_entry=False,
        )
        gross_pnl = self._gross_pnl(execution_price)
        exit_fee = abs(self.position_size * execution_price) * max(self.fee_rate, 0.0)
        exit_slippage = abs(self.position_size * (execution_price - trade_exit.exit_price))
        fees = self.entry_fees + exit_fee
        slippage = self.entry_slippage + exit_slippage
        entry_notional = sum(abs(entry.position_size * entry.entry_price) for entry in self.entries)
        holding_ms = trade_exit.exit_time - self.entry_time
        funding_intervals = max(0.0, holding_ms / (8 * 60 * 60 * 1000))
        funding = self.funding + entry_notional * max(self.funding_rate, 0.0) * funding_intervals
        realized_pnl = gross_pnl - fees - funding
        risk = self.risk_usd if self.risk_usd > 0 else 1.0
        return TradeResult(
            setup_id=self.setup_id,
            symbol=self.symbol,
            setup_type=self.setup_type,
            direction=self.direction,
            tf=self.tf,
            entry_type=self.entry_type,
            entry_time=self.entry_time,
            entry_price=self.average_entry,
            position_size=self.position_size,
            stop_price=self.stop_price,
            tp_price=self.tp_price,
            risk_usd=self.risk_usd,
            exit_time=trade_exit.exit_time,
            exit_price=execution_price,
            exit_reason=trade_exit.exit_reason,
            realized_pnl=realized_pnl,
            realized_r=realized_pnl / risk,
            gross_r=gross_pnl / risk,
            fees=fees,
            slippage=slippage,
            funding=funding,
            mae_r=self.mae_r,
            mfe_r=self.mfe_r,
            fib_depth=self.fib_depth,
            bos_or_choch_type=self.bos_or_choch_type,
            market_regime=self.market_regime,
            btc_context=self.btc_context,
            features=dict(self.features),
        )
