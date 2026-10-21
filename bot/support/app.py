"""FastAPI для бота поддержки. Webhook URL: /command."""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pybotx.bot.api.responses.command_accepted import build_command_accepted_response
from services.cache_service import get_redis
from bot.support.bot import support_bot
from services.specialist_panel_service import maybe_show_shift_panel

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await support_bot.startup()
    logger.info("[SUPPORT] Бот поддержки запущен")
    yield
    await support_bot.shutdown()
    await get_redis().shutdown()
    logger.info("[SUPPORT] Бот поддержки остановлен")


app = FastAPI(
    title="Support Bot",
    lifespan=lifespan,
)


@app.post("/command")
async def command_handler(request: Request) -> JSONResponse:
    data = await request.json()
    support_bot.async_execute_raw_bot_command(
        data, request_headers=request.headers,
    )
    return JSONResponse(
        build_command_accepted_response(),
        status_code=HTTPStatus.ACCEPTED,
    )


@app.get("/status")
async def status_handler(request: Request) -> JSONResponse:
    """
    Обработчик GET /status.

    eXpress вызывает его при переключении пользователя на чат с ботом.
    Мы используем это, чтобы проактивно показать специалисту панель смены.
    """
    # params = dict(request.query_params)
    #
    # # 1. Показываем панель специалисту (если это специалист и давно не показывали)
    # try:
    #     user_huid_str = params.get("user_huid")
    #     bot_id_str = params.get("bot_id")
    #
    #     if user_huid_str and bot_id_str:
    #         user_huid = uuid.UUID(user_huid_str)
    #
    #         asyncio.create_task(
    #             maybe_show_shift_panel(
    #                 bot=support_bot,
    #                 user_huid=user_huid,
    #             )
    #         )
    # except Exception as exc:
    #     logger.exception(f"[/status] Ошибка показа панели: {exc}")

    # 2. Возвращаем стандартный ответ для eXpress

    status = await support_bot.raw_get_status(
        dict(request.query_params), request_headers=request.headers,
    )
    return JSONResponse(status)


@app.post("/notification/callback")
async def callback_handler(request: Request) -> JSONResponse:
    await support_bot.set_raw_botx_method_result(
        await request.json(), verify_request=False,
    )
    return JSONResponse(
        build_command_accepted_response(),
        status_code=HTTPStatus.ACCEPTED,
    )


@app.get("/health")
async def health() -> dict:
    """
    Публичный healthcheck без авторизации.
    Используется Docker healthcheck и балансировщиками.
    Не проверяет внешние зависимости — только «процесс жив».
    """
    return {"status": "ok", "bot": "support"}