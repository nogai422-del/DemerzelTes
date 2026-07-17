# Настройки поведения бота, которые можно менять из админ-панели.

from bot.database import db

_SCHEMA_READY = False


async def ensure_chat_behavior_schema() -> None:
    """Создаёт таблицу общих переключателей поведения бота."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    async with db() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_behavior_settings (
                id                            INTEGER PRIMARY KEY CHECK (id = 1),
                restrict_new_members_telegram INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await cur.execute(
            """
            INSERT OR IGNORE INTO chat_behavior_settings (
                id, restrict_new_members_telegram
            ) VALUES (1, 0)
            """
        )

    _SCHEMA_READY = True


async def get_restrict_new_members_telegram() -> bool:
    """Возвращает состояние старого Telegram-ограничения при входе.

    По умолчанию функция выключена: доступами управляет новый алгоритм бота,
    а Telegram не урезает медиа-права участника сразу после присоединения.
    """
    await ensure_chat_behavior_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT restrict_new_members_telegram
            FROM chat_behavior_settings
            WHERE id = 1
            """
        )
        row = await cur.fetchone()
    return bool(int(row[0] if row else 0))


async def set_restrict_new_members_telegram(enabled: bool) -> None:
    """Включает или выключает прежнее ограничение новых участников в Telegram."""
    await ensure_chat_behavior_schema()
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO chat_behavior_settings (
                id, restrict_new_members_telegram
            ) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET
                restrict_new_members_telegram = excluded.restrict_new_members_telegram
            """,
            (1 if enabled else 0,),
        )
