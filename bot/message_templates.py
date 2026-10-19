"""Гибкие шаблоны сообщений и универсальная отправка Telegram-медиа."""

import os
import time
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.database import db
from bot.notification_delivery import send_notification_card

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
MEDIA_DIR = os.path.join(BASE_DIR, "bot", "images", "message_media")
os.makedirs(MEDIA_DIR, exist_ok=True)

DEFAULTS = {
    "viewd": {
        "title": "/viewd — мои донаты",
        "enabled": 1,
        "message": (
            "<b>Донат-функции пользователя</b>\n"
            "👤 {identity}\n\n{donation_lines}"
        ),
        "empty_text": "Активных донат-функций сейчас нет.",
        "usage_text": "",
        "media_path": "",
        "delete_seconds": 30,
        "show_delete_notice": 1,
        "disable_preview": 1,
        "silent": 0,
        "protect_content": 0,
        "button1_text": "",
        "button1_url": "",
        "button2_text": "",
        "button2_url": "",
    },
    "viewmd": {
        "title": "/viewmd — донаты участника",
        "enabled": 1,
        "message": (
            "<b>Донат-функции пользователя</b>\n"
            "👤 {identity}\n\n{donation_lines}"
        ),
        "empty_text": "Активных донат-функций сейчас нет.",
        "usage_text": (
            "<b>Как использовать /viewmd</b>\n"
            "Ответьте командой на сообщение пользователя или укажите его "
            "Telegram ID: <code>/viewmd 123456789</code>."
        ),
        "media_path": "",
        "delete_seconds": 30,
        "show_delete_notice": 1,
        "disable_preview": 1,
        "silent": 0,
        "protect_content": 0,
        "button1_text": "",
        "button1_url": "",
        "button2_text": "",
        "button2_url": "",
    },
}


async def ensure_message_template_schema() -> None:
    async with db() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS message_templates (
                template_key       TEXT PRIMARY KEY,
                title              TEXT NOT NULL DEFAULT '',
                enabled            INTEGER NOT NULL DEFAULT 1,
                message            TEXT NOT NULL DEFAULT '',
                empty_text         TEXT NOT NULL DEFAULT '',
                usage_text         TEXT NOT NULL DEFAULT '',
                media_path         TEXT NOT NULL DEFAULT '',
                delete_seconds     INTEGER NOT NULL DEFAULT 30,
                show_delete_notice INTEGER NOT NULL DEFAULT 1,
                disable_preview    INTEGER NOT NULL DEFAULT 1,
                silent             INTEGER NOT NULL DEFAULT 0,
                protect_content    INTEGER NOT NULL DEFAULT 0,
                button1_text       TEXT NOT NULL DEFAULT '',
                button1_url        TEXT NOT NULL DEFAULT '',
                button2_text       TEXT NOT NULL DEFAULT '',
                button2_url        TEXT NOT NULL DEFAULT '',
                updated_at         INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        for key, item in DEFAULTS.items():
            await cur.execute(
                """
                INSERT OR IGNORE INTO message_templates (
                    template_key, title, enabled, message, empty_text, usage_text,
                    media_path, delete_seconds, show_delete_notice,
                    disable_preview, silent, protect_content,
                    button1_text, button1_url, button2_text, button2_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, item["title"], item["enabled"], item["message"],
                    item["empty_text"], item["usage_text"], item["media_path"],
                    item["delete_seconds"], item["show_delete_notice"],
                    item["disable_preview"], item["silent"], item["protect_content"],
                    item["button1_text"], item["button1_url"],
                    item["button2_text"], item["button2_url"], int(time.time()),
                ),
            )


async def get_message_template(key: str) -> dict[str, Any]:
    await ensure_message_template_schema()
    async with db() as cur:
        await cur.execute("SELECT * FROM message_templates WHERE template_key = ?", (key,))
        row = await cur.fetchone()
    if row is None:
        return dict(DEFAULTS[key])
    return dict(row)


def build_inline_keyboard(template: dict[str, Any]) -> InlineKeyboardMarkup | None:
    rows = []
    for index in (1, 2):
        text = str(template.get(f"button{index}_text") or "").strip()
        url = str(template.get(f"button{index}_url") or "").strip()
        if text and url:
            # Каждая кнопка находится на отдельной строке.
            rows.append([InlineKeyboardButton(text=text, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def render_template(text: str, values: dict[str, Any]) -> str:
    result = str(text or "")
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    return result


async def send_configured_message(
    bot,
    chat_id: int,
    template: dict[str, Any],
    text: str,
    *,
    message_thread_id: int | None = None,
) -> Any:
    """Надёжно отправляет настроенную карточку; медиа не отменяет текст."""
    keyboard = build_inline_keyboard(template)
    return await send_notification_card(
        bot,
        chat_id=int(chat_id),
        text=text,
        image_path=str(template.get("media_path") or "").strip(),
        reply_markup=keyboard,
        message_thread_id=message_thread_id,
        disable_notification=bool(template.get("silent")),
        protect_content=bool(template.get("protect_content")),
        context=f"message_template:{template.get('template_key', 'unknown')}",
    )
