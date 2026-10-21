# services/support_service.py
import uuid
from typing import List, Any, Coroutine, Sequence, Optional
from uuid import UUID
import logging

from db.tickets.models import Support, SupportStatus, SupportRegistrationRequest
from db.tickets.repo import SupportRepo, TicketRepo

logger = logging.getLogger(__name__)
_support_service: Optional["SupportService"] = None


def get_support_service() -> "SupportService":
    global _support_service
    if _support_service is None:
        _support_service = SupportService()
    return _support_service


class SupportService:
    def __init__(self):
        self.repo = SupportRepo()
        self.repo_ticket = TicketRepo()

    async def get_all(self) -> Sequence[UUID]:
        return await self.repo.get_all()

    async def get_online(self) -> Sequence[Support]:
        return await self.repo.get_all_online_specialists()

    async def set_status(self, support_id: uuid.UUID, status: SupportStatus):
        return await self.repo.set_status(support_id=support_id, status=status)

    async def get_or_create(self, support_id: uuid.UUID, full_name: str) -> Support:
        return await self.repo.get_or_create_specialist(support_id=support_id, full_name=full_name)

    async def get_support_by_id(self, support_id: uuid.UUID) -> Support:
        return await self.repo.get_by_id(support_id=support_id)

    async def get_online_specialist(self) -> Sequence[Support]:
        return await self.repo.get_all_online_specialists()

    # ── Заявки на регистрацию ────────────────────────────────

    async def request_registration(
            self, support_id: UUID, full_name: str, chat_id: uuid.UUID
    ) -> Optional[SupportRegistrationRequest]:
        """Создаёт pending-заявку. None = уже есть активная заявка."""
        return await self.repo.create_registration_request(support_id, full_name, chat_id)

    async def get_pending_registrations(self) -> Sequence[SupportRegistrationRequest]:
        return await self.repo.get_pending_registration_requests()

    async def approve_registration(
            self,
            request_id: int,
            supervisor_huid: UUID,
    ) -> Optional[tuple[Support, SupportRegistrationRequest]]:
        """
        Одобряет заявку и создаёт Support (статус OFFLINE).
        Возвращает (support, request) или None, если заявка уже обработана.
        """
        request = await self.repo.approve_registration(request_id, supervisor_huid)
        if request is None:
            return None

        existing = await self.repo.get_by_id(request.support_id)
        if existing is None:
            existing = await self.repo.get_or_create_specialist(
                support_id=request.support_id,
                full_name=request.full_name,
            )

        logger.info(
            f"[REGISTRATION] Заявка #{request_id} одобрена супервизором "
            f"{supervisor_huid}, специалист {request.full_name} создан"
        )
        return existing, request

    async def reject_registration(
            self,
            request_id: int,
            supervisor_huid: UUID,
            reason: str,
    ) -> Optional[SupportRegistrationRequest]:
        request = await self.repo.reject_registration(
            request_id, supervisor_huid, reason,
        )
        if request is not None:
            logger.info(
                f"[REGISTRATION] Заявка #{request_id} отклонена супервизором "
                f"{supervisor_huid}: {reason}"
            )
        return request

    async def get_support_status(
            self, support_id: uuid.UUID,
    ) -> Optional[SupportStatus]:
        """Возвращает текущий статус специалиста или None."""
        specialist = await self.repo.get_by_id(support_id)
        if specialist is None:
            return None
        return specialist.status

    async def ensure_chat_id(
            self,
            support_id: UUID,
            chat_id: UUID,
    ) -> None:
        """
        Заполняет chat_id специалиста, если он ещё не задан.
        Тихо пропускает, если специалиста нет в БД.
        """
        updated = await self.repo.set_chat_id(support_id, chat_id)
        if updated:
            logger.info(
                f"[SUPPORT] chat_id установлен для {support_id}: {chat_id}"
            )

    async def get_chat_id(
            self, support_id: UUID,
    ) -> Optional[UUID]:
        return await self.repo.get_chat_id(support_id)

    async def get_all_with_chat(self) -> Sequence[Support]:
        return await self.repo.get_all_with_chat()
