"""
Фоновые задачи бота поддержки.

Все задачи используют support_bot для отправки сообщений, потому что:
  • уведомления пользователям идут в личные чаты бота поддержки
  • уведомления операторам — в треды тикетов (чаты бота поддержки)
  • supervisor_bot нужен только если шлём что-то супервизорам напрямую

Файл не содержит точку входа — она в main_worker.py.
"""
import asyncio
import logging
import uuid

from pybotx import BubbleMarkup, KeyboardMarkup

from bot.support.bot import support_bot
from config import BotConfig, SLAConfig, AuditConfig
from db.tickets.models import WorkSessionEndReason
from services.audit_service import get_audit_service
from services.lock_service import RedisLock
from services.cache_service import get_redis
from services.shift_service import ShiftService
from services.sla_service import SLAService
from services.ticket_service import TicketService
from db.audit.models import AuditAction, AuditSeverity
from bot.supervisor.bot import supervisor_bot
from services.supervisor_service import get_supervisor_service
from config import BotConfig


logger = logging.getLogger(__name__)

_shift_service: ShiftService | None = None

def get_shift_service():
    global _shift_service
    if _shift_service is None:
        _shift_service = ShiftService()
    return _shift_service
# ══════════════════════════════════════════════════════════════
# 1. AUTO-ASSIGN QUEUE — назначение тикетов из очереди
# ══════════════════════════════════════════════════════════════
async def auto_assign_queue() -> None:
    """
    Каждые 15 секунд проверяет очередь NEW/OPEN и назначает тикеты
    на самых свободных онлайн-операторов.

    Redis-lock защищает от дублей, если случайно запустится
    несколько worker-процессов.
    """
    ticket_service = TicketService()
    redis = get_redis()

    await asyncio.sleep(10)
    logger.info("[WORKER] auto_assign_queue запущена")

    while True:
        try:
            async with RedisLock(redis, "auto_assign", ttl=20) as acquired:
                if not acquired:
                    logger.debug("[WORKER] auto_assign: lock занят, пропускаем")
                else:
                    n = await ticket_service.process_queue(support_bot)
                    if n > 0:
                        logger.info(f"[WORKER] auto_assign: назначено тикетов: {n}")
        except asyncio.CancelledError:
            logger.info("[WORKER] auto_assign_queue остановлена")
            break
        except Exception as exc:
            logger.exception(f"[WORKER] Ошибка в auto_assign_queue: {exc}")

        await asyncio.sleep(15)


# ══════════════════════════════════════════════════════════════
# 2. AUTO-CLOSE TICKETS — закрытие SOLVED-тикетов старше 24ч
# ══════════════════════════════════════════════════════════════
async def auto_close_tickets() -> None:
    """
    Раз в час закрывает тикеты в статусе SOLVED, которые висят
    без ответа пользователя более 24 часов.
    """
    ticket_service = TicketService()
    audit = get_audit_service()
    redis = get_redis()

    await asyncio.sleep(15)
    logger.info("[WORKER] auto_close_tickets запущена")

    while True:
        try:
            async with RedisLock(redis, "auto_close", ttl=3650) as acquired:
                if acquired:
                    expired = await ticket_service.repo.get_expired_solved_tickets()
                    for ticket in expired:
                        try:
                            await ticket_service.repo.close_ticket(ticket.ticket_id)

                            shift_service = get_shift_service()
                            await shift_service.end_ticket_work(
                                ticket_id=ticket.ticket_id,
                                reason=WorkSessionEndReason.AUTO_CLOSED.value,
                            )

                            # AUDIT: TICKET_AUTO_CLOSED_24H
                            await audit.system_action(
                                AuditAction.TICKET_AUTO_CLOSED_24H,
                                ticket_id=ticket.ticket_id,
                                old={"status": "solved"},
                                new={"status": "closed"},
                                context={
                                    "reason": "no_feedback_24h",
                                    "task": "auto_close_tickets",
                                },
                            )

                            # Уведомляем тред (если есть)
                            if ticket.support_thread_message_id:
                                try:
                                    await support_bot.send_message(
                                        bot_id=uuid.UUID(BotConfig.BOT_ID),
                                        chat_id=ticket.support_thread_message_id,
                                        body=(
                                            f"🏁 Тикет #{ticket.ticket_id} "
                                            f"закрыт системой."
                                        ),
                                        wait_callback=False
                                    )
                                except Exception as exc:
                                    logger.error(
                                        f"[WORKER] Не удалось уведомить тред "
                                        f"#{ticket.ticket_id}: {exc}"
                                    )

                        except Exception as exc:
                            logger.exception(
                                f"[WORKER] Ошибка автозакрытия "
                                f"#{ticket.ticket_id}: {exc}"
                            )
        except asyncio.CancelledError:
            logger.info("[WORKER] auto_close_tickets остановлена")
            break
        except Exception as exc:
            logger.exception(f"[WORKER] Ошибка в auto_close_tickets: {exc}")

        await asyncio.sleep(3600)


# ══════════════════════════════════════════════════════════════
# 3. SLA MONITOR — контроль дедлайнов
# ══════════════════════════════════════════════════════════════
async def sla_monitor_loop() -> None:
    """
    Каждые 15 минут проверяет SLA всех активных тикетов:
      • за 4ч до дедлайна — предупреждение 🟡
      • за 2ч — эскалация 🟠
      • при просрочке — breach 🔴
    """
    sla_service = SLAService()
    redis = get_redis()

    logger.info("[WORKER] sla_monitor_loop запущена")

    while True:
        try:
            async with RedisLock(redis, "sla_monitor", ttl=950) as acquired:
                if acquired:
                    await sla_service.check_and_notify(support_bot)
        except asyncio.CancelledError:
            logger.info("[WORKER] sla_monitor_loop остановлена")
            break
        except Exception as exc:
            logger.exception(f"[WORKER] Ошибка в sla_monitor_loop: {exc}")

        await asyncio.sleep(SLAConfig.CHECK_INTERVAL_SEC)


# ══════════════════════════════════════════════════════════════
# 4. USER RESPONSE MONITOR — напоминания, PENDING, автозакрытие
# ══════════════════════════════════════════════════════════════
async def user_response_monitor_loop() -> None:
    """
    Каждые 15 секунд обрабатывает таймеры жизненного цикла:
      1. напоминание через 5 мин после ответа оператора
      2. перевод в PENDING через ещё 5 минут
      3. перевод в PENDING через 10 мин после запроса закрытия
      4. автозакрытие через 2 часа в PENDING
    """
    ticket_service = TicketService()
    redis = get_redis()

    await asyncio.sleep(5)
    logger.info("[WORKER] user_response_monitor_loop запущена")

    while True:
        try:
            async with RedisLock(redis, "user_response", ttl=20) as acquired:
                if acquired:
                    n = await ticket_service.process_user_response_timeouts(
                        support_bot,
                    )
                    if n > 0:
                        logger.info(
                            f"[WORKER] user_response: обработано событий: {n}"
                        )
        except asyncio.CancelledError:
            logger.info("[WORKER] user_response_monitor_loop остановлена")
            break
        except Exception as exc:
            logger.exception(
                f"[WORKER] Ошибка в user_response_monitor_loop: {exc}"
            )

        await asyncio.sleep(15)


# ══════════════════════════════════════════════════════════════
# 5. AUDIT RETENTION — очистка старых записей аудита
# ══════════════════════════════════════════════════════════════
async def audit_retention_loop() -> None:
    """
    Раз в сутки удаляет audit_logs старше AuditConfig.RETENTION_DAYS.
    """
    audit = get_audit_service()
    redis = get_redis()

    await asyncio.sleep(AuditConfig.CLEANUP_STARTUP_DELAY_SEC)
    logger.info(
        f"[WORKER] audit_retention_loop запущена "
        f"(retention: {AuditConfig.RETENTION_DAYS} дней)"
    )

    while True:
        try:
            async with RedisLock(redis, "audit_retention", ttl=86450) as acquired:
                if acquired:
                    deleted = await audit.repo.purge_older_than(
                        AuditConfig.RETENTION_DAYS,
                    )
                    if deleted:
                        logger.info(
                            f"[AUDIT] Очищено {deleted} записей старше "
                            f"{AuditConfig.RETENTION_DAYS} дней"
                        )
        except asyncio.CancelledError:
            logger.info("[WORKER] audit_retention_loop остановлена")
            break
        except Exception as exc:
            logger.exception(f"[WORKER] Ошибка в audit_retention_loop: {exc}")

        await asyncio.sleep(AuditConfig.CLEANUP_INTERVAL_SEC)


async def escalate_to_supervisors(
    body: str,
    bubbles: KeyboardMarkup | None = None,
    *,
    only_huids: list[uuid.UUID] | None = None,
) -> int:
    """
    Рассылает текст всем активным супервизорам через supervisor_bot.

    Аргументы:
      body        — текст сообщения
      bubbles     — опциональная клавиатура
      only_huids  — если задан, шлём только этим супервизорам

    Возвращает количество успешно доставленных сообщений.
    """
    svc = get_supervisor_service()
    supervisors = await svc.get_all_active_with_chat()

    if only_huids is not None:
        wanted = set(only_huids)
        supervisors = [s for s in supervisors if s.supervisor_id in wanted]

    if not supervisors:
        logger.warning(
            "[ESCALATE] Нет активных супервизоров с chat_id — "
            "уведомление не отправлено"
        )
        return 0

    delivered = 0
    bot_id = uuid.UUID(BotConfig.SUPERVISOR_BOT_ID)

    for sup in supervisors:
        # chat_id может прийти как UUID или как str из БД — нормализуем
        chat_id = sup.chat_id
        if chat_id is None:
            continue

        try:
            await supervisor_bot.send_message(
                bot_id=bot_id,
                chat_id=chat_id,
                body=body,
                bubbles=bubbles,
                wait_callback = False
            )
            delivered += 1
        except Exception as exc:
            logger.error(
                f"[ESCALATE] Не удалось уведомить супервизора "
                f"{sup.full_name} ({sup.supervisor_id}): {exc}"
            )

    if delivered:
        logger.info(
            f"[ESCALATE] Доставлено {delivered}/{len(supervisors)} "
            f"супервизорам"
        )

    return delivered

async def shift_monitor_loop() -> None:
    """
    Каждую минуту проверяет, не истекли ли смены специалистов.
    Если истекли — переводит в OFFLINE, закрывает shift и work sessions.
    """
    from services.shift_service import ShiftService
    from services.lock_service import RedisLock
    from services.cache_service import get_redis
    from services.audit_service import get_audit_service
    from db.audit.models import AuditAction

    redis = get_redis()
    await asyncio.sleep(30)
    logger.info("[WORKER] shift_monitor_loop запущена")

    while True:
        try:
            async with RedisLock(redis, "shift_monitor", ttl=70) as acquired:
                if acquired:
                    svc = ShiftService()
                    processed = await svc.process_expired_shifts()

                    for specialist in processed:
                        try:
                            await support_bot.send_message(
                                bot_id=uuid.UUID(BotConfig.BOT_ID),
                                chat_id=specialist.chat_id or specialist.support_id,
                                body=(
                                    "⏰ Ваша смена автоматически завершена "
                                    "по истечении выбранного времени.\n"
                                    "Чтобы продолжить работу, отправьте `/online`."
                                ),
                                wait_callback=False,
                            )
                        except Exception as exc:
                            logger.error(
                                f"[SHIFT] Не удалось уведомить "
                                f"{specialist.full_name}: {exc}"
                            )

                        # [AUDIT]
                        await get_audit_service().log(
                            action="support.shift_auto_ended",
                            actor_type="system",
                            entity_type="support",
                            entity_id=str(specialist.support_id),
                            actor_name="bot",
                            severity=AuditSeverity.INFO,
                            context={"reason": "auto_timeout"},
                        )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception(f"[WORKER] shift_monitor_loop: {exc}")
        await asyncio.sleep(60)

# ══════════════════════════════════════════════════════════════
# ВСЕ ЗАДАЧИ — удобный агрегатор
# ══════════════════════════════════════════════════════════════
ALL_TASKS = [
    ("auto_assign_queue", auto_assign_queue),
    ("auto_close_tickets", auto_close_tickets),
    ("sla_monitor_loop", sla_monitor_loop),
    ("user_response_monitor_loop", user_response_monitor_loop),
    ("audit_retention_loop", audit_retention_loop),
    ("shift_monitor_loop", shift_monitor_loop),
]