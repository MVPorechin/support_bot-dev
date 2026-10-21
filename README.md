# 🤖 Support Bot для eXpress

Асинхронный бот технической поддержки для корпоративного мессенджера **eXpress**. Полный жизненный цикл тикетов: от создания пользователем до сбора обратной связи и аналитики.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-blue.svg)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/docker-compose-blue.svg)](https://docs.docker.com/compose/)

---

## 📖 Оглавление

- [Возможности](#-возможности)
- [Технологический стек](#-технологический-стек)
- [Архитектура](#-архитектура)
- [Быстрый старт](#-быстрый-старт)
- [Конфигурация](#-конфигурация)
- [Миграции БД](#-миграции-бд)
- [Команды бота](#-команды-бота)
- [Фоновые задачи](#-фоновые-задачи)
- [Разработка](#-разработка)
- [Troubleshooting](#-troubleshooting)

---

## ✨ Возможности

### Для пользователей
- 💬 Создание обращений из личного чата с ботом (текст, файлы, изображения)
- 📎 Первое сообщение без текста (картинка) **не теряется** — сразу летит в тред поддержки
- ⭐ Оценка качества работы (1–5) и текстовый комментарий
- 🔄 Возможность переоткрыть проблему, если не решена

### Для операторов поддержки
- 🟢 Управление сменой: `/online`, `/offline`, `/break`
- 📥 Взятие тикетов из очереди: `/take <ID>`, `/take_next`
- 🔀 Перевод тикета коллеге: `/transfer <ID>`
- 📜 Аудит переводов: `/history <ID>`
- 🎯 Шаблоны быстрых ответов и запрос закрытия

### Для супервизоров
- 📊 Панель управления с быстрыми отчётами и экспортом
- ⏱ Мониторинг SLA в реальном времени с цветовой индикацией
- ⏸ Просмотр и реактивация отложенных тикетов (`PENDING`)
- 🚨 Принудительное закрытие зависших тикетов
- 👥 Управление статусами операторов
- 🎫 Регистрация новых супервизоров через инвайт-коды
- ✅ Модерация регистрации операторов
- 📥 Массовая загрузка операторов из CSV
- 📊 Экспорт тикетов (по специалисту, пользователю, периоду) и переписки
- 🔍 Аудит всех действий с фильтрацией по тикету, актору, correlation_id

### Автоматические процессы
- 🎯 **Умное автоназначение** на самого свободного онлайн-оператора (лимит: 2 тикета на оператора)
- ⏱ **SLA-мониторинг** с 3 порогами эскалации (8ч дедлайн, пороги 4ч / 2ч / 0)
- ⏸ **PENDING state-machine**: 5 мин молчания → напоминание → ещё 5 мин → отложен → 2 часа → закрыт
- 🕐 **Таймаут запроса закрытия**: 10 минут на ответ пользователя после запроса оператора
- 🔄 **Умное переоткрытие**: если старый оператор оффлайн — тикет в приоритетную очередь
- 🧹 **Автозакрытие SOLVED-тикетов** через 24 часа
- 📝 **Иммутабельный аудит** всех действий

---

## 🏗 Технологический стек

| Компонент | Технология | Назначение |
|---|---|---|
| **Язык** | Python 3.11+ | Асинхронная бизнес-логика |
| **Фреймворк** | FastAPI + Uvicorn | ASGI-сервер, webhooks |
| **Мессенджер** | [pybotx](https://github.com/ExpressApp/pybotx) | Интеграция с eXpress |
| **FSM** | [pybotx-fsm](https://github.com/ExpressApp/pybotx-fsm) | Состояния диалогов |
| **БД** | PostgreSQL 15+ (asyncpg) | Хранение данных |
| **ORM** | SQLAlchemy 2.0 (async) | Работа с БД |
| **Миграции** | Alembic | Версионирование схемы |
| **Кэш** | Redis | FSM, rate-limit |
| **Деплой** | Docker + Compose | Контейнеризация |

---

## 🗂 Архитектура
~~~
.
├── app/
│   ├── bot/
│   │   ├── support/                    # ── БОТ ПОДДЕРЖКИ ──
│   │   │   ├── __init__.py
│   │   │   ├── bot.py                  # инстанс Bot для support
│   │   │   ├── collector.py            # команды операторов
│   │   │   ├── middleware.py           # ticket_creation_middleware
│   │   │   └── app.py                  # FastAPI для support
│   │   │
│   │   ├── supervisor/                 # ── БОТ СУПЕРВИЗОРА ──
│   │   │   ├── __init__.py
│   │   │   ├── bot.py                  # инстанс Bot для supervisor
│   │   │   ├── collector.py            # команды супервизоров
│   │   │   ├── middleware.py           # supervisor_middleware 
│   │   │   └── app.py                  # FastAPI для supervisor
│   │   │
│   │   └── template.py                 # общие шаблоны
│   │
│   ├── services/                       # общие сервисы 
│   │   ├── ticket_service.py
│   │   ├── support_service.py
│   │   ├── supervisor_service.py
│   │   ├── sla_service.py
│   │   ├── export_service.py
│   │   ├── audit_service.py
│   │   ├── cache_service.py           # кеш
│   │   └── lock_service.py            # управление замком
│   │
│   ├── db/                             # общая БД 
│   ├── config.py
│   └── worker.py                       # фоновые задачи
│
├── main_support.py                     # вход: uvicorn main_support:app
├── main_supervisor.py                  # вход: uvicorn main_supervisor:app
├── main_worker.py                      # вход: python main_worker.py
├── docker-compose.yml
└── Dockerfile
~~~


### Принципы слоистой архитектуры

- **Middleware** — тонкий роутинг, не знает бизнес-логики
- **Services** — бизнес-логика, не знают о pybotx-специфике
- **Repositories** — доступ к БД, только `AsyncSession`
- **Атомарные операции** — все критичные изменения состояния через `UPDATE ... WHERE ... RETURNING`
- **Correlation ID** — через `contextvars`, связывает события одного входящего сообщения

---

## 🚀 Быстрый старт

### Требования

- Docker ≥ 24
- Docker Compose ≥ 2.20
- Git

### Установка

```bash
# 1. Клонировать репозиторий
git clone <repo-url> support-bot
cd support-bot
```
# 2. Скопировать .env.example → .env и заполнить
```bash
cp .env.template.example .env.template
nano .env.template
```
# 3. Собрать и запустить сервисы
```bash
docker compose up -d --build
```
# 4. Применить миграции
```bash
docker compose run --rm migrate alembic upgrade head
```
# 5. Создать первого супервизора (bootstrap)
```bash
docker compose run --rm support_bot \
  python -m scripts.create_supervisor \
  "<ваш-huid>" "<ФИО>"
```
# 6. Проверить, что всё работает
```bash
docker compose ps
curl http://localhost:8000/status
```
Проверка после запуска
Откройте eXpress, напишите боту в личку любой текст

Бот должен создать тикет и ответить приветствием

Супервизор → /sv_start → должна открыться панель

## ⚙️ Конфигурация 
.env — переменные окружения
env
#### ── Основной бот поддержки ──────────────────────────────
```
BOT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SECRET_KEY=your_secret_key_here
CTS_URL=https://cts.express.company.com
```
#### ── Бот супервизора ─────────────────────────────────────
```
SUPERVISOR_BOT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SUPERVISOR_SECRET_KEY=your_supervisor_secret_key
SUPERVISOR_CTS_URL=https://cts.express.company.com
```
### ── ID чата поддержки (общий групповой чат операторов) ──
```
SUPPORT_CHAT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```
#### ── База данных ─────────────────────────────────────────
```
DATABASE_URL=postgresql+asyncpg://user:pass@db:5432/support_bot
```
#### ── Redis ───────────────────────────────────────────────
```
REDIS_URL=redis://redis:6379/0
```
#### ── Bootstrap первого супервизора (опционально) ─────────
```
BOOTSTRAP_SUPERVISOR_HUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
BOOTSTRAP_SUPERVISOR_NAME='Иванов Иван Иванович'
```
config.py — параметры приложения
```python
class UserResponseConfig:
    REMINDER_AFTER = timedelta(minutes=5)          # Напоминание
    PENDING_AFTER_REMINDER = timedelta(minutes=5)  # Уход в PENDING
    CLOSE_PENDING_AFTER = timedelta(hours=2)       # Автозакрытие
    CLOSE_REQUEST_TIMEOUT = timedelta(minutes=10)  # Таймаут запроса закрытия

class SLAConfig:
    DEADLINE_HOURS = 8        # Дедлайн от создания
    WARNING_HOURS = 4         # 🟡 первое предупреждение
    CRITICAL_HOURS = 2        # 🟠 эскалация
    CHECK_INTERVAL_SEC = 900  # Проверка каждые 15 минут

class AuditConfig:
    RETENTION_DAYS = 365              # Хранение аудита
    CLEANUP_INTERVAL_SEC = 86_400     # Очистка раз в сутки

class RegistrationConfig:
    REQUEST_COOLDOWN_SEC = 60         # Антиспам регистрации оператора
    MAX_BULK_SIZE = 500               # Максимум строк в bulk-загрузке

class SupervisorInviteConfig:
    TTL_HOURS = 24                    # Время жизни инвайта
    MAX_ACTIVE_PER_SUPERVISOR = 1     # Один активный инвайт
```
### 🗄 Миграции БД
```bash
# Создать миграцию (автогенерация по моделям)
docker compose run --rm migrate alembic revision --autogenerate -m "add new field"

# Создать пустую миграцию (для ручного кода)
docker compose run --rm migrate alembic revision -m "add custom migration"

# Применить все миграции
docker compose run --rm migrate alembic upgrade head

# Откатить последнюю
docker compose run --rm migrate alembic downgrade -1

# Текущая ревизия
docker compose run --rm migrate alembic current

# История
docker compose run --rm migrate alembic history
Список миграций:
```
### Ревизия	Что добавляет
|Ревизия| Описание |
|---|---|
|0001|	Базовая схема: support, supervisors, tickets, ticket_transfers|
|0002|Поля PENDING-логики + close_request_sent_at|
|0003|	Таблица audit_logs + индексы|
|0004|Расширение pg_trgm + GIN-индексы по ФИО|
|0005|GIN-индекс по audit_logs.actor_name|
|0006|Таблица support_registration_requests|
|0007|Таблица supervisor_invites|
⚠️ Важно: CREATE INDEX CONCURRENTLY и CREATE EXTENSION требуют autocommit_block(). См. миграции 0004 и 0005 как пример.

### 🎮 Команды бота
#### Пользователь
|Команда|Описание|
|---|---|
|/start|Начать общение с поддержкой|
|/status|Узнать статус обращения|
|/online|Начать смену (создаёт заявку супервизору, если не зарегистрирован)|
#### Оператор поддержки
|Команда|Описание|
|---|---|
|/online|Начать смену|
|/offline|Завершить смену|
|/take <ID>|Взять тикет в работу|
|/take_next|Взять следующий из очереди|
|/my_tickets|Мои активные заявки|
|/list|Список открытых тикетов|
|/transfer <ID>|Передать тикет коллеге|
|/history <ID>|История переводов тикета|
|/receive_ticket <ID> <huid> [comment]|Принять перевод|
#### Супервизор
| Команда                                                    | Описание                                 |
|------------------------------------------------------------|------------------------------------------|
| /sv_start `<code>`                                         | Панель супервизора / регистрация по коду |
| /sv_queue| Очередь и распределение                  |
| /sv_sla| 	SLA всех активных тикетов               |
| /sv_list	Все| активные тикеты                          |
| /sv_spec_list| 	Специалисты в сети + смена статуса      |
| /sv_transfer| Перевод тикета между операторами         |
| /sv_join <ID>| 	Подключиться к треду тикета             |
| /sv_request_ticket| 	Закрыть тикет вручную                   |
| /sv_pending	| Отложенные тикеты (PENDING)              |
| /sv_reactivate <ID>	| Вернуть в очередь с приоритетом          |
| /sv_reactivate_to <ID>| 	Назначить конкретному специалисту       |
| /sv_pending_reg	| Заявки на регистрацию операторов         |
| /sv_approve_reg <ID>	| Одобрить регистрацию                     |
| /sv_reject_reg <ID> [reason]| 	Отклонить регистрацию                   |
| /sv_upload_specialists| 	Массовая загрузка операторов из CSV     |
| /sv_invite_create	| Создать инвайт для нового супервизора    |
| /sv_invite_list| 	Мои приглашения                         |
| /sv_export	| Все тикеты в CSV                         |
| /sv_export_messages| 	Переписка в CSV                         |
| /sv_export_support <ФИО> [limit]| 	Тикеты по специалисту                   |
| /sv_export_user <ФИО> [limit]| 	Тикеты по пользователю                  |
| /sv_export_period <from> <to> [limit]	| Тикеты за период                         |
| /sv_audit <ID>	| Аудит по тикету                          |
| /sv_audit_actor <ФИО или HUID>| 	Аудит по сотруднику                     |
| /sv_audit_corr <correlation_id>| 	Цепочка одного сообщения                |
|  /sv_audit_recent [N]| 	Последние N событий                     |
#### Фоновые задачи
Все запускаются в lifespan и корректно отменяются при остановке.

| Задача	            | Интервал                                  | 	Назначение                         |
|--------------------|-------------------------------------------|-------------------------------------|
| auto_assign_queue  | 	15 сек                                   | 	Назначение тикетов из очереди      |
| auto_close_tickets | 	1 час                                    | 	Закрытие SOLVED-тикетов старше 24ч |
|sla_monitor_loop	| 15 мин                                    | 	SLA-мониторинг и эскалация         |
|user_response_monitor_loop	| 15 сек| 	Напоминания, PENDING, автозакрытие |
|audit_retention_loop| 	24 часа                                  | 	Очистка старых записей аудита      |
#### Гарантии
Graceful shutdown: каждая задача отменяется явно через .cancel()

Ошибки не роняют цикл: try/except вокруг тела итерации

Идемпотентность: через metadata_.sla_checkpoints и RETURNING в SQL

⚠️ Для multi-worker деплоя (несколько воркеров Gunicorn) нужен распределённый lock в Redis, иначе фоновые задачи стартуют в каждом воркере. Пока в проекте используется один процесс Uvicorn.


#### Полезные SQL-запросы для отладки
```sql
-- Тикеты в PENDING старше 1 часа
SELECT ticket_id, user_name, pending_since
FROM tickets
WHERE status = 'pending' AND pending_since < now() - interval '1 hour';

-- Нарушения SLA за последние 24 часа
SELECT ticket_id, user_name, sla_deadline
FROM tickets
WHERE sla_breached = TRUE AND closed_at > now() - interval '24 hours';

-- Последние события аудита
SELECT created_at, actor_name, action, severity
FROM audit_logs
ORDER BY created_at DESC
LIMIT 50;

-- Активные заявки на регистрацию операторов
SELECT id, full_name, created_at
FROM support_registration_requests
WHERE status = 'pending'
ORDER BY created_at;
```
#### 🔧 Troubleshooting
Бот не отвечает на webhook
```bash
# 1. Проверить /status
curl http://localhost:8000/status

# 2. Логи приложения
docker compose logs -f support_bot

# 3. Проверить webhook в eXpress (CTS)
# Убедиться, что URL webhook доступен снаружи
Ошибка миграции Multiple head revisions
bash
# Смотрим головы
docker compose run --rm migrate alembic heads

# Если больше одной — удалить дубли или слить
docker compose run --rm migrate alembic merge -m "merge heads" <rev1> <rev2>
Ошибка CREATE INDEX CONCURRENTLY cannot run inside a transaction block
```
Обернуть в autocommit_block():

```python
with op.get_context().autocommit_block():
    op.execute("CREATE INDEX CONCURRENTLY ...")
```
#### Foreign key violation при удалении оператора
Операторы не удаляются, а переводятся в OFFLINE. Если нужно деактивировать супервизора — UPDATE supervisors SET is_active = FALSE.

#### Фоновые задачи дублируются
Проверьте, что запущен один процесс Uvicorn. Для multi-worker нужен распределённый lock в Redis.

#### Ошибка записи в аудит не роняет бота
Это by design: AuditService.log оборачивает запись в try/except. Ошибки попадают в лог с префиксом [AUDIT]. Если аудит не пишется — проверьте права на таблицу audit_logs и объём места на диске.

#### Медиафайлы теряются при первом сообщении
Убедитесь, что используется версия middleware, где тред поддержки создаётся при создании тикета, а не при назначении оператора. См. ticket_middleware.py → ветка else → create_chat_for_support.

### 📚 Документация
Архитектура и жизненный цикл тикетов — Confluence: «Support Bot / Architecture»

Аудит, экспорт, модерация — Confluence: «Support Bot / Extended Features»

Регистрация супервизоров — Confluence: «Support Bot / Supervisor Invites»

Возврат PENDING-тикетов — Confluence: «Support Bot / PENDING Reactivation»

