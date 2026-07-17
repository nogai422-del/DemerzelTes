# Основной message-пайплайн: welcome, фоновые циклы и начисление активности.

import asyncio
import html
import time

from aiogram import Bot, Router
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database import db
from bot.handlers.chat_lock import handle_chat_lock
from bot.handlers.moderation import moderation_handle_message
from bot.message_queue import bot_send_photo
from bot.utils import (
    get_full_name,
    safe_delete,
    send_info,
    send_myth,
    send_rating_info,
    send_wisdom,
)

router = Router()

ALLOWED_USER_CONTENT_TYPES = {
    "text",
    "photo",
    "video",
    "animation",
    "document",
    "paid_media",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "story",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
    "game",
    "invoice",
    "successful_payment",
    "refunded_payment",
    "connected_website",
    "passport_data",
    "web_app_data",
    "users_shared",
    "chat_shared",
    "checklist",
}


async def _get_wisdom_timer_seconds() -> int | None:
    async with db() as cur:
        await cur.execute("SELECT wisdom_timer_minutes FROM other LIMIT 1")
        row = await cur.fetchone()

    if row and row[0] is not None:
        return int(row[0]) * 60
    return None


async def _fetch_timed_messages():
    async with db() as cur:
        await cur.execute("SELECT chat_id, message_id, send_time FROM timed_messages")
        return await cur.fetchall()


async def _delete_timed_message(chat_id: int, message_id: int) -> None:
    async with db() as cur:
        await cur.execute(
            "DELETE FROM timed_messages WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )


async def _fetch_hello_messages():
    async with db() as cur:
        await cur.execute("SELECT chat_id, message_id, send_time FROM hello_messages")
        return await cur.fetchall()


async def _delete_hello_message(chat_id: int, message_id: int) -> None:
    async with db() as cur:
        await cur.execute(
            "DELETE FROM hello_messages WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )


async def _fetch_last_message_rows():
    async with db() as cur:
        await cur.execute("SELECT chat_id, time, wisdom_time FROM last_message")
        return await cur.fetchall()


async def _update_wisdom_time(chat_id: int, current_time: float) -> None:
    async with db() as cur:
        await cur.execute(
            "UPDATE last_message SET wisdom_time=? WHERE chat_id=?",
            (current_time, chat_id),
        )


async def _delete_last_message_chat(chat_id: int) -> None:
    async with db() as cur:
        await cur.execute("DELETE FROM last_message WHERE chat_id=?", (chat_id,))


async def _load_chat_user_stats(chat_id: int, user_id: int):
    async with db() as cur:
        await cur.execute(
            "SELECT score, level, rank_name_cache FROM chat_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        return await cur.fetchone()


async def _apply_score_delta(chat_id: int, user_id: int, score_delta: int) -> None:
    async with db() as cur:
        await cur.execute(
            "SELECT 1 FROM chat_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        exists = await cur.fetchone()

        if exists:
            await cur.execute(
                """
                UPDATE chat_users
                SET score = score + ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (score_delta, chat_id, user_id),
            )
        else:
            await cur.execute(
                """
                INSERT INTO chat_users (chat_id, user_id, score, level, rank_name_cache)
                VALUES (?, ?, ?, 0, '')
                """,
                (chat_id, user_id, score_delta),
            )


async def _load_level_for_score(current_score: int):
    async with db() as cur:
        await cur.execute(
            """
            SELECT level, rank_name, message, image_path, button_text, button_url
            FROM levels
            WHERE points <= ?
            ORDER BY points DESC, level DESC
            LIMIT 1
            """,
            (current_score,),
        )
        return await cur.fetchone()


async def _update_user_level(chat_id: int, user_id: int, target_level: int, rank_name: str) -> None:
    async with db() as cur:
        await cur.execute(
            """
            UPDATE chat_users
            SET level = ?, rank_name_cache = ?
            WHERE chat_id = ? AND user_id = ?
            """,
            (target_level, rank_name, chat_id, user_id),
        )


async def _load_last_message_row(chat_id: int):
    async with db() as cur:
        await cur.execute("SELECT * FROM last_message WHERE chat_id = ?", (chat_id,))
        return await cur.fetchone()


async def _save_last_message_activity(chat_id: int, current_time: float, row_last) -> None:
    async with db() as cur:
        if row_last is None:
            await cur.execute(
                "INSERT INTO last_message (chat_id, time, wisdom_time) VALUES (?, ?, ?)",
                (chat_id, current_time, current_time),
            )
        else:
            await cur.execute(
                "UPDATE last_message SET time = ? WHERE chat_id = ?",
                (current_time, chat_id),
            )


async def _increment_user_messages(chat_id: int, user_id: int) -> int:
    async with db() as cur:
        await cur.execute(
            """
            UPDATE chat_users
            SET messages = COALESCE(messages, 0) + 1
            WHERE chat_id=? AND user_id=?
            """,
            (chat_id, user_id),
        )
        await cur.execute(
            "SELECT messages FROM chat_users WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        row = await cur.fetchone()

    return row[0] if row else 1


async def _load_reminder_thresholds():
    async with db() as cur:
        await cur.execute(
            """
            SELECT rules_reminder_message_number,
                   rating_reminder_message_number,
                   myth_reminder_message_number
            FROM other
            LIMIT 1
            """
        )
        return await cur.fetchone()


@router.message(lambda message: message.left_chat_member)
async def on_user_leave(message: Message):
    await safe_delete(message)


async def wisdom_loop(bot: Bot):
    while True:
        try:
            current_time = time.time()
            wisdom_timer_seconds = await _get_wisdom_timer_seconds()

            for chat_id, message_id, send_time in await _fetch_timed_messages():
                if current_time - float(send_time) > 300:
                    try:
                        await bot.delete_message(chat_id, message_id)
                    except Exception as e:
                        print(f"Ошибка при удалении timed_messages в чате {chat_id}: {e}")
                    finally:
                        await _delete_timed_message(chat_id, message_id)

            for chat_id, message_id, send_time in await _fetch_hello_messages():
                if current_time - float(send_time) > 300:
                    try:
                        await bot.delete_message(chat_id, message_id)
                    except Exception as e:
                        print(f"Ошибка при удалении hello_messages в чате {chat_id}: {e}")
                    finally:
                        await _delete_hello_message(chat_id, message_id)

            if wisdom_timer_seconds is not None:
                for chat_id, last_message_time, last_wisdom_time in await _fetch_last_message_rows():
                    try:
                        last_message_time = float(last_message_time)
                        last_wisdom_time = float(last_wisdom_time)

                        if (
                            current_time - last_message_time > wisdom_timer_seconds
                            and last_message_time > last_wisdom_time
                        ):
                            try:
                                sent = await send_wisdom(bot, chat_id)
                                if sent:
                                    await _update_wisdom_time(chat_id, current_time)
                            except Exception as e:
                                error_message = str(e)

                                if "group chat was upgraded to a supergroup chat" in error_message:
                                    print(f"Чат {chat_id} стал супергруппой - удаляем.")
                                    await _delete_last_message_chat(chat_id)
                                elif (
                                    "chat not found" in error_message
                                    or "group chat was deleted" in error_message
                                    or "Forbidden" in error_message
                                ):
                                    print(f"Чат {chat_id} недоступен - удаляем.")
                                    await _delete_last_message_chat(chat_id)
                                elif "not enough rights to send text messages to the chat" in error_message:
                                    print(f"Нет прав писать в чат {chat_id} - удаляем.")
                                    await _delete_last_message_chat(chat_id)
                                else:
                                    print(f"Ошибка отправки мудрости в чате {chat_id}: {e}")

                    except ValueError as ve:
                        print(f"Ошибка преобразования типов данных для чата {chat_id}: {ve}")

        except Exception as e:
            print(f"Ошибка в wisdom_loop: {e}")

        await asyncio.sleep(1)


@router.message()
async def handle_messages(message: Message):
    if not message.from_user:
        return

    if message.content_type not in ALLOWED_USER_CONTENT_TYPES:
        return

    if await handle_chat_lock(message):
        return

    if await moderation_handle_message(message):
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    message_text = message.text or message.caption or ""

    full_name = await get_full_name(message.from_user)
    safe_full_name = html.escape(full_name)

    word_count = len(message_text.split())
    score_delta = 10 + word_count * 2

    await _apply_score_delta(chat_id, user_id, score_delta)

    current_score, current_level, rank_name_cache = await _load_chat_user_stats(chat_id, user_id)
    rank_name_cache = rank_name_cache or ""

    row_level = await _load_level_for_score(current_score)
    if row_level:
        target_level, rank_name, level_message, image_path, button_text, button_url = row_level
        rank_name = rank_name or ""
    else:
        target_level = 0
        rank_name = ""
        level_message = None
        image_path = None
        button_text = None
        button_url = None

    send_congrats = False
    if target_level != current_level:
        send_congrats = True
    elif rank_name and rank_name_cache and rank_name != rank_name_cache:
        send_congrats = True

    await _update_user_level(chat_id, user_id, target_level, rank_name)

    has_text = bool(message.text or message.caption)
    if has_text:
        row_last = await _load_last_message_row(chat_id)
        await _save_last_message_activity(chat_id, time.time(), row_last)

    new_messages = await _increment_user_messages(chat_id, user_id)
    row_reminders = await _load_reminder_thresholds()

    if send_congrats and level_message and image_path:
        try:
            photo = FSInputFile(f"bot/images/{image_path}")

            markup = None
            if button_text and button_url:
                markup = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
                )

            await bot_send_photo(
                message,
                photo,
                caption=level_message.format(user_id=user_id, name=safe_full_name),
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            print(f"Ошибка при отправке сообщения о новом уровне: {e}")

    if row_reminders:
        rules_reminder_message_number, rating_reminder_message_number, myth_reminder_message_number = row_reminders

        if new_messages == rules_reminder_message_number:
            try:
                await send_info(message)
            except Exception as e:
                print("Ошибка send_info:", e)

        if new_messages == rating_reminder_message_number:
            try:
                await send_rating_info(message)
            except Exception as e:
                print("Ошибка send_rating_info:", e)

        if new_messages == myth_reminder_message_number:
            try:
                await send_myth(message)
            except Exception as e:
                print("Ошибка send_myth:", e)
