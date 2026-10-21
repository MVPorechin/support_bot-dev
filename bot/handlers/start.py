# app/bot/handlers/start.py
from pybotx import Bot, IncomingMessage

from bot.support.collector import collector


@collector.command("/start", description="Начать общение с поддержкой")
async def start_handler(message: IncomingMessage, bot: Bot) -> None:
    await bot.answer_message(
        body=f"Привет {message.sender.username}! Чем я могу помочь? Просто напиши свой вопрос в этот чат."
    )
