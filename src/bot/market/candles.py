from __future__ import annotations

import pandas as pd

from bot.exchange.bybit_client import Candle


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open_time": c.open_time,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
    )
