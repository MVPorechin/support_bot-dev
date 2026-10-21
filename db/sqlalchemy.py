# app/db/sqlalchemy.py
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from config import DBConfig  # строка подключения

# Базовый класс для моделей
Base = declarative_base()

# Асинхронный движок
engine = create_async_engine(
    DBConfig.DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=DBConfig.DEBUG,  # логирование SQL-запросов при DEBUG=True
    future=True,
)

# Фабрика сессий
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Основная сессия для использования в контекстных менеджерах
async def get_session() -> AsyncGenerator[AsyncSession | Any, Any]:
    async with async_session_factory() as session:
        yield session


# Удобная переменная для импорта (будет использоваться в репозиториях)
session = async_session_factory
