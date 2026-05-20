from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from pybit.unified_trading import HTTP

INTERVAL_MAP: dict[str, str] = {"5M": "5", "15M": "15", "1H": "60", "4H": "240"}


@dataclass(slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class BybitClient:
    def __init__(
        self,
        category: str = "linear",
        api_key: str | None = None,
        api_secret: str | None = None,
        max_concurrency: int = 5,
    ) -> None:
        self._category = category
        self._http = HTTP(api_key=api_key, api_secret=api_secret, testnet=False)
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def list_top_symbols(self, quote: str, count: int) -> list[str]:
        async with self._semaphore:
            payload: dict[str, Any] = await asyncio.to_thread(
                self._http.get_tickers,
                category=self._category,
            )
        tickers = payload.get("result", {}).get("list", [])
        filtered = [it for it in tickers if it.get("symbol", "").endswith(quote)]
        filtered.sort(key=lambda row: float(row.get("turnover24h", "0")), reverse=True)
        return [item["symbol"] for item in filtered[:count]]

    async def fetch_klines(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        interval = INTERVAL_MAP[timeframe]
        async with self._semaphore:
            payload: dict[str, Any] = await asyncio.to_thread(
                self._http.get_kline,
                category=self._category,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
        rows = payload.get("result", {}).get("list", [])
        rows = list(reversed(rows))
        return [
            Candle(
                open_time=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            )
            for r in rows
        ]
