from pybotx import Bot, IncomingMessage

from bot.support.collector import collector
from services import ticket_service


@collector.command("/status", description="Узнать статус моего обращения")
async def status_handler(message: IncomingMessage, bot: Bot) -> None:
    ticket = await ticket_service.get_open_ticket(message.user_huid)
    if ticket:
        await bot.answer_message(f"Статус: {ticket.status}")
    else:
        await bot.answer_message("У вас нет активных обращений.")
