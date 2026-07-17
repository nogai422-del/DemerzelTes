# Состояние предупреждений об ограничениях: одна актуальная карточка на пользователя.

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from bot.database import db

_SCHEMA_READY = False
_WARNING_LOCKS: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
_SEEN_MEDIA_GROUPS: dict[tuple[int, int, str], float] = {}
_MEDIA_GROUP_TTL_SECONDS = 60.0

ONBOARDING_PERMISSION_TYPE = "onboarding"
ONBOARDING_TITLE = "До сохранения анкеты"
ONBOARDING_MESSAGE = (
    "{user}, сейчас вы не можете использовать медиа и другие донат-возможности. "
    "Сначала заполните анкету, которую отправит администратор командой /bv. "
    "После сохранения анкеты командой /save будут показываться отдельные "
    "уведомления по каждому виду доната."
)


async def ensure_warning_schema() -> None:
    """Создаёт новый формат хранения предупреждений и общий шаблон до /save."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    async with db() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS permission_types (
                media_type  TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                message     TEXT NOT NULL,
                image_path  TEXT NOT NULL DEFAULT '',
                button_text TEXT NOT NULL DEFAULT '',
                button_url  TEXT NOT NULL DEFAULT ''
            )
            """
        )

        await cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_messages'"
        )
        table_exists = await cur.fetchone() is not None

        if table_exists:
            await cur.execute("PRAGMA table_info(permission_messages)")
            columns = {str(row[1]) for row in await cur.fetchall()}
            if "user_id" not in columns:
                # Старый формат хранил одно сообщение на весь чат и не позволял
                # корректно заменять предупреждение конкретного пользователя.
                await cur.execute("DROP TABLE permission_messages")
                table_exists = False

        if not table_exists:
            await cur.execute(
                """
                CREATE TABLE permission_messages (
                    chat_id    INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    send_time  REAL NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )

        await cur.execute(
            """
            INSERT OR IGNORE INTO permission_types (
                media_type, title, message, image_path, button_text, button_url
            ) VALUES (?, ?, ?, '', '', '')
            """,
            (ONBOARDING_PERMISSION_TYPE, ONBOARDING_TITLE, ONBOARDING_MESSAGE),
        )

    _SCHEMA_READY = True


async def has_completed_form(chat_id: int, user_id: int) -> bool:
    """Анкета считается завершённой после /save с непустым текстом."""
    async with db() as cur:
        await cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_users'"
        )
        if await cur.fetchone() is None:
            return False
        await cur.execute(
            """
            SELECT filled_form_text
            FROM chat_users
            WHERE chat_id = ? AND user_id = ?
            """,
            (int(chat_id), int(user_id)),
        )
        row = await cur.fetchone()
    return bool(row and str(row[0] or "").strip())


async def _get_warning_message_id(chat_id: int, user_id: int) -> int | None:
    await ensure_warning_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT message_id
            FROM permission_messages
            WHERE chat_id = ? AND user_id = ?
            """,
            (int(chat_id), int(user_id)),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def _save_warning_message_id(chat_id: int, user_id: int, message_id: int) -> None:
    await ensure_warning_schema()
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO permission_messages (chat_id, user_id, message_id, send_time)
            VALUES (?, ?, ?, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                message_id = excluded.message_id,
                send_time = excluded.send_time
            """,
            (int(chat_id), int(user_id), int(message_id)),
        )


async def _forget_warning(chat_id: int, user_id: int) -> None:
    await ensure_warning_schema()
    async with db() as cur:
        await cur.execute(
            "DELETE FROM permission_messages WHERE chat_id = ? AND user_id = ?",
            (int(chat_id), int(user_id)),
        )


def _cleanup_media_groups(now: float) -> None:
    if len(_SEEN_MEDIA_GROUPS) < 1000:
        return
    threshold = now - _MEDIA_GROUP_TTL_SECONDS
    stale = [key for key, seen_at in _SEEN_MEDIA_GROUPS.items() if seen_at < threshold]
    for key in stale:
        _SEEN_MEDIA_GROUPS.pop(key, None)


async def replace_warning(
    bot: Any,
    *,
    chat_id: int,
    user_id: int,
    sender: Callable[[], Awaitable[Any]],
    media_group_id: str | None = None,
) -> Any | None:
    """
    Удаляет предыдущее предупреждение пользователя и отправляет новое.
    Для Telegram-альбома отправляет только одно предупреждение на всю пачку.
    """
    key = (int(chat_id), int(user_id))
    async with _WARNING_LOCKS[key]:
        media_key: tuple[int, int, str] | None = None
        if media_group_id:
            now = time.monotonic()
            media_key = (key[0], key[1], str(media_group_id))
            seen_at = _SEEN_MEDIA_GROUPS.get(media_key)
            if seen_at is not None and now - seen_at < _MEDIA_GROUP_TTL_SECONDS:
                return None
            _SEEN_MEDIA_GROUPS[media_key] = now
            _cleanup_media_groups(now)

        old_message_id = await _get_warning_message_id(*key)
        if old_message_id:
            try:
                await bot.delete_message(key[0], old_message_id)
            except Exception:
                pass
            finally:
                await _forget_warning(*key)

        try:
            sent = await sender()
        except Exception:
            if media_key is not None:
                _SEEN_MEDIA_GROUPS.pop(media_key, None)
            raise

        if sent is not None and getattr(sent, "message_id", None) is not None:
            await _save_warning_message_id(key[0], key[1], int(sent.message_id))
        return sent


async def clear_warning(bot: Any, chat_id: int, user_id: int) -> None:
    """Удаляет активное предупреждение пользователя, например после /save."""
    key = (int(chat_id), int(user_id))
    async with _WARNING_LOCKS[key]:
        old_message_id = await _get_warning_message_id(*key)
        if old_message_id:
            try:
                await bot.delete_message(key[0], old_message_id)
            except Exception:
                pass
        await _forget_warning(*key)
