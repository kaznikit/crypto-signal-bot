from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


def _is_candle_close(now: datetime, timeframe: str) -> bool:
    if timeframe == "5M":
        return now.minute % 5 == 0
    if timeframe == "15M":
        return now.minute % 15 == 0
    if timeframe == "1H":
        return now.minute == 0
    if timeframe == "4H":
        return now.minute == 0 and now.hour % 4 == 0
    return False


class TimeframeScheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def add_tick_job(self, callback) -> None:
        self._scheduler.add_job(callback, trigger="cron", minute="*")

    def start(self) -> None:
        self._scheduler.start()
        logger.info("Scheduler started")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler shutdown")

    @staticmethod
    def closed_timeframes(now: datetime | None = None) -> list[str]:
        current = now or datetime.now(tz=UTC)
        return [tf for tf in ("4H", "1H", "15M", "5M") if _is_candle_close(current, tf)]
