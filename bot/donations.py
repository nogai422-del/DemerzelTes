# Учёт срочных донат-пакетов и отправка уведомлений об окончании срока.

import asyncio
import math
import os
import time
from typing import Any

from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
)
from aiogram.utils.formatting import html_decoration as hd

from bot.database import db
from bot.message_queue import bot_send_message, bot_send_photo_to_chat, bot_send_animation_to_chat
from bot.warning_state import replace_warning
from bot.utils import resolve_bot_image_path

DAY_SECONDS = 86400
DEFAULT_LONG_PACKAGE_MIN_DAYS = 28
DEFAULT_PREEXPIRY_NOTICE_DAYS = 3
CHECK_INTERVAL_SECONDS = 60
_SCHEMA_READY = False

CATEGORY_TITLES = {
    "voice": "Голосовые сообщения",
    "emoji": "Смайлики",
    "tag": "Тег",
    "video_note": "Кружки",
}

EVENT_TITLES = {
    "preexpiry": "Предупреждение до окончания",
    "expired": "Срок истёк",
    "limit_exhausted": "Дневной лимит исчерпан",
    "denied": "Попытка реакции без Медиа 2",
}

CATEGORY_EVENT_TYPES = {
    "voice": ("preexpiry", "expired", "limit_exhausted"),
    "emoji": ("preexpiry", "expired", "limit_exhausted"),
    "tag": ("preexpiry", "expired"),
    "video_note": ("preexpiry", "expired", "limit_exhausted"),
}

DEFAULT_TEMPLATES = {
    ("voice", "preexpiry"): "{user}, действие доната на голосовые сообщения закончится через {days_left} дн. — {valid_until}.",
    ("voice", "expired"): "{user}, срок действия доната на голосовые сообщения истёк.",
    ("voice", "limit_exhausted"): (
        "{user}, дневной лимит голосовых сообщений ({limit}) исчерпан. "
        "Счётчик сбросится в начале следующего дня."
    ),
    ("emoji", "preexpiry"): "{user}, действие доната на смайлики закончится через {days_left} дн. — {valid_until}.",
    ("emoji", "expired"): "{user}, срок действия доната на смайлики истёк.",
    ("emoji", "limit_exhausted"): (
        "{user}, дневной лимит смайликов ({limit}) исчерпан. "
        "Счётчик сбросится в начале следующего дня."
    ),
    ("tag", "preexpiry"): "{user}, действие доната на тег закончится через {days_left} дн. — {valid_until}.",
    ("tag", "expired"): "{user}, срок действия доната на тег истёк.",
    ("video_note", "preexpiry"): "{user}, действие доната на кружки закончится через {days_left} дн. — {valid_until}.",
    ("video_note", "expired"): "{user}, срок действия доната на кружки истёк.",
    ("video_note", "limit_exhausted"): (
        "{user}, дневной лимит кружков ({limit}) исчерпан. "
        "Счётчик сбросится в начале следующего дня."
    ),
    ("reaction", "denied"): "{user}, реакции доступны только с уровнем Медиа 2. Ваша реакция удалена.",
}

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DONATION_IMAGES_DIR = os.path.join(BASE_DIR, "bot", "images", "donation_images")
os.makedirs(DONATION_IMAGES_DIR, exist_ok=True)


async def ensure_donation_schema() -> None:
    """Создаёт таблицы и базовые шаблоны без отдельной ручной миграции."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    now = int(time.time())

    async with db() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS donation_grants (
                chat_id         INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                category        TEXT NOT NULL,
                valid_until     INTEGER NOT NULL,
                package_days    INTEGER NOT NULL DEFAULT 0,
                daily_limit     INTEGER NOT NULL DEFAULT 0,
                preexpiry_sent  INTEGER NOT NULL DEFAULT 0,
                expired_sent    INTEGER NOT NULL DEFAULT 0,
                updated_at      INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id, category)
            )
            """
        )
        await cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_donation_grants_due
            ON donation_grants(valid_until, preexpiry_sent, expired_sent)
            """
        )

        # Миграция существующей базы: персональный суточный лимит хранится
        # прямо в выдаче доната. Нулевое значение означает старый пакет, для
        # которого используется прежний лимит по умолчанию.
        await cur.execute("PRAGMA table_info(donation_grants)")
        grant_columns = {str(row[1]) for row in await cur.fetchall()}
        if "daily_limit" not in grant_columns:
            await cur.execute(
                "ALTER TABLE donation_grants ADD COLUMN daily_limit INTEGER NOT NULL DEFAULT 0"
            )

        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS donation_notification_templates (
                category      TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                title         TEXT NOT NULL,
                message       TEXT NOT NULL DEFAULT '',
                image_path    TEXT NOT NULL DEFAULT '',
                button1_text  TEXT NOT NULL DEFAULT '',
                button1_url   TEXT NOT NULL DEFAULT '',
                button2_text  TEXT NOT NULL DEFAULT '',
                button2_url   TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (category, event_type)
            )
            """
        )

        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS donation_settings (
                id                       INTEGER PRIMARY KEY CHECK (id = 1),
                voice_daily_limit        INTEGER NOT NULL DEFAULT 20,
                emoji_daily_limit        INTEGER NOT NULL DEFAULT 50,
                video_note_daily_limit   INTEGER NOT NULL DEFAULT 10,
                viewd_delete_seconds     INTEGER NOT NULL DEFAULT 30,
                viewmd_delete_seconds    INTEGER NOT NULL DEFAULT 30,
                preexpiry_enabled        INTEGER NOT NULL DEFAULT 1,
                long_package_min_days    INTEGER NOT NULL DEFAULT 28,
                preexpiry_notice_days    INTEGER NOT NULL DEFAULT 3
            )
            """
        )
        await cur.execute("PRAGMA table_info(donation_settings)")
        settings_columns = {str(row[1]) for row in await cur.fetchall()}
        if "voice_daily_limit" not in settings_columns:
            await cur.execute(
                "ALTER TABLE donation_settings ADD COLUMN voice_daily_limit INTEGER NOT NULL DEFAULT 20"
            )
        if "viewd_delete_seconds" not in settings_columns:
            await cur.execute(
                "ALTER TABLE donation_settings ADD COLUMN viewd_delete_seconds INTEGER NOT NULL DEFAULT 30"
            )
        if "viewmd_delete_seconds" not in settings_columns:
            await cur.execute(
                "ALTER TABLE donation_settings ADD COLUMN viewmd_delete_seconds INTEGER NOT NULL DEFAULT 30"
            )
        if "preexpiry_enabled" not in settings_columns:
            await cur.execute(
                "ALTER TABLE donation_settings ADD COLUMN preexpiry_enabled INTEGER NOT NULL DEFAULT 1"
            )
        if "long_package_min_days" not in settings_columns:
            await cur.execute(
                "ALTER TABLE donation_settings ADD COLUMN long_package_min_days INTEGER NOT NULL DEFAULT 28"
            )
        if "preexpiry_notice_days" not in settings_columns:
            await cur.execute(
                "ALTER TABLE donation_settings ADD COLUMN preexpiry_notice_days INTEGER NOT NULL DEFAULT 3"
            )

        await cur.execute(
            """
            INSERT OR IGNORE INTO donation_settings (
                id, voice_daily_limit, emoji_daily_limit, video_note_daily_limit,
                viewd_delete_seconds, viewmd_delete_seconds,
                preexpiry_enabled, long_package_min_days, preexpiry_notice_days
            ) VALUES (1, 20, 50, 10, 30, 30, 1, 28, 3)
            """
        )

        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS donation_usage (
                chat_id      INTEGER NOT NULL,
                user_id      INTEGER NOT NULL,
                category     TEXT NOT NULL,
                used_today   INTEGER NOT NULL DEFAULT 0,
                used_date    TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (chat_id, user_id, category)
            )
            """
        )

        for (category, event_type), message in DEFAULT_TEMPLATES.items():
            category_title = CATEGORY_TITLES.get(category, "Реакции")
            title = f"{category_title} — {EVENT_TITLES[event_type]}"
            await cur.execute(
                """
                INSERT OR IGNORE INTO donation_notification_templates (
                    category, event_type, title, message, image_path,
                    button1_text, button1_url, button2_text, button2_url
                ) VALUES (?, ?, ?, ?, '', '', '', '', '')
                """,
                (category, event_type, title, message),
            )

        # Переводим только прежние стандартные шаблоны предупреждений на
        # настраиваемую подстановку {days_left}. Пользовательские тексты не
        # меняем.
        old_preexpiry_templates = {
            "voice": "{user}, действие доната на голосовые сообщения закончится через 3 дня — {valid_until}.",
            "emoji": "{user}, действие доната на смайлики закончится через 3 дня — {valid_until}.",
            "tag": "{user}, действие доната на тег закончится через 3 дня — {valid_until}.",
            "video_note": "{user}, действие доната на кружки закончится через 3 дня — {valid_until}.",
        }
        for category, old_message in old_preexpiry_templates.items():
            await cur.execute(
                """
                UPDATE donation_notification_templates
                SET title = ?, message = ?
                WHERE category = ? AND event_type = 'preexpiry' AND message = ?
                """,
                (
                    f"{CATEGORY_TITLES[category]} — {EVENT_TITLES['preexpiry']}",
                    DEFAULT_TEMPLATES[(category, "preexpiry")],
                    category,
                    old_message,
                ),
            )

        # Обновляем только прежний стандартный текст реакции. Пользовательский
        # текст из веб-панели не перезаписываем.
        await cur.execute(
            """
            UPDATE donation_notification_templates
            SET title = 'Реакции — попытка без Медиа 2',
                message = ?
            WHERE category = 'reaction'
              AND event_type = 'denied'
              AND message IN (
                  '{user}, у вас нет активного доната на реакции. Реакция удалена.',
                  '{user}, у вас нет действующего доната на реакции. Реакция удалена.'
              )
            """,
            (DEFAULT_TEMPLATES[("reaction", "denied")],),
        )

        # Старые активные записи ГС и смайликов тоже попадут под уведомление
        # об истечении. Исходная длительность старого пакета неизвестна, поэтому
        # для них не включаем предупреждение за 3 дня.
        for table_name, category in (
            ("voice_permissions", "voice"),
            ("emoji_permissions", "emoji"),
        ):
            await cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            )
            if await cur.fetchone() is None:
                continue

            await cur.execute(
                f"""
                INSERT OR IGNORE INTO donation_grants (
                    chat_id, user_id, category, valid_until, package_days, daily_limit,
                    preexpiry_sent, expired_sent, updated_at
                )
                SELECT chat_id, user_id, ?, valid_until, 0, ?, 0, 0, ?
                FROM {table_name}
                WHERE valid_until > ?
                """,
                (category, 20 if category == "voice" else 50, now, now),
            )

        # Сохраняем уже использованное сегодня количество смайлов при переходе
        # со старого счётчика на общий счётчик донат-функций.
        await cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'emoji_permissions'"
        )
        if await cur.fetchone() is not None:
            await cur.execute(
                """
                INSERT OR IGNORE INTO donation_usage (
                    chat_id, user_id, category, used_today, used_date
                )
                SELECT chat_id, user_id, 'emoji',
                       COALESCE(used_today, 0), COALESCE(used_date, '')
                FROM emoji_permissions
                """
            )

    _SCHEMA_READY = True


async def record_donation_grant(
    chat_id: int,
    user_id: int,
    category: str,
    valid_until: int,
    package_days: int,
    daily_limit: int | None = None,
) -> None:
    if category not in CATEGORY_TITLES:
        raise ValueError(f"Неизвестная категория доната: {category}")

    await ensure_donation_schema()
    now = int(time.time())

    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO donation_grants (
                chat_id, user_id, category, valid_until, package_days, daily_limit,
                preexpiry_sent, expired_sent, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(chat_id, user_id, category) DO UPDATE SET
                valid_until = excluded.valid_until,
                package_days = excluded.package_days,
                daily_limit = CASE
                    WHEN excluded.daily_limit > 0 THEN excluded.daily_limit
                    ELSE donation_grants.daily_limit
                END,
                preexpiry_sent = 0,
                expired_sent = 0,
                updated_at = excluded.updated_at
            """,
            (
                chat_id, user_id, category, valid_until, max(0, package_days),
                max(0, int(daily_limit or 0)), now,
            ),
        )


async def extend_donation_grant(
    chat_id: int,
    user_id: int,
    category: str,
    days: int,
    daily_limit: int | None = None,
) -> tuple[int, int]:
    """Продлевает пакет и возвращает (valid_until, полная оставшаяся длительность в днях)."""
    if days <= 0:
        raise ValueError("Количество дней должно быть положительным")

    await ensure_donation_schema()
    now = int(time.time())

    async with db() as cur:
        await cur.execute(
            """
            SELECT valid_until
            FROM donation_grants
            WHERE chat_id = ? AND user_id = ? AND category = ?
            """,
            (chat_id, user_id, category),
        )
        row = await cur.fetchone()

    current_until = int(row[0]) if row else 0
    base_time = current_until if current_until > now else now
    valid_until = base_time + days * DAY_SECONDS
    package_days = max(days, math.ceil((valid_until - now) / DAY_SECONDS))

    await record_donation_grant(
        chat_id=chat_id,
        user_id=user_id,
        category=category,
        valid_until=valid_until,
        package_days=package_days,
        daily_limit=daily_limit,
    )
    return valid_until, package_days


async def revoke_donation_grant(chat_id: int, user_id: int, category: str) -> bool:
    """Удаляет один срочный донат и его суточный счётчик.

    Возвращает ``True``, если активная или сохранённая выдача существовала.
    """
    if category not in CATEGORY_TITLES:
        raise ValueError(f"Неизвестная категория доната: {category}")

    await ensure_donation_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT 1 FROM donation_grants
            WHERE chat_id = ? AND user_id = ? AND category = ?
            """,
            (chat_id, user_id, category),
        )
        existed = await cur.fetchone() is not None
        await cur.execute(
            """
            DELETE FROM donation_grants
            WHERE chat_id = ? AND user_id = ? AND category = ?
            """,
            (chat_id, user_id, category),
        )
        await cur.execute(
            """
            DELETE FROM donation_usage
            WHERE chat_id = ? AND user_id = ? AND category = ?
            """,
            (chat_id, user_id, category),
        )
    return existed


async def revoke_all_donation_grants(chat_id: int, user_id: int) -> list[str]:
    """Удаляет все срочные донаты пользователя и возвращает их категории."""
    await ensure_donation_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT category FROM donation_grants
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        )
        categories = [str(row[0]) for row in await cur.fetchall()]
        await cur.execute(
            "DELETE FROM donation_grants WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await cur.execute(
            "DELETE FROM donation_usage WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
    return categories


async def has_active_donation(chat_id: int, user_id: int, category: str) -> bool:
    await ensure_donation_schema()
    now = int(time.time())

    async with db() as cur:
        await cur.execute(
            """
            SELECT 1
            FROM donation_grants
            WHERE chat_id = ? AND user_id = ? AND category = ? AND valid_until > ?
            """,
            (chat_id, user_id, category, now),
        )
        return await cur.fetchone() is not None


async def get_usage_limits() -> dict[str, int]:
    """Возвращает настраиваемые суточные лимиты донат-функций."""
    await ensure_donation_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT voice_daily_limit, emoji_daily_limit, video_note_daily_limit
            FROM donation_settings
            WHERE id = 1
            """
        )
        row = await cur.fetchone()

    return {
        "voice": max(1, int(row[0] if row else 20)),
        "emoji": max(1, int(row[1] if row else 50)),
        "video_note": max(1, int(row[2] if row else 10)),
    }


async def set_usage_limits(*, voice: int, emoji: int, video_note: int) -> None:
    """Сохраняет суточные лимиты по умолчанию из админ-панели."""
    await ensure_donation_schema()
    voice_limit = max(1, min(int(voice), 100000))
    emoji_limit = max(1, min(int(emoji), 100000))
    video_note_limit = max(1, min(int(video_note), 100000))
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO donation_settings (
                id, voice_daily_limit, emoji_daily_limit, video_note_daily_limit
            ) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                voice_daily_limit = excluded.voice_daily_limit,
                emoji_daily_limit = excluded.emoji_daily_limit,
                video_note_daily_limit = excluded.video_note_daily_limit
            """,
            (voice_limit, emoji_limit, video_note_limit),
        )


async def get_expiry_notification_settings() -> dict[str, int | bool]:
    """Возвращает настройки предупреждений для долгих донат-пакетов."""
    await ensure_donation_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT preexpiry_enabled, long_package_min_days, preexpiry_notice_days
            FROM donation_settings
            WHERE id = 1
            """
        )
        row = await cur.fetchone()

    return {
        "enabled": bool(int(row[0] if row else 1)),
        "min_package_days": max(
            DEFAULT_LONG_PACKAGE_MIN_DAYS,
            min(int(row[1] if row else DEFAULT_LONG_PACKAGE_MIN_DAYS), 3650),
        ),
        "notice_days": max(
            2,
            min(int(row[2] if row else DEFAULT_PREEXPIRY_NOTICE_DAYS), 3),
        ),
    }


async def set_expiry_notification_settings(
    *, enabled: bool, min_package_days: int, notice_days: int
) -> None:
    """Сохраняет порог долгого пакета и срок предварительного оповещения."""
    await ensure_donation_schema()
    min_days = max(DEFAULT_LONG_PACKAGE_MIN_DAYS, min(int(min_package_days), 3650))
    lead_days = max(2, min(int(notice_days), 3))
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO donation_settings (
                id, preexpiry_enabled, long_package_min_days, preexpiry_notice_days
            ) VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                preexpiry_enabled = excluded.preexpiry_enabled,
                long_package_min_days = excluded.long_package_min_days,
                preexpiry_notice_days = excluded.preexpiry_notice_days
            """,
            (1 if enabled else 0, min_days, lead_days),
        )


async def get_donation_view_timers() -> dict[str, int]:
    """Возвращает время показа сообщений /viewd и /viewmd в секундах.

    Нулевое значение отключает автоматическое удаление.
    """
    await ensure_donation_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT viewd_delete_seconds, viewmd_delete_seconds
            FROM donation_settings
            WHERE id = 1
            """
        )
        row = await cur.fetchone()

    return {
        "viewd": max(0, min(int(row[0] if row else 30), 86400)),
        "viewmd": max(0, min(int(row[1] if row else 30), 86400)),
    }


async def set_donation_view_timers(*, viewd: int, viewmd: int) -> None:
    """Сохраняет время показа сообщений просмотра донатов."""
    await ensure_donation_schema()
    viewd_seconds = max(0, min(int(viewd), 86400))
    viewmd_seconds = max(0, min(int(viewmd), 86400))
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO donation_settings (
                id, voice_daily_limit, emoji_daily_limit, video_note_daily_limit,
                viewd_delete_seconds, viewmd_delete_seconds
            ) VALUES (1, 20, 50, 10, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                viewd_delete_seconds = excluded.viewd_delete_seconds,
                viewmd_delete_seconds = excluded.viewmd_delete_seconds
            """,
            (viewd_seconds, viewmd_seconds),
        )


async def check_and_update_usage_limit(
    chat_id: int,
    user_id: int,
    category: str,
    add_count: int = 1,
) -> tuple[bool, int, int]:
    """
    Атомарно проверяет и обновляет суточный счётчик.

    Возвращает ``(разрешено, лимит, использовано_после_операции)``.

    Если сообщение превышает оставшийся лимит, оно отклоняется, а счётчик
    фиксируется на значении лимита. После этого новые смайлы или кружки
    блокируются до смены календарного дня. ``BEGIN IMMEDIATE`` не позволяет
    нескольким одновременно пришедшим сообщениям списать один остаток дважды.
    """
    if category not in ("voice", "emoji", "video_note"):
        raise ValueError(f"Для категории {category} не настроен счётчик")

    await ensure_donation_schema()
    limits = await get_usage_limits()
    fallback_limit = limits[category]
    now = int(time.time())
    async with db() as cur:
        await cur.execute(
            """
            SELECT daily_limit
            FROM donation_grants
            WHERE chat_id = ? AND user_id = ? AND category = ? AND valid_until > ?
            """,
            (int(chat_id), int(user_id), category, now),
        )
        grant_row = await cur.fetchone()

    if grant_row is None:
        return False, fallback_limit, 0

    stored_limit = int(grant_row[0] or 0)
    limit = stored_limit if stored_limit > 0 else fallback_limit
    amount = max(1, int(add_count))
    today = time.strftime("%Y-%m-%d", time.localtime())
    chat_id = int(chat_id)
    user_id = int(user_id)

    async with db() as cur:
        # Блокировка берётся до чтения. Это важно для пачки быстрых обновлений,
        # которые aiogram может обрабатывать параллельно.
        await cur.execute("BEGIN IMMEDIATE")
        await cur.execute(
            """
            SELECT used_today, used_date
            FROM donation_usage
            WHERE chat_id = ? AND user_id = ? AND category = ?
            """,
            (chat_id, user_id, category),
        )
        row = await cur.fetchone()

        if row is None:
            used_today = 0
            await cur.execute(
                """
                INSERT INTO donation_usage (
                    chat_id, user_id, category, used_today, used_date
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (chat_id, user_id, category, today),
            )
        else:
            used_today = max(0, int(row[0] or 0))
            used_date = str(row[1] or "")
            if used_date != today:
                used_today = 0
                await cur.execute(
                    """
                    UPDATE donation_usage
                    SET used_today = 0, used_date = ?
                    WHERE chat_id = ? AND user_id = ? AND category = ?
                    """,
                    (today, chat_id, user_id, category),
                )

        if used_today >= limit:
            # В том числе нормализуем счётчик, если лимит уменьшили в панели.
            allowed = False
            used_after = limit
        elif used_today + amount <= limit:
            allowed = True
            used_after = used_today + amount
        else:
            # Превышающая остаток пачка закрывает лимит на весь текущий день.
            allowed = False
            used_after = limit

        await cur.execute(
            """
            UPDATE donation_usage
            SET used_today = ?, used_date = ?
            WHERE chat_id = ? AND user_id = ? AND category = ?
            """,
            (used_after, today, chat_id, user_id, category),
        )

    return allowed, limit, used_after


async def get_active_donation_statuses(chat_id: int, user_id: int) -> list[dict[str, Any]]:
    """Возвращает активные срочные донаты пользователя с лимитами и расходом."""
    await ensure_donation_schema()
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    fallback_limits = await get_usage_limits()

    async with db() as cur:
        await cur.execute(
            """
            SELECT category, valid_until, package_days, daily_limit
            FROM donation_grants
            WHERE chat_id = ? AND user_id = ? AND valid_until > ?
            ORDER BY valid_until ASC
            """,
            (int(chat_id), int(user_id), now),
        )
        grants = await cur.fetchall()

        await cur.execute(
            """
            SELECT category, used_today, used_date
            FROM donation_usage
            WHERE chat_id = ? AND user_id = ?
            """,
            (int(chat_id), int(user_id)),
        )
        usage_rows = await cur.fetchall()

    usage = {
        str(row[0]): int(row[1] or 0) if str(row[2] or "") == today else 0
        for row in usage_rows
    }
    result: list[dict[str, Any]] = []
    for row in grants:
        category = str(row[0])
        stored_limit = int(row[3] or 0)
        daily_limit = stored_limit if stored_limit > 0 else fallback_limits.get(category, 0)
        result.append(
            {
                "category": category,
                "title": CATEGORY_TITLES.get(category, category),
                "valid_until": int(row[1]),
                "package_days": int(row[2] or 0),
                "daily_limit": int(daily_limit or 0),
                "used_today": int(usage.get(category, 0)),
            }
        )
    return result


async def get_donation_daily_limit(chat_id: int, user_id: int, category: str) -> int:
    """Возвращает персональный лимит активного пакета или прежний лимит по умолчанию."""
    statuses = await get_active_donation_statuses(chat_id, user_id)
    for item in statuses:
        if item["category"] == category:
            return int(item["daily_limit"] or 0)
    return 0


async def get_notification_template(category: str, event_type: str) -> dict[str, Any] | None:
    await ensure_donation_schema()

    async with db() as cur:
        await cur.execute(
            """
            SELECT title, message, image_path,
                   button1_text, button1_url, button2_text, button2_url
            FROM donation_notification_templates
            WHERE category = ? AND event_type = ?
            """,
            (category, event_type),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "title": row[0],
        "message": row[1],
        "image_path": row[2],
        "button1_text": row[3],
        "button1_url": row[4],
        "button2_text": row[5],
        "button2_url": row[6],
    }


def _build_keyboard(template: dict[str, Any]) -> InlineKeyboardMarkup | None:
    buttons: list[InlineKeyboardButton] = []

    for number in (1, 2):
        text = (template.get(f"button{number}_text") or "").strip()
        url = (template.get(f"button{number}_url") or "").strip()
        if text and url:
            buttons.append(InlineKeyboardButton(text=text, url=url))

    if not buttons:
        return None

    # Каждая кнопка занимает отдельную строку. Так длинные подписи не
    # сталкиваются друг с другом и не выходят за границы сообщения на телефоне.
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


async def _get_user_name(bot, chat_id: int, user_id: int) -> str:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        user = member.user
        return user.full_name or user.username or str(user_id)
    except Exception:
        return str(user_id)


def _render_message(
    template_message: str,
    *,
    user_id: int,
    full_name: str,
    category: str,
    valid_until: int,
    event_type: str,
    limit: int | None = None,
    used_today: int | None = None,
    now: int | None = None,
) -> str:
    safe_name = hd.quote(full_name)
    user_link = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
    category_title = CATEGORY_TITLES.get(category, "Реакции" if category == "reaction" else category)
    valid_until_text = time.strftime("%d.%m.%Y %H:%M", time.localtime(valid_until))
    current_time = int(time.time()) if now is None else int(now)
    days_left = (
        max(1, math.ceil(max(0, valid_until - current_time) / DAY_SECONDS))
        if event_type == "preexpiry"
        else 0
    )

    result = template_message or DEFAULT_TEMPLATES.get((category, event_type), "{user}")
    replacements = {
        "{user}": user_link,
        "{user_id}": str(user_id),
        "{full_name}": safe_name,
        "{category}": hd.quote(category_title),
        "{valid_until}": valid_until_text,
        "{days_left}": str(days_left),
        "{limit}": str(limit if limit is not None else ""),
        "{used_today}": str(used_today if used_today is not None else ""),
    }
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


async def _send_notification(
    bot,
    grant: Any,
    event_type: str,
    *,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    limit: int | None = None,
    used_today: int | None = None,
) -> Any:
    category = str(grant["category"])
    chat_id = int(grant["chat_id"])
    user_id = int(grant["user_id"])
    valid_until = int(grant["valid_until"])

    template = await get_notification_template(category, event_type)
    if template is None:
        raise RuntimeError(f"Нет шаблона уведомления {category}/{event_type}")

    full_name = await _get_user_name(bot, chat_id, user_id)
    text = _render_message(
        template.get("message") or "",
        user_id=user_id,
        full_name=full_name,
        category=category,
        valid_until=valid_until,
        event_type=event_type,
        limit=limit,
        used_today=used_today,
    )
    keyboard = _build_keyboard(template)

    image_path = (template.get("image_path") or "").strip()
    full_image_path = resolve_bot_image_path(os.path.join(BASE_DIR, "bot", "images", image_path)) if image_path else ""

    reply_kwargs: dict[str, Any] = {}
    if reply_to_message_id is not None:
        reply_kwargs["reply_parameters"] = ReplyParameters(
            message_id=int(reply_to_message_id),
            allow_sending_without_reply=True,
        )
    if message_thread_id is not None:
        reply_kwargs["message_thread_id"] = int(message_thread_id)

    if full_image_path and os.path.isfile(full_image_path):
        media_kwargs = dict(
            wait=True,
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            **reply_kwargs,
        )
        # GIF нельзя надёжно отправлять через sendPhoto: Telegram ждёт animation.
        if os.path.splitext(full_image_path)[1].lower() == ".gif":
            sent = await bot_send_animation_to_chat(
                bot, chat_id, FSInputFile(full_image_path), **media_kwargs
            )
        else:
            sent = await bot_send_photo_to_chat(
                bot, chat_id, FSInputFile(full_image_path), **media_kwargs
            )
    else:
        sent = await bot_send_message(
            bot,
            chat_id,
            text,
            wait=True,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
            **reply_kwargs,
        )

    if sent is None:
        raise RuntimeError(f"Не удалось отправить донат-уведомление в chat_id={chat_id}")
    return sent


async def send_test_donation_notification(
    bot,
    *,
    chat_id: int,
    user_id: int,
    category: str,
    event_type: str = "expired",
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> None:
    """Отправляет тестовый шаблон без изменения срока и флагов реального доната."""
    is_reaction_denied = category == "reaction" and event_type == "denied"
    is_donation_event = (
        category in CATEGORY_TITLES
        and event_type in CATEGORY_EVENT_TYPES.get(category, ())
    )
    if not (is_reaction_denied or is_donation_event):
        raise ValueError("Недопустимая категория или тип тестового уведомления")

    await ensure_donation_schema()
    now = int(time.time())
    expiry_settings = await get_expiry_notification_settings()
    notice_days = int(expiry_settings["notice_days"])
    valid_until = (
        now + notice_days * DAY_SECONDS if event_type == "preexpiry" else now
    )

    grant = {
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "category": category,
        "valid_until": valid_until,
        "package_days": (
            int(expiry_settings["min_package_days"])
            if event_type == "preexpiry"
            else 1
        ),
    }
    limits = await get_usage_limits()
    await _send_notification(
        bot,
        grant,
        event_type,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
        limit=limits.get(category),
        used_today=limits.get(category),
    )


async def send_usage_limit_notification(
    bot,
    *,
    chat_id: int,
    user_id: int,
    category: str,
    limit: int,
    used_today: int,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> None:
    """Отправляет отдельный редактируемый шаблон исчерпания лимита."""
    if category not in ("voice", "emoji", "video_note"):
        raise ValueError("Уведомление о лимите поддерживается только для ГС, смайлов и кружков")

    grant = {
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "category": category,
        "valid_until": int(time.time()),
        "package_days": 0,
    }

    async def _sender():
        return await _send_notification(
            bot,
            grant,
            "limit_exhausted",
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            limit=limit,
            used_today=used_today,
        )

    await replace_warning(
        bot,
        chat_id=int(chat_id),
        user_id=int(user_id),
        sender=_sender,
    )


async def send_reaction_denied_notification(
    bot,
    *,
    chat_id: int,
    user_id: int,
    message_id: int,
) -> None:
    """Отправляет предупреждение после удаления реакции без уровня «Медиа 2»."""
    await ensure_donation_schema()
    now = int(time.time())
    grant = {
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "category": "reaction",
        "valid_until": now,
        "package_days": 0,
    }
    async def _sender():
        return await _send_notification(
            bot,
            grant,
            "denied",
            reply_to_message_id=int(message_id),
        )

    await replace_warning(
        bot,
        chat_id=int(chat_id),
        user_id=int(user_id),
        sender=_sender,
    )


async def _load_due_grants(
    event_type: str,
    now: int,
    *,
    min_package_days: int = DEFAULT_LONG_PACKAGE_MIN_DAYS,
    notice_days: int = DEFAULT_PREEXPIRY_NOTICE_DAYS,
) -> list[Any]:
    async with db() as cur:
        if event_type == "preexpiry":
            notice_seconds = max(2, min(int(notice_days), 3)) * DAY_SECONDS
            package_threshold = max(
                DEFAULT_LONG_PACKAGE_MIN_DAYS, int(min_package_days)
            )
            await cur.execute(
                """
                SELECT chat_id, user_id, category, valid_until, package_days, daily_limit
                FROM donation_grants
                WHERE category IN ('voice', 'emoji', 'tag', 'video_note')
                  AND package_days >= ?
                  AND preexpiry_sent = 0
                  AND expired_sent = 0
                  AND valid_until > ?
                  AND valid_until <= ?
                ORDER BY valid_until ASC
                """,
                (package_threshold, now, now + notice_seconds),
            )
        else:
            await cur.execute(
                """
                SELECT chat_id, user_id, category, valid_until, package_days, daily_limit
                FROM donation_grants
                WHERE category IN ('voice', 'emoji', 'tag', 'video_note')
                  AND expired_sent = 0
                  AND valid_until <= ?
                ORDER BY valid_until ASC
                """,
                (now,),
            )
        return list(await cur.fetchall())


async def _claim_grant(grant: Any, event_type: str) -> bool:
    flag = "preexpiry_sent" if event_type == "preexpiry" else "expired_sent"
    async with db() as cur:
        await cur.execute(
            f"""
            UPDATE donation_grants
            SET {flag} = 1
            WHERE chat_id = ? AND user_id = ? AND category = ?
              AND valid_until = ? AND {flag} = 0
            """,
            (
                grant["chat_id"],
                grant["user_id"],
                grant["category"],
                grant["valid_until"],
            ),
        )
        return cur.rowcount > 0


async def _release_grant(grant: Any, event_type: str) -> None:
    flag = "preexpiry_sent" if event_type == "preexpiry" else "expired_sent"
    async with db() as cur:
        await cur.execute(
            f"""
            UPDATE donation_grants
            SET {flag} = 0
            WHERE chat_id = ? AND user_id = ? AND category = ?
              AND valid_until = ?
            """,
            (
                grant["chat_id"],
                grant["user_id"],
                grant["category"],
                grant["valid_until"],
            ),
        )


async def process_due_donation_notifications(bot) -> None:
    await ensure_donation_schema()
    now = int(time.time())
    expiry_settings = await get_expiry_notification_settings()

    # Сначала истёкшие пакеты, затем предупреждения по ещё активным.
    for event_type in ("expired", "preexpiry"):
        if event_type == "preexpiry" and not expiry_settings["enabled"]:
            continue

        grants = await _load_due_grants(
            event_type,
            now,
            min_package_days=int(expiry_settings["min_package_days"]),
            notice_days=int(expiry_settings["notice_days"]),
        )
        for grant in grants:
            if not await _claim_grant(grant, event_type):
                continue

            try:
                await _send_notification(bot, grant, event_type)
            except asyncio.CancelledError:
                await _release_grant(grant, event_type)
                raise
            except Exception as exc:
                await _release_grant(grant, event_type)
                print(
                    "Ошибка донат-уведомления "
                    f"{grant['category']}/{event_type} для user_id={grant['user_id']}: {exc}"
                )


async def donation_notifications_loop(bot) -> None:
    await ensure_donation_schema()

    while True:
        try:
            await process_due_donation_notifications(bot)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            print(f"Ошибка цикла донат-уведомлений: {exc}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
