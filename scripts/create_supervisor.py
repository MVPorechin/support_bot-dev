"""
Создание первого супервизора вручную (bootstrap).

Использование:
    python -m scripts.create_supervisor <huid> "<ФИО>"

Пример:
    python -m scripts.create_supervisor \
        "856ac747-d98b-5cca-8619-80b9e7b6b58b" \
        "Иванов Иван Иванович"

Проверяет, что супервизор ещё не создан (идемпотентно).
"""
import asyncio
import logging
import sys
import uuid

from services.supervisor_service import SupervisorService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("scripts.create_supervisor")


async def _create(huid: uuid.UUID, full_name: str) -> int:
    svc = SupervisorService()

    existing = await svc.get_by_id(huid)
    if existing is not None:
        logger.info(
            f"ℹ️  Супервизор уже существует: "
            f"{existing.full_name} ({existing.supervisor_id})"
        )
        return 0

    sup = await svc.get_or_create(
        supervisor_id=huid,
        full_name=full_name,
    )
    logger.info(
        f"✅ Супервизор создан: "
        f"{sup.full_name} ({sup.supervisor_id})"
    )
    return 0


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Использование:\n"
            "  python -m scripts.create_supervisor <huid> \"<ФИО>\"\n"
            "\n"
            "Пример:\n"
            "  python -m scripts.create_supervisor \\\n"
            "    \"856ac747-d98b-5cca-8619-80b9e7b6b58b\" \\\n"
            "    \"Иванов Иван Иванович\""
        )
        sys.exit(1)

    try:
        huid = uuid.UUID(sys.argv[1])
    except (ValueError, TypeError):
        print(f"❌ Неверный HUID: {sys.argv[1]}")
        sys.exit(1)

    full_name = sys.argv[2].strip()
    if not full_name:
        print("❌ ФИО не может быть пустым")
        sys.exit(1)

    exit_code = asyncio.run(_create(huid, full_name))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()