# services/supervisor_service.py
import uuid
import logging
from typing import Optional, Sequence

from db.tickets.models import Supervisor, SupervisorInvite
from db.tickets.repo import SupervisorRepo
from config import SupervisorInviteConfig

logger = logging.getLogger(__name__)

_supervisor_service: Optional["SupervisorService"] = None


def get_supervisor_service() -> "SupervisorService":
    global _supervisor_service
    if _supervisor_service is None:
        _supervisor_service = SupervisorService()
    return _supervisor_service

class SupervisorService:
    def __init__(self):
        self.repo = SupervisorRepo()

    async def get_or_create(self, supervisor_id: uuid.UUID, full_name: str) -> Supervisor:
        return await self.repo.get_or_create(supervisor_id=supervisor_id, full_name=full_name)

    async def get_all_active(self) -> Sequence[uuid.UUID]:
        return await self.repo.get_all_active()

    async def get_by_id(self, supervisor_id: uuid.UUID) -> Optional[Supervisor]:
        return await self.repo.get_by_id(supervisor_id)

    async def is_supervisor(self, huid: uuid.UUID) -> bool:
        """Быстрая проверка: является ли пользователь супервизором."""
        supervisor = await self.repo.get_by_id(huid)
        return supervisor is not None and supervisor.is_active

    async def create_invite(self, created_by: uuid.UUID) -> SupervisorInvite:
        """
        Создаёт приглашение. Отзывает предыдущие активные того же автора,
        чтобы не плодить висящие коды.
        """
        # Отзываем активные приглашения этого супервизора
        existing = await self.repo.get_my_invites(created_by, limit=100)
        for inv in existing:
            if inv.used_at is None and inv.revoked_at is None:
                await self.repo.revoke_invite(inv.code, created_by)

        invite = await self.repo.create_invite(
            created_by=created_by,
            ttl_hours=SupervisorInviteConfig.TTL_HOURS,
        )
        logger.info(
            f"[INVITE] Супервизор {created_by} создал приглашение "
            f"{invite.code}, истекает {invite.expires_at}"
        )
        return invite

    async def register_by_invite(
        self,
        code: uuid.UUID,
        candidate_huid: uuid.UUID,
        candidate_name: str,
    ) -> tuple[bool, str, Optional[Supervisor]]:
        """
        Регистрирует супервизора по коду.
        Возвращает (success, message, supervisor).
        """
        # Уже супервизор?
        existing = await self.repo.get_by_id(candidate_huid)
        if existing is not None:
            return False, "Вы уже зарегистрированы как супервизор.", existing

        # Атомарно «съедаем» код
        used = await self.repo.use_invite(code, candidate_huid)
        if used is None:
            # Разбираемся, почему не получилось
            invite = await self.repo.get_invite_by_code(code)
            if invite is None:
                return False, "Неверный код приглашения.", None
            if invite.revoked_at is not None:
                return False, "Приглашение отозвано.", None
            if invite.used_at is not None:
                return False, "Приглашение уже использовано.", None
            from datetime import datetime, timezone
            if invite.expires_at <= datetime.now(timezone.utc):
                return False, "Срок действия приглашения истёк.", None
            return False, "Не удалось использовать приглашение.", None

        # Создаём супервизора
        supervisor = await self.repo.get_or_create(
            supervisor_id=candidate_huid,
            full_name=candidate_name,
        )

        logger.info(
            f"[INVITE] Супервизор {candidate_huid} ({candidate_name}) "
            f"зарегистрирован по приглашению {code}"
        )
        return True, "Регистрация успешна.", supervisor

    async def get_my_invites(self, supervisor_huid: uuid.UUID) -> Sequence[SupervisorInvite]:
        return await self.repo.get_my_invites(supervisor_huid)

    async def revoke_invite(self, code: uuid.UUID, revoked_by: uuid.UUID) -> bool:
        return await self.repo.revoke_invite(code, revoked_by)

    async def ensure_chat_id(
        self,
        supervisor_id: uuid.UUID,
        chat_id: uuid.UUID,
    ) -> None:
        """
        Заполняет chat_id, если он ещё не задан.
        Тихо пропускает, если супервизора нет в БД.
        """
        updated = await self.repo.set_chat_id(supervisor_id, chat_id)
        if updated:
            logger.info(
                f"[SUPERVISOR] chat_id установлен для {supervisor_id}: "
                f"{chat_id}"
            )

    async def get_chat_id(
        self, supervisor_id: uuid.UUID,
    ) -> Optional[uuid.UUID]:
        return await self.repo.get_chat_id(supervisor_id)

    async def get_all_active_with_chat(self) -> Sequence[Supervisor]:
        return await self.repo.get_all_active_with_chat()