"""Точка входа для бота поддержки.
Запуск: uvicorn main_support:app --host 0.0.0.0 --port 8000
"""
import logging
from bot.support.app import app   # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)