from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .config import Settings
from .db.pool import create_pool
from .db.ddl import ensure_schema
from .db.repo import Repo

from .avito.client import AvitoClient, AvitoClientConfig
from .jobs.poller import incremental_poll_all
from .jobs.scheduler import make_scheduler, add_poll_job

from .bot.router import router as bot_router
from .analysis.classifier import ModelClassifier

async def main() -> None:
    s = Settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO))
    log = logging.getLogger("app")
    log.info("Starting...")

    pool = await create_pool(s.pg_dsn)

    async with pool.acquire() as conn:
        await ensure_schema(conn)

    repo = Repo(pool)

    client = AvitoClient(
        AvitoClientConfig(
            city_slug=s.avito_city_slug,
            max_pages=s.avito_max_pages,
            page_delay_s=s.avito_page_delay_s,
            timeout_s=s.avito_timeout_s,
            user_agent=s.avito_user_agent,
        )
    )
    classifier = ModelClassifier(pool)
    await classifier.load()
    bot = Bot(token=s.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    # Dependency injection for handlers
    dp["repo"] = repo
    dp["client"] = client
    dp["classifier"] = classifier

    dp.include_router(bot_router)

    sched = make_scheduler()
    between_queries_delay_s = int(os.getenv("AVITO_BETWEEN_QUERIES_DELAY_S", "60"))
    add_poll_job(
        sched,
        minutes=s.avito_poll_minutes,
        func=incremental_poll_all,
        args=[repo, client, classifier],
        kwargs={
            "bot": bot,
            "notify_chat_id": s.notify_chat_id,
            "between_queries_delay_s": between_queries_delay_s,
        },
    )
    sched.start()

    try:
        await dp.start_polling(bot)
    finally:
        sched.shutdown(wait=False)
        await bot.session.close()
        await pool.close()
        log.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
