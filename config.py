import os
from dataclasses import dataclass
from dotenv import load_dotenv
from uuid import UUID
from datetime import timedelta
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


# ══════════════════════════════════════════════════════════════
# Pydantic-settings
# ══════════════════════════════════════════════════════════════
class SupportBotConfig(BaseSettings):
    """Конфиг бота поддержки (пользователи + операторы)."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_id: UUID
    secret_key: str
    cts_url: str
    support_chat_id: UUID


class SupervisorBotConfig(BaseSettings):
    """Конфиг бота супервизора."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supervisor_bot_id: UUID
    supervisor_secret_key: str
    supervisor_cts_url: str


class RedisConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    redis_url: str = "redis://support_redis:6379/0"

    @property
    def URL(self) -> str:
        """Alias для удобства использования."""
        return self.redis_url


class DatabaseConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str


@lru_cache(maxsize=1)
def get_redis_config() -> RedisConfig:
    return RedisConfig()


# ══════════════════════════════════════════════════════════════
# Backward-compat: старые конфиги через os.getenv
# ══════════════════════════════════════════════════════════════
class BotConfig:
    BOT_ID = os.getenv("BOT_ID")
    SECRET_KEY = os.getenv("SECRET_KEY")
    CTS_URL = os.getenv("CTS_URL")

    HOST = "0.0.0.0"
    PORT = int(os.getenv("PORT", 8000))
    SUPPORT_CHAT_ID = os.getenv("SUPPORT_CHAT_ID")

    # ── бот супервизора ──────────────────────────────
    SUPERVISOR_BOT_ID = os.getenv("SUPERVISOR_BOT_ID")
    SUPERVISOR_SECRET_KEY = os.getenv("SUPERVISOR_SECRET_KEY")
    # ВАЖНО: исправлено — читаем явную переменную, а не CTS_URL
    SUPERVISOR_CTS_URL = os.getenv(
        "SUPERVISOR_CTS_URL",
        os.getenv("CTS_URL"),   # fallback для dev
    )
    # ВАЖНО: исправлено — либо удалите, либо явно
    SUPERVISOR_CHAT_ID = os.getenv(
        "SUPERVISOR_CHAT_ID",
        os.getenv("SUPPORT_CHAT_ID"),
    )


class DBConfig:
    DATABASE_URL = os.getenv("DATABASE_URL")
    DEBUG = False


class ITSMConfig:
    ITSM_API_URL = os.getenv("ITSM_API_URL")
    ITSM_TOKEN = os.getenv("ITSM_TOKEN")


# ══════════════════════════════════════════════════════════════
# Бизнес-конфиги (постоянные значения)
# ══════════════════════════════════════════════════════════════
class UserResponseConfig:
    """Таймауты для контроля ответа пользователя в тикете."""
    REMINDER_AFTER = timedelta(minutes=5)
    PENDING_AFTER_REMINDER = timedelta(minutes=15)
    CLOSE_PENDING_AFTER = timedelta(hours=2)
    CLOSE_REQUEST_TIMEOUT = timedelta(minutes=10)


class SLAConfig:
    """Конфигурация SLA — все тайминги в часах."""
    DEADLINE_HOURS: int = 8
    WARNING_HOURS: int = 4
    CRITICAL_HOURS: int = 2
    CHECK_INTERVAL_SEC: int = 900


class AuditConfig:
    """Настройки журнала аудита."""
    RETENTION_DAYS = 365
    CLEANUP_INTERVAL_SEC = 86_400
    CLEANUP_STARTUP_DELAY_SEC = 60


class RegistrationConfig:
    """Параметры модерации регистрации специалистов."""
    REQUEST_COOLDOWN_SEC = 60
    UPLOAD_WINDOW_SEC = 300
    MAX_BULK_SIZE = 500


class SupervisorInviteConfig:
    """Настройки приглашений на роль супервизора."""
    TTL_HOURS = 24
    MAX_ACTIVE_PER_SUPERVISOR = 1