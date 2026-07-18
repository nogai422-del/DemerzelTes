# Состояние анкеты и предупреждений об ограничениях.

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from bot.database import db
from bot.notification_delivery import resolve_notification_source_path

_SCHEMA_READY = False
_WARNING_LOCKS: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
_SEEN_MEDIA_GROUPS: dict[tuple[int, int, str], float] = {}
_MEDIA_GROUP_TTL_SECONDS = 60.0

FORM_STAGE_NEW = "new"
FORM_STAGE_FILLING = "filling"
FORM_STAGE_SAVED = "saved"

ONBOARDING_PERMISSION_TYPE = "onboarding"
ONBOARDING_TITLE = "До получения анкеты"
ONBOARDING_MESSAGE = (
    "{user}, сейчас вы не можете использовать медиа и другие донат-возможности. "
    "Сначала получите анкету командой /bv и заполните её."
)

FORM_FILLING_PREFIX = "form_filling:"
FORM_FILLING_STAGE_TITLE = "Во время заполнения анкеты"

POST_SAVE_GENERIC_PERMISSION_TYPE = "other_media"

_POST_SAVE_DEFAULTS = {
    "photo": (
        "Фотографии",
        "{user}, фотографии доступны с уровнем Медиа 1 или Медиа 2.",
    ),
    "video": (
        "Видео",
        "{user}, видео доступны с уровнем Медиа 1 или Медиа 2.",
    ),
    "animation": (
        "GIF / Анимации",
        "{user}, GIF и анимации доступны с уровнем Медиа 1 или Медиа 2.",
    ),
    "document": (
        "Файлы",
        "{user}, файлы и документы доступны только с уровнем Медиа 2.",
    ),
    "audio": (
        "Аудио",
        "{user}, музыка и аудиофайлы доступны только с уровнем Медиа 2.",
    ),
    "voice": (
        "Голосовые сообщения",
        "{user}, для голосовых сообщений нужен отдельный активный донат.",
    ),
    "video_note": (
        "Видео-сообщения",
        "{user}, для кружков нужен отдельный активный донат.",
    ),
    "emoji": (
        "Эмодзи",
        "{user}, для смайликов нужен отдельный активный донат.",
    ),
    "badword": (
        "Мат",
        "{user}, ругательства и запрещённые слова доступны только с уровнем Медиа 2.",
    ),
    "link": (
        "Ссылки",
        "{user}, ссылки доступны только с уровнем Медиа 2.",
    ),
    "sticker": (
        "Стикеры",
        "{user}, стикеры в этом чате отключены. Сообщение удалено.",
    ),
    POST_SAVE_GENERIC_PERMISSION_TYPE: (
        "Другой тип вложения",
        "{user}, этот тип вложения недоступен на вашем текущем медиа-уровне. Сообщение удалено.",
    ),
    "view": (
        "Просмотр анкет",
        "{user}, для просмотра чужих анкет требуется отдельное разрешение.",
    ),
}


def post_save_permission_type(permission_type: str) -> str:
    """Возвращает существующую категорию обычного уведомления после /save."""
    value = str(permission_type or "").strip()
    return value if value in _POST_SAVE_DEFAULTS else POST_SAVE_GENERIC_PERMISSION_TYPE


_FORM_FILLING_DEFAULTS = {
    "badword": (
        "Мат в анкете",
        "{user}, сообщение удалено: во время заполнения анкеты нельзя использовать "
        "запрещённые слова. Исправьте ответ и отправьте его заново.",
    ),
    "emoji": (
        "Смайлики в анкете",
        "{user}, сообщение удалено: во время заполнения анкеты отправляйте ответы "
        "без смайликов. Исправьте ответ и повторите отправку.",
    ),
    "link": (
        "Ссылки в анкете",
        "{user}, сообщение удалено: во время заполнения анкеты нельзя добавлять "
        "ссылки. Уберите ссылку и отправьте ответ заново.",
    ),
    "photo": (
        "Фотографии во время анкеты",
        "{user}, фотография удалена. Пока анкета не сохранена командой /save, "
        "отправляйте ответы обычным текстом.",
    ),
    "video": (
        "Видео во время анкеты",
        "{user}, видео удалено. Пока анкета не сохранена командой /save, "
        "отправляйте ответы обычным текстом.",
    ),
    "animation": (
        "GIF во время анкеты",
        "{user}, GIF удалена. Пока анкета не сохранена командой /save, "
        "отправляйте ответы обычным текстом.",
    ),
    "audio": (
        "Аудио во время анкеты",
        "{user}, аудиофайл удалён. Пока анкета не сохранена командой /save, "
        "отправляйте ответы обычным текстом.",
    ),
    "document": (
        "Файлы во время анкеты",
        "{user}, файл удалён. Пока анкета не сохранена командой /save, "
        "отправляйте ответы обычным текстом.",
    ),
    "voice": (
        "Голосовые во время анкеты",
        "{user}, голосовое сообщение удалено. Пока анкета не сохранена командой "
        "/save, отправляйте ответы обычным текстом.",
    ),
    "video_note": (
        "Кружки во время анкеты",
        "{user}, кружок удалён. Пока анкета не сохранена командой /save, "
        "отправляйте ответы обычным текстом.",
    ),
    "reaction": (
        "Реакции во время анкеты",
        "{user}, реакция удалена. До сохранения анкеты командой /save реакции "
        "недоступны.",
    ),
    "sticker": (
        "Стикеры во время анкеты",
        "{user}, стикер удалён. Пока анкета не сохранена командой /save, "
        "отправляйте ответы обычным текстом.",
    ),
}


def form_filling_permission_type(permission_type: str) -> str:
    return f"{FORM_FILLING_PREFIX}{permission_type}"


def is_form_filling_permission_type(permission_type: str) -> bool:
    return str(permission_type).startswith(FORM_FILLING_PREFIX)


def base_permission_type(permission_type: str) -> str:
    value = str(permission_type)
    return value[len(FORM_FILLING_PREFIX):] if is_form_filling_permission_type(value) else value


async def ensure_warning_schema() -> None:
    """Создаёт таблицы предупреждений и шаблоны для трёх этапов анкеты."""
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
            INSERT INTO permission_types (
                media_type, title, message, image_path, button_text, button_url
            ) VALUES (?, ?, ?, '', '', '')
            ON CONFLICT(media_type) DO NOTHING
            """,
            (ONBOARDING_PERMISSION_TYPE, ONBOARDING_TITLE, ONBOARDING_MESSAGE),
        )

        # Полный набор обычных шаблонов после /save. Существующие тексты
        # администратора не перезаписываются.
        for permission_type, (title, message) in _POST_SAVE_DEFAULTS.items():
            await cur.execute(
                """
                INSERT OR IGNORE INTO permission_types (
                    media_type, title, message, image_path, button_text, button_url
                ) VALUES (?, ?, ?, '', '', '')
                """,
                (permission_type, title, message),
            )

        # У каждого ограничения есть отдельный шаблон для промежутка /bv → /save.
        # INSERT OR IGNORE сохраняет любые тексты, уже настроенные администратором.
        for permission_type, (title, message) in _FORM_FILLING_DEFAULTS.items():
            await cur.execute(
                """
                INSERT OR IGNORE INTO permission_types (
                    media_type, title, message, image_path, button_text, button_url
                ) VALUES (?, ?, ?, '', '', '')
                """,
                (form_filling_permission_type(permission_type), title, message),
            )

        # Удаляем только мёртвые ссылки на файлы. Такие ссылки остаются после
        # старых пересборок Bothost, когда БД сохранялась, а uploads внутри
        # контейнера исчезали. Тексты и кнопки не затрагиваются.
        await cur.execute(
            "SELECT media_type, image_path FROM permission_types "
            "WHERE TRIM(COALESCE(image_path,'')) <> ''"
        )
        stale_types = []
        for media_type, image_path in await cur.fetchall():
            source = resolve_notification_source_path(str(image_path or ""))
            if source is None or not source.is_file():
                stale_types.append(str(media_type))
        if stale_types:
            await cur.executemany(
                "UPDATE permission_types SET image_path='' WHERE media_type=?",
                [(item,) for item in stale_types],
            )
            print(f"Удалены мёртвые ссылки картинок оповещений: {len(stale_types)}")

    _SCHEMA_READY = True


async def get_form_stage(chat_id: int, user_id: int) -> str:
    """Возвращает этап: до /bv, заполнение между /bv и /save, либо сохранено."""
    async with db() as cur:
        await cur.execute(
            """
            SELECT filled_form_text, form_stage
            FROM chat_users
            WHERE chat_id = ? AND user_id = ?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (int(chat_id), int(user_id)),
        )
        row = await cur.fetchone()

        if row and (
            str(row[0] or "").strip()
            or str(row[1] or "").strip() == FORM_STAGE_SAVED
        ):
            return FORM_STAGE_SAVED
        if row and str(row[1] or "").strip() == FORM_STAGE_FILLING:
            return FORM_STAGE_FILLING

        await cur.execute(
            """
            SELECT 1 FROM bv_messages
            WHERE chat_id = ? AND target_user_id = ?
            LIMIT 1
            """,
            (int(chat_id), int(user_id)),
        )
        if await cur.fetchone() is not None:
            return FORM_STAGE_FILLING

    return FORM_STAGE_NEW


async def mark_form_started(chat_id: int, user_id: int) -> str:
    """Переводит нового пользователя в промежуточный этап после отправки /bv."""
    now = int(time.time())
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO chat_users (chat_id, user_id, form_stage, form_started_at)
            VALUES (?, ?, 'filling', ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                form_stage = CASE
                    WHEN TRIM(COALESCE(chat_users.filled_form_text, '')) <> ''
                         OR chat_users.form_stage = 'saved'
                    THEN 'saved'
                    ELSE 'filling'
                END,
                form_started_at = CASE
                    WHEN chat_users.form_started_at > 0
                    THEN chat_users.form_started_at ELSE excluded.form_started_at
                END
            """,
            (int(chat_id), int(user_id), now),
        )
    return await get_form_stage(chat_id, user_id)


async def mark_form_saved(chat_id: int, user_id: int) -> None:
    """Фиксирует переход после /save; текст анкеты сохраняется отдельно."""
    now = int(time.time())
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO chat_users (chat_id, user_id, form_stage, form_saved_at)
            VALUES (?, ?, 'saved', ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                form_stage = 'saved',
                form_saved_at = excluded.form_saved_at
            """,
            (int(chat_id), int(user_id), now),
        )


async def has_completed_form(chat_id: int, user_id: int) -> bool:
    return await get_form_stage(chat_id, user_id) == FORM_STAGE_SAVED


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
    """Удаляет прошлую карточку пользователя и отправляет одну актуальную."""
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

        # Сначала отправляем новую карточку. Старую удаляем только после
        # успешной доставки — битая картинка или временная ошибка Telegram
        # больше не оставляет пользователя вообще без предупреждения.
        old_message_id = await _get_warning_message_id(*key)
        try:
            sent = await sender()
        except Exception:
            if media_key is not None:
                _SEEN_MEDIA_GROUPS.pop(media_key, None)
            raise

        new_message_id = getattr(sent, "message_id", None) if sent is not None else None
        if new_message_id is None:
            if media_key is not None:
                _SEEN_MEDIA_GROUPS.pop(media_key, None)
            return None

        await _save_warning_message_id(key[0], key[1], int(new_message_id))

        if old_message_id and int(old_message_id) != int(new_message_id):
            try:
                await bot.delete_message(key[0], int(old_message_id))
            except Exception:
                # Новое сообщение уже доставлено; невозможность удалить старое
                # не должна откатывать состояние.
                pass
        return sent


async def clear_warning(bot: Any, chat_id: int, user_id: int) -> None:
    """Удаляет активное предупреждение пользователя при смене этапа."""
    key = (int(chat_id), int(user_id))
    async with _WARNING_LOCKS[key]:
        old_message_id = await _get_warning_message_id(*key)
        if old_message_id:
            try:
                await bot.delete_message(key[0], old_message_id)
            except Exception:
                pass
        await _forget_warning(*key)
