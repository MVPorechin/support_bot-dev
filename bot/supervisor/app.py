"""FastAPI для бота супервизора. Webhook URL: /supervisor/command."""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pybotx.bot.api.responses.command_accepted import build_command_accepted_response
from services.cache_service import get_redis
from bot.supervisor.bot import supervisor_bot
from services.supervisor_panel_service import maybe_show_shift_panel

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await supervisor_bot.startup()
    logger.info("[SUPERVISOR] Бот супервизора запущен")
    yield
    await supervisor_bot.shutdown()
    await get_redis().shutdown()
    logger.info("[SUPERVISOR] Бот супервизора остановлен")


app = FastAPI(
    title="Supervisor Bot",
    lifespan=lifespan,
)


@app.post("/command")
async def command_handler(request: Request) -> JSONResponse:
    data = await request.json()
    supervisor_bot.async_execute_raw_bot_command(
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
    Мы используем это, чтобы проактивно показать супервизора панель смены.
    """
    # params = dict(request.query_params)
    #
    # # 1. Показываем панель специалисту (если это супервизор и давно не показывали)
    # try:
    #     user_huid_str = params.get("user_huid")
    #     bot_id_str = params.get("bot_id")
    #
    #     if user_huid_str and bot_id_str:
    #         user_huid = uuid.UUID(user_huid_str)
    #
    #         asyncio.create_task(
    #             maybe_show_shift_panel(
    #                 bot=supervisor_bot,
    #                 user_huid=user_huid,
    #             )
    #         )
    # except Exception as exc:
    #     logger.exception(f"[/status] Ошибка показа панели: {exc}")

    # 2. Возвращаем стандартный ответ для eXpress


    status = await supervisor_bot.raw_get_status(
        dict(request.query_params), request_headers=request.headers,
    )
    return JSONResponse(status)


@app.post("/notification/callback")
async def callback_handler(request: Request) -> JSONResponse:
    await supervisor_bot.set_raw_botx_method_result(
        await request.json(), verify_request=False,
    )
    return JSONResponse(
        build_command_accepted_response(),
        status_code=HTTPStatus.ACCEPTED,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "bot": "supervisor"}