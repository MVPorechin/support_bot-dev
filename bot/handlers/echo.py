# app/bot/handlers/echo.py
from pybotx import Bot, IncomingMessage
from bot.collector import collector

@collector.handler(  # декоратор регистрирует функцию как обработчик
    handler=None,    # None означает "любое сообщение, не попавшее под другие правила"
)
async def echo_handler(message: IncomingMessage, bot: Bot) -> None:
    await bot.answer_message(
        body=f"👋 Привет! Твоё обращение зафиксировано. Скоро с тобой свяжутся.",
    )