"""Точка входа для бота супервизора.
Запуск: uvicorn main_supervisor:app --host 0.0.0.0 --port 8001
"""
import logging
from bot.supervisor.app import app   # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)