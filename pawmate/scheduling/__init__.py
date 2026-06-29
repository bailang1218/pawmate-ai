"""Scheduling package: APScheduler-based cron task system."""

from __future__ import annotations

import logging
from typing import Optional

_logger = logging.getLogger("pawmate")


def init_scheduler() -> Optional[object]:
    """Initialize and return the scheduler; tool registration happens outside."""
    try:
        from pawmate.scheduling.scheduler import PawScheduler

        scheduler = PawScheduler()
        _logger.info("[Scheduler] initialized")
        return scheduler
    except Exception as e:
        _logger.warning("[Scheduler] init failed: %s", e)
        return None
