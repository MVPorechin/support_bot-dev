"""
Точка входа для контейнера support_worker.

Запуск:
  • В Docker:     python main_worker.py
  • Локально:     python main_worker.py
  • В compose:    command: python main_worker.py

Не поднимает FastAPI и не обрабатывает webhook.
Только фоновые задачи + старт обоих ботов (для отправки сообщений).
"""
import asyncio
import logging
import signal
import sys

from bot.support.bot import support_bot
from bot.supervisor.bot import supervisor_bot
from services.cache_service import get_redis
from worker_tasks import ALL_TASKS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worker")


# main_worker.py
async def run_all_tasks(stop_event: asyncio.Event) -> None:
    tasks = [
        asyncio.create_task(coro(), name=name)
        for name, coro in ALL_TASKS
    ]

    logger.info(
        f"[WORKER] Запущено {len(tasks)} фоновых задач: "
        f"{', '.join(t.get_name() for t in tasks)}"
    )

    stop_task = asyncio.create_task(stop_event.wait(), name="stop_listener")

    try:
        done, pending = await asyncio.wait(
            tasks + [stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in done:
            if t is stop_task:
                continue
            if t.exception():
                # ✅ exc_info=exc — выведет полный traceback
                logger.error(
                    f"[WORKER] Задача {t.get_name()} завершилась с ошибкой",
                    exc_info=t.exception(),
                )
            else:
                logger.warning(
                    f"[WORKER] Задача {t.get_name()} завершилась неожиданно"
                )

        for t in tasks + [stop_task]:
            t.cancel()
        await asyncio.gather(*tasks, *[stop_task], return_exceptions=True)

    except asyncio.CancelledError:
        for t in tasks + [stop_task]:
            t.cancel()
        await asyncio.gather(*tasks, *[stop_task], return_exceptions=True)


async def main() -> None:
    logger.info("[WORKER] Старт")

    # Поднимаем ботов для отправки сообщений из фоновых задач
    await support_bot.startup()
    await supervisor_bot.startup()

    # Graceful shutdown по SIGTERM/SIGINT
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("[WORKER] Получен сигнал остановки")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows — только SIGINT через KeyboardInterrupt
            pass

    try:
        await run_all_tasks(stop_event)
    finally:
        logger.info("[WORKER] Остановка ботов")
        await support_bot.shutdown()
        await supervisor_bot.shutdown()
        await get_redis().shutdown()
        logger.info("[WORKER] Остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[WORKER] Прервано пользователем")
        sys.exit(0)