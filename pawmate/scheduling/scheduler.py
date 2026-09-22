"""
APScheduler-based task scheduler for PawMate.

Persistence: SQLAlchemyJobStore → sessions.db (shared with other modules).
AsyncIOScheduler — runs on the same asyncio event loop as the engine.
"""
import logging
from typing import Optional, Callable, Dict, List, Awaitable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.executors.asyncio import AsyncIOExecutor
from pawmate.storage.app_paths import get_app_paths

logger = logging.getLogger("pawmate.scheduler")

TaskFireCallback = Callable[[str, str], Awaitable[None]]

# Module-level callback registry (avoids pickling bound methods).
_callbacks: Dict[str, TaskFireCallback] = {}


async def _fire_impl(task_id: str, action: str) -> None:
    """Module-level function: looks up the callback by task_id prefix."""
    # Callbacks are registered under a fixed key "default"
    cb = _callbacks.get("default")
    if cb is None:
        logger.warning("[Scheduler] no callback registered, drop %s", task_id)
        return
    try:
        await cb(task_id, action)
    except Exception:
        logger.exception("[Scheduler] task %s callback raised", task_id)


class PawScheduler:
    """定时任务调度器。持久化到 SQLite，重启后继续等。"""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        timezone: str = "Asia/Shanghai",
    ):
        if db_path is None:
            db_path = get_app_paths().ensure_database_path()
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        url = f"sqlite:///{self.db_path}"
        jobstores = {
            "default": SQLAlchemyJobStore(url=url, tablename="scheduled_jobs"),
        }
        executors = {"default": AsyncIOExecutor()}
        job_defaults = {
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60,
        }
        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone=timezone,
        )
        self._started = False

    def set_callback(self, callback: TaskFireCallback) -> None:
        _callbacks["default"] = callback

    def start(self) -> None:
        if self._started:
            return
        self.scheduler.start()
        self._started = True
        jobs = self.scheduler.get_jobs()
        logger.info("[Scheduler] started, %d pending jobs", len(jobs))
        for job in jobs:
            logger.info(" - %s: next=%s", job.id, job.next_run_time)

    def shutdown(self) -> None:
        if not self._started:
            return
        self.scheduler.shutdown(wait=False)
        self._started = False
        logger.info("[Scheduler] shutdown")

    def schedule_once(self, delay_seconds: int, action: str) -> str:
        """N 秒后执行一次。"""
        run_at = datetime.now() + timedelta(seconds=delay_seconds)
        task_id = f"once_{uuid4().hex[:8]}"
        self.scheduler.add_job(
            _fire_impl,
            DateTrigger(run_date=run_at),
            args=[task_id, action],
            id=task_id,
            name=f"once: {action[:50]}",
        )
        return task_id

    def schedule_daily(self, hour: int, minute: int, action: str) -> str:
        """每天 HH:MM 执行。"""
        task_id = f"daily_{uuid4().hex[:8]}"
        self.scheduler.add_job(
            _fire_impl,
            CronTrigger(hour=hour, minute=minute),
            args=[task_id, action],
            id=task_id,
            name=f"daily {hour:02d}:{minute:02d}: {action[:50]}",
        )
        return task_id

    def list_tasks(self) -> List[Dict]:
        jobs = self.scheduler.get_jobs()
        return [
            {
                "id": j.id,
                "name": j.name,
                "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                "trigger": str(j.trigger),
            }
            for j in jobs
        ]

    def cancel(self, task_id: str) -> bool:
        try:
            self.scheduler.remove_job(task_id)
            return True
        except Exception:
            return False
