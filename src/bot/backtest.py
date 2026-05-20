from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(slots=True)
class BacktestResult:
    trades: int
    wins: int
    losses: int
    avg_r: float
    max_drawdown_r: float


def run_placeholder_backtest(data: list[dict[str, float]]) -> BacktestResult:
    """
    Minimal harness: converts candles and returns baseline metrics.
    Replace trade simulation with FSM replay for production use.
    """
    df = pd.DataFrame(data)
    if df.empty:
        return BacktestResult(trades=0, wins=0, losses=0, avg_r=0.0, max_drawdown_r=0.0)
    returns = (df["close"].pct_change().fillna(0) * 10).clip(-1.0, 1.0)
    wins = int((returns > 0).sum())
    losses = int((returns < 0).sum())
    equity = returns.cumsum()
    drawdown = (equity.cummax() - equity).max()
    return BacktestResult(
        trades=int(len(returns)),
        wins=wins,
        losses=losses,
        avg_r=float(returns.mean()),
        max_drawdown_r=float(drawdown),
    )
