"""Инстанс Bot для поддержки — только collector операторов."""
import logging
from pybotx import Bot, BotAccountWithSecret
from bot.support.collector import collector
from bot.support.middleware import ticket_creation_middleware, _maybe_fill_specialist_chat_id

from config import BotConfig

logger = logging.getLogger(__name__)


def create_support_bot() -> Bot:
    """Собирает Bot только с коллектором поддержки и ticket-middleware."""
    bot = Bot(
        collectors=[collector],
        bot_accounts=[
            BotAccountWithSecret(
                id=BotConfig.BOT_ID,
                secret_key=BotConfig.SECRET_KEY,
                cts_url=BotConfig.CTS_URL,
            ),
        ],
        middlewares=[ticket_creation_middleware, _maybe_fill_specialist_chat_id],
    )
    logger.info(
        f"[SUPPORT] Bot создан: id={BotConfig.BOT_ID}"
    )
    return bot


# Singleton — используется и в app.py, и в worker_tasks.py
support_bot = create_support_bot()