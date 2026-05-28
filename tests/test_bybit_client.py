import asyncio

import bot.exchange.bybit_client as bybit_client_module
from bot.exchange.bybit_client import BybitClient


def _row(open_time: int, price: float) -> list[str]:
    return [
        str(open_time),
        str(price),
        str(price + 0.001),
        str(price - 0.001),
        str(price + 0.0005),
        "1000",
    ]


class _FakeHTTP:
    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows

    def get_kline(self, **_kwargs):
        return {"result": {"list": self._rows}}


def test_fetch_klines_drops_forming_tail_candle(monkeypatch) -> None:
    open0 = 1_000_000_000_000
    open1 = open0 + 300_000
    open2 = open1 + 300_000
    rows_desc = [_row(open2, 2.0), _row(open1, 1.5), _row(open0, 1.0)]

    client = BybitClient()
    client._http = _FakeHTTP(rows_desc)
    monkeypatch.setattr(bybit_client_module, "_now_utc_ms", lambda: open2 + 120_000)

    candles = asyncio.run(client.fetch_klines(symbol="ALTUSDT", timeframe="5M", limit=3))
    assert [c.open_time for c in candles] == [open0, open1]


def test_fetch_klines_keeps_last_candle_when_it_is_closed(monkeypatch) -> None:
    open0 = 1_000_000_000_000
    open1 = open0 + 300_000
    rows_desc = [_row(open1, 1.5), _row(open0, 1.0)]

    client = BybitClient()
    client._http = _FakeHTTP(rows_desc)
    monkeypatch.setattr(bybit_client_module, "_now_utc_ms", lambda: open1 + 300_000)

    candles = asyncio.run(client.fetch_klines(symbol="ALTUSDT", timeframe="5M", limit=2))
    assert [c.open_time for c in candles] == [open0, open1]
