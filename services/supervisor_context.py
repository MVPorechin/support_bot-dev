# services/supervisor_context.py
"""
Контекстные переменные для супервизора.
Живут в рамках одного входящего сообщения (contextvars).
"""
import uuid
from contextvars import ContextVar
from typing import Optional


# chat_id супервизора, определённый в middleware
supervisor_chat_id_var: ContextVar[Optional[uuid.UUID]] = ContextVar(
    "supervisor_chat_id", default=None,
)

# huid супервизора — на всякий случай, если понадобится
supervisor_huid_var: ContextVar[Optional[uuid.UUID]] = ContextVar(
    "supervisor_huid", default=None,
)


def get_supervisor_chat_id() -> Optional[uuid.UUID]:
    """Возвращает chat_id текущего супервизора или None."""
    return supervisor_chat_id_var.get()


def set_supervisor_context(
    huid: uuid.UUID,
    chat_id: Optional[uuid.UUID],
) -> None:
    supervisor_huid_var.set(huid)
    supervisor_chat_id_var.set(chat_id)