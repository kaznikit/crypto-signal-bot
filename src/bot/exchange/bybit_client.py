from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pybit.unified_trading import HTTP

INTERVAL_MAP: dict[str, str] = {"1M": "1", "5M": "5", "15M": "15", "1H": "60", "4H": "240"}
INTERVAL_MS_MAP: dict[str, int] = {
    "1M": 60 * 1000,
    "5M": 5 * 60 * 1000,
    "15M": 15 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}

PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
)


def _now_utc_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


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
        self._disabled_env_proxy_retry = False

    @staticmethod
    def _wrong_ssl_version(exc: Exception) -> bool:
        return "WRONG_VERSION_NUMBER" in str(exc).upper()

    @staticmethod
    def _proxy_env_present() -> bool:
        return any(os.environ.get(key) for key in PROXY_ENV_KEYS)

    def _disable_env_proxy_for_retry(self) -> bool:
        client = getattr(self._http, "client", None)
        if client is None or self._disabled_env_proxy_retry:
            return False
        if not getattr(client, "trust_env", True):
            return False
        client.trust_env = False
        self._disabled_env_proxy_retry = True
        return True

    async def _call_http(self, fn: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:
            if (
                self._wrong_ssl_version(exc)
                and self._proxy_env_present()
                and self._disable_env_proxy_for_retry()
            ):
                return await asyncio.to_thread(fn, **kwargs)
            raise

    async def list_top_symbols(self, quote: str, count: int) -> list[str]:
        async with self._semaphore:
            payload: dict[str, Any] = await self._call_http(
                self._http.get_tickers,
                category=self._category,
            )
        tickers = payload.get("result", {}).get("list", [])
        filtered = [it for it in tickers if it.get("symbol", "").endswith(quote)]
        filtered.sort(key=lambda row: float(row.get("turnover24h", "0")), reverse=True)
        return [item["symbol"] for item in filtered[:count]]

    async def fetch_klines(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        interval = INTERVAL_MAP[timeframe]
        if limit <= 0:
            return []

        all_rows: list[list[Any]] = []
        remaining = int(limit)
        end: int | None = None

        while remaining > 0:
            page_limit = min(1000, remaining)
            req_kwargs: dict[str, Any] = {
                "category": self._category,
                "symbol": symbol,
                "interval": interval,
                "limit": page_limit,
            }
            if end is not None:
                req_kwargs["end"] = end

            async with self._semaphore:
                payload: dict[str, Any] = await self._call_http(
                    self._http.get_kline,
                    **req_kwargs,
                )

            rows = payload.get("result", {}).get("list", [])
            if not rows:
                break
            all_rows.extend(rows)
            remaining -= len(rows)

            oldest_open_time = min(int(r[0]) for r in rows)
            end = oldest_open_time - 1
            if len(rows) < page_limit:
                break

        # Bybit returns rows in reverse order; pages may overlap on boundaries.
        dedup: dict[int, list[Any]] = {}
        for row in all_rows:
            dedup[int(row[0])] = row

        candles = [
            Candle(
                open_time=open_time,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for open_time, row in sorted(dedup.items())
        ]
        # Bybit часто отдаёт формирующийся текущий бар. Для детерминизма стратегии
        # работаем только с полностью закрытыми свечами.
        tf_ms = INTERVAL_MS_MAP[timeframe]
        if candles and (candles[-1].open_time + tf_ms > _now_utc_ms()):
            candles = candles[:-1]
        return candles
