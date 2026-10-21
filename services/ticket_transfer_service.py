# services/ticket_transfer_service.py
import uuid
from typing import Any, Coroutine

from db.tickets.models import TicketTransfer
from db.tickets.repo import TicketTransferRepo
from config import BotConfig

class TicketTransferService:
    def __init__(self):
        self.repo = TicketTransferRepo()
        self._bot_id = uuid.UUID(BotConfig.BOT_ID)

    async def set(
            self,
            ticket_id:int,
            from_huid:uuid.UUID,
            to_huid:uuid.UUID,
            comment:str
    )-> Coroutine[Any, Any, TicketTransfer]:
        """
        Создает запись о переводе тикета.
        """
        return await self.repo.set(
            ticket_id=ticket_id,
            from_huid=from_huid,
            to_huid=to_huid,
            comment=comment
        )

    async def get_history(self, ticket_id: int) -> list[TicketTransfer]:
        """Возвращает историю переводов тикета."""
        return await self.repo.get_transfer_history(ticket_id)