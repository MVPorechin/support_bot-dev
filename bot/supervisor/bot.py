"""Инстанс Bot для супервизора — только collector супервизоров."""
import logging

from pybotx import Bot, BotAccountWithSecret

from bot.supervisor.collector import supervisor_collector
from bot.supervisor.middleware import supervisor_middleware
from config import BotConfig

logger = logging.getLogger(__name__)


def create_supervisor_bot() -> Bot:
    bot = Bot(
        collectors=[supervisor_collector],
        bot_accounts=[
            BotAccountWithSecret(
                id=BotConfig.SUPERVISOR_BOT_ID,
                secret_key=BotConfig.SUPERVISOR_SECRET_KEY,
                cts_url=BotConfig.SUPERVISOR_CTS_URL,
            ),
            BotAccountWithSecret(
                id=BotConfig.BOT_ID,
                secret_key=BotConfig.SECRET_KEY,
                cts_url=BotConfig.SUPERVISOR_CTS_URL,
            )
        ],
        # middleware НЕ ставим — сообщения супервизора не создают тикеты
        middlewares=[supervisor_middleware],
    )
    logger.info(
        f"[SUPERVISOR] Bot создан: id={BotConfig.SUPERVISOR_BOT_ID}"
    )
    return bot


supervisor_bot = create_supervisor_bot()