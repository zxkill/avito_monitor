from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger


def make_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone="Europe/Amsterdam")


def add_poll_job(
    sched: AsyncIOScheduler,
    *,
    minutes: int,
    func,
    args=None,
    kwargs=None,
) -> None:
    sched.add_job(
        func,
        trigger=IntervalTrigger(minutes=minutes),
        id="poll_searches",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
        args=args or [],
        kwargs=kwargs or {},
    )
