# Команды бота: анкеты, рейтинг, статистика и административные действия.

from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ChatPermissions

from bot.message_queue import bot_answer, bot_reply, bot_send_photo

from bot.database import db
from bot.utils import (
    save_timed_message, get_old_messages, update_old_message, get_full_name,
    is_in_chat_member, safe_delete, is_command_admin, resolve_bot_image_path, normalize_telegram_button_url,
)
from bot.handlers.moderation import is_user_muted, send_restriction_warning
from bot.warning_state import clear_warning, mark_form_saved, mark_form_started, get_form_stage, FORM_STAGE_SAVED
from env_config import require_int_env

import asyncio
import html
import os
import time

_bv_locks: dict[tuple[int, int], asyncio.Lock] = {}
_view_locks: dict[int, asyncio.Lock] = {}


def _form_chunks(text: str, limit: int = 3000) -> list[str]:
    """Оставляет место заголовку и учитывает длину эмодзи в Telegram."""
    chunks, current, units = [], [], 0
    for character in text:
        size = 2 if ord(character) > 0xFFFF else 1
        if units + size > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(character)
        units += size
    if current:
        chunks.append("".join(current))
    return chunks

router = Router()

COSMOS_ID = require_int_env("COSMOS_ID")


# Сохраняет текст анкеты пользователя в chat_users.
async def _save_filled_form(cur, chat_id: int, user_id: int, filled_form_text: str):
    """Атомарно сохраняет анкету и этап, не создавая дубликаты пользователя."""
    await cur.execute(
        """INSERT INTO form_history(chat_id,user_id,filled_form_text)
           SELECT chat_id,user_id,filled_form_text FROM chat_users
           WHERE chat_id=? AND user_id=? AND TRIM(COALESCE(filled_form_text,'')) <> ''
             AND filled_form_text <> ?""",
        (int(chat_id),int(user_id),filled_form_text),
    )
    await cur.execute(
        """INSERT INTO chat_users (
               chat_id, user_id, filled_form_text, form_stage, form_saved_at
           ) VALUES (?, ?, ?, 'saved', CAST(strftime('%s','now') AS INTEGER))
           ON CONFLICT(chat_id, user_id) DO UPDATE SET
               filled_form_text=excluded.filled_form_text,
               form_stage='saved',
               form_saved_at=excluded.form_saved_at""",
        (int(chat_id), int(user_id), filled_form_text),
    )


# Команда /save: сохраняет анкету пользователя из reply-сообщения.
@router.message(Command("save"))
async def handle_save_command(message: Message, bot):
    try:
        await safe_delete(message)

        if await is_user_muted(message.chat.id, message.from_user.id):
            return

        is_admin = await is_command_admin(
            bot, message.chat.id, message.from_user.id, owner_id=COSMOS_ID
        )
        replied = message.reply_to_message
        if not replied or not replied.from_user or replied.sender_chat:
            await bot_answer(message, "Чтобы сохранить анкету, вызовите /save в ответ на сообщение с анкетой.")
            return
        is_own_update = (
            replied.from_user.id == message.from_user.id
            and await get_form_stage(message.chat.id, message.from_user.id) == FORM_STAGE_SAVED
        )
        if not is_admin and not is_own_update:
            await bot_answer(message, "Первую анкету подтверждает администратор. После проверки вы сможете обновлять свою анкету самостоятельно.")
            return

        if is_admin or is_own_update:
            if message.reply_to_message:
                chat_id = message.chat.id
                user = message.reply_to_message.from_user
                user_id = user.id

                filled_form_text = (
                    message.reply_to_message.text
                    or message.reply_to_message.caption
                    or ""
                ).strip()

                if not filled_form_text:
                    await bot_answer(
                        message,
                        "Не удалось сохранить анкету: сообщение пустое.",
                    )
                    return

                full_name = await get_full_name(user)

                async with db() as cur:
                    await _save_filled_form(cur, chat_id, user_id, filled_form_text)
                    await cur.execute(
                        "DELETE FROM bv_messages WHERE chat_id=? AND target_user_id=?",
                        (chat_id, user_id),
                    )

                await mark_form_saved(chat_id, user_id)
                # После /save промежуточное предупреждение больше не нужно.
                await clear_warning(bot, chat_id, user_id)

                # Снимаем Telegram-ограничения на медиа у пользователя.
                # Это отдельный уровень прав Telegram и не заменяет внутреннюю модерацию бота.
                restrictions_removed = False
                try:
                    if not is_admin:
                        # Самостоятельное обновление текста не меняет права Telegram.
                        restrictions_removed = True
                    else:
                        permissions = ChatPermissions(
                            can_send_messages=True,
                            can_send_photos=True,
                            can_send_videos=True,
                            can_send_video_notes=True,
                            can_send_audios=True,
                            can_send_voice_notes=True,
                            can_send_documents=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True,
                        )
                        await bot.restrict_chat_member(
                            chat_id,
                            user_id,
                            permissions=permissions,
                            use_independent_chat_permissions=True,
                        )
                        restrictions_removed = True
                except Exception as e:
                    print(f"Ошибка снятия Telegram-ограничений в /save: {e}")

                if restrictions_removed:
                    await bot_answer(
                        message,
                        f"Анкета пользователя {full_name} успешно сохранена.",
                    )
                else:
                    await bot_answer(
                        message,
                        f"Анкета пользователя {full_name} сохранена, но снять Telegram-ограничения не удалось.",
                    )

    except Exception as e:
        print(f"Ошибка в /save: {e}")


# Проверяет право пользователя на команду /view.
async def _has_view_permission(cur, chat_id: int, user_id: int) -> bool:
    """
    Есть ли у пользователя право смотреть анкеты (/view).
    Хранится в chat_users.can_view_forms (0/1).
    """
    await cur.execute(
        """
        SELECT can_view_forms
        FROM chat_users
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat_id, user_id),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


# Команда /view: показывает сохраненную анкету пользователя (если есть право can_view_forms).
@router.message(Command("view"))
async def handle_view_command(message: Message, bot):
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if await is_user_muted(chat_id, message.from_user.id):
            return

        replied_message = message.reply_to_message

        requester_id = message.from_user.id

        if replied_message and (not replied_message.from_user or replied_message.sender_chat):
            await bot_answer(message, "Анкета привязана к участнику, а не к сообщению канала или анонимного администратора.")
            return
        target_user = replied_message.from_user if replied_message else message.from_user
        target_id = target_user.id

        async with db() as cur:
            allowed = target_id == requester_id or await _has_view_permission(cur, chat_id, requester_id)

        if not allowed:
            await send_restriction_warning(message, "view")
            return

        full_name = await get_full_name(target_user)

        async with db() as cur:
            await cur.execute(
                "SELECT filled_form_text FROM chat_users WHERE chat_id=? AND user_id=?",
                (chat_id, target_id),
            )
            row = await cur.fetchone()

        if row and row[0]:
            lock = _view_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                old_message_ids = await get_old_messages(chat_id)
                new_ids = []
                try:
                    for part in _form_chunks(str(row[0])):
                        text = (
                            f"Я нашла анкету пользователя {html.escape(full_name)}\n\n"
                            f"<blockquote expandable>{html.escape(part)}</blockquote>"
                        )
                        sent = await bot_answer(message, text, wait=True, parse_mode="HTML", disable_web_page_preview=True)
                        if not sent:
                            raise RuntimeError("Не удалось отправить анкету")
                        new_ids.append(sent.message_id)
                    async with db() as cur:
                        await cur.execute("DELETE FROM old_message WHERE chat_id=?", (chat_id,))
                        await cur.executemany(
                            "INSERT INTO old_message(chat_id,message_id) VALUES (?,?)",
                            [(chat_id, message_id) for message_id in new_ids],
                        )
                except Exception:
                    for message_id in new_ids:
                        try:
                            await bot.delete_message(chat_id, message_id)
                        except Exception:
                            pass
                    raise
                for old_id in old_message_ids:
                    try:
                        await bot.delete_message(chat_id, old_id)
                    except Exception:
                        pass

        else:
            sent = await bot_answer(message, "Я не нашла анкету.", wait=True)
            await save_timed_message(chat_id, sent.message_id)

    except Exception as e:
        print("Ошибка /view:", e)


# Команда /bv: отправляет шаблон анкеты в ответ на сообщение пользователя.
@router.message(Command("bv"))
async def handle_bv_command(message: Message):
    """Показывает анкету любому участнику и оставляет один активный ответ на человека."""
    try:
        await safe_delete(message)
        chat_id = message.chat.id
        if await is_user_muted(chat_id, message.from_user.id):
            return

        replied = message.reply_to_message
        if not replied or not replied.from_user:
            sent = await bot_answer(
                message,
                "Для отправки формы вызовите /bv в ответ на сообщение пользователя.",
                wait=True,
            )
            if sent:
                await save_timed_message(chat_id, sent.message_id)
            return

        target_id = replied.from_user.id
        lock = _bv_locks.setdefault((chat_id, target_id), asyncio.Lock())
        async with lock:
            async with db() as cur:
                await cur.execute("SELECT content FROM form_text ORDER BY rowid LIMIT 1")
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT message_id FROM bv_messages WHERE chat_id=? AND target_user_id=?",
                    (chat_id, target_id),
                )
                previous = await cur.fetchone()

            if previous:
                try:
                    await message.bot.delete_message(chat_id, int(previous[0]))
                except Exception:
                    pass

            form_text = str(row[0] if row else "").strip()
            if not form_text:
                form_text = "Шаблон анкеты пока не настроен администратором."

            sent = await bot_reply(replied, form_text, parse_mode="HTML", wait=True)
            if sent:
                async with db() as cur:
                    await cur.execute(
                        """INSERT INTO bv_messages(chat_id,target_user_id,message_id,updated_at)
                           VALUES(?,?,?,?)
                           ON CONFLICT(chat_id,target_user_id) DO UPDATE SET
                           message_id=excluded.message_id, updated_at=excluded.updated_at""",
                        (chat_id, target_id, sent.message_id, int(time.time())),
                    )
                await mark_form_started(chat_id, target_id)
                # При переходе к заполнению убираем старую общую карточку.
                await clear_warning(message.bot, chat_id, target_id)
    except Exception as e:
        print("Ошибка /bv:", e)


# Команда /top10: собирает рейтинг активных участников текущего чата.
@router.message(Command("top10"))
async def send_rating(message: Message):
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if await is_user_muted(chat_id, message.from_user.id):
            return

        # Берем больше кандидатов, чем нужно для топа, чтобы отфильтровать вышедших.
        CANDIDATES_LIMIT = 50  # Можно увеличить, если чат большой и много вышедших пользователей.

        async with db() as cur:
            await cur.execute(
                """
                SELECT user_id, score, level
                FROM chat_users
                WHERE chat_id = ?
                ORDER BY score DESC
                LIMIT ?
                """,
                (chat_id, CANDIDATES_LIMIT),
            )
            top_users = await cur.fetchall()

        active_users = []

        rank_cache: dict[int, str] = {}

        # Собираем top-10 только из пользователей, которые действительно состоят в чате.
        for user_id, score, level in top_users:
            if len(active_users) >= 10:
                break

            try:
                cm = await message.bot.get_chat_member(chat_id, user_id)
            except Exception:
                continue

            if not is_in_chat_member(cm):
                continue

            try:
                full_name = await get_full_name(cm.user)
            except Exception:
                full_name = f"User {user_id}"

            if level not in rank_cache:
                async with db() as cur:
                    await cur.execute(
                        "SELECT rank_name FROM levels WHERE level = ?",
                        (level,),
                    )
                    row = await cur.fetchone()
                rank_cache[level] = row[0] if row else ""

            rank_name = rank_cache[level]

            active_users.append((full_name, score, rank_name))

        # Формируем финальный текст рейтинга для отправки.
        if active_users:
            rating_message = "Рейтинг топ чатлан Пространства:\n\n"
            for idx, (full_name, score, rank_name) in enumerate(active_users, start=1):
                if rank_name:
                    rating_message += f"{idx}. {full_name} — {rank_name} ({score} очков)\n"
                else:
                    rating_message += f"{idx}. {full_name} — {score} очков\n"
        else:
            rating_message = "Пока нет данных для рейтинга в этом чате."

        sent = await bot_answer(
            message,
            rating_message,
            wait=True,
        )

        if sent:
            await save_timed_message(chat_id, sent.message_id)

    except Exception as e:
        print("Ошибка /top10:", e)


# Команда /stats: показывает очки, уровень и прогресс текущего пользователя.
@router.message(Command("stats"))
async def send_stats(message: Message):
    try:
        chat_id = message.chat.id
        user_id = message.from_user.id

        await safe_delete(message)

        if await is_user_muted(chat_id, user_id):
            return

        # Читаем текущие очки и уровень пользователя в этом чате.
        async with db() as cur:
            await cur.execute(
                "SELECT score, level FROM chat_users WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            stats_row = await cur.fetchone()

        if stats_row:
            current_score = int(stats_row[0] or 0)
            current_level = int(stats_row[1] or 0)
        else:
            current_score, current_level = 0, 0

        full_name = await get_full_name(message.from_user)
        safe_full_name = html.escape(full_name)
        user_name_link = f'<a href="tg://user?id={user_id}">{safe_full_name}</a>'
        sent = None

        # Отдельный текст для пользователей, у которых уровень еще не определен.
        if current_level == 0:
            sent = await bot_answer(
                message,
                (
                    f"{user_name_link}, вы только начали свой путь в Пространстве.\n"
                    f"Идите по нему, и не останавливайтесь, вас ожидают великие свершения.\n\n"
                    f"Количество очков: {current_score}"
                ),
                parse_mode="HTML",
                wait=True,
            )
        else:
            # Для ненулевого уровня подгружаем визуал и кнопку из таблицы levels.
            async with db() as cur:
                await cur.execute(
                    """
                    SELECT rank_name, image_path, button_text, button_url
                    FROM levels
                    WHERE level = ?
                    """,
                    (current_level,),
                )
                level_data = await cur.fetchone()

            if not level_data:
                sent = await bot_answer(
                    message,
                    "Произошла ошибка: данные уровня не найдены.",
                    wait=True,
                )
            else:
                rank_name, image_path, button_text, button_url = level_data
                safe_rank_name = html.escape((rank_name or "").strip())
                caption = (
                    f"{user_name_link}, ваше текущее воплощение: {safe_rank_name}\n\n"
                    f"Количество очков: {current_score}"
                )

                button_text = (button_text or "").strip()
                button_url = normalize_telegram_button_url(button_url)
                keyboard = (
                    InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
                    )
                    if button_text and button_url
                    else None
                )

                image_full_path = resolve_bot_image_path(f"bot/images/{image_path or ''}")

                # Сначала пробуем фото; при любой ошибке/отсутствии файла — текстовый fallback.
                if image_full_path and os.path.isfile(image_full_path):
                    try:
                        sent = await bot_send_photo(
                            message,
                            FSInputFile(image_full_path),
                            caption=caption,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                            wait=True,
                        )
                    except Exception as photo_error:
                        print(f"Ошибка /stats (photo): {photo_error}")
                        sent = None

                if not sent:
                    sent = await bot_answer(
                        message,
                        caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        wait=True,
                    )

        if sent:
            await save_timed_message(chat_id, sent.message_id)

    except Exception as e:
        print(f"Ошибка /stats: {e}")


# Показывает админскую статистику по таблицам и активностям.
@router.message(Command("st"))
async def admin_stats(message: Message, bot: Bot):
    try:
        await safe_delete(message)

        chat_id = message.chat.id
        admin_id = message.from_user.id

        if await is_user_muted(chat_id, admin_id):
            return

        if not await is_command_admin(
            bot, chat_id, admin_id, owner_id=COSMOS_ID
        ):
            return

        if message.reply_to_message is None:
            return

        # Статистика читается по пользователю из reply-сообщения.
        user = message.reply_to_message.from_user
        user_id = user.id
        full_name = await get_full_name(user)

        async with db() as cur:
            await cur.execute(
                "SELECT score, level FROM chat_users WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id)
            )
            stats_row = await cur.fetchone()
            if stats_row:
                current_score, current_level = stats_row
                await cur.execute(
                    "SELECT rank_name FROM levels WHERE level = ?",
                    (current_level,)
                )
                level_row = await cur.fetchone()
                rank_name = level_row[0] if level_row else 'Новичок'
                response_message = (
                    f"Пользователь: {full_name}\n"
                    f"Уровень: {rank_name}\n"
                    f"Количество очков: {current_score}"
                )
            else:
                response_message = f"Не удалось найти статистику пользователя {full_name}."

        sent = await bot_answer(message, response_message, wait=True)
        await save_timed_message(chat_id, sent.message_id)
    except Exception as e:
        print("Ошибка /st:", e)


# Команда /ban: банит пользователя из reply-сообщения (для админа/создателя).
@router.message(Command("ban"))
async def handle_ban_command(message: Message, bot: Bot):
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if await is_user_muted(chat_id, message.from_user.id):
            return

        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [a.user.id for a in admins]
        if not await is_command_admin(
            bot, chat_id, message.from_user.id, owner_id=COSMOS_ID
        ):
            return

        if not message.reply_to_message:
            return

        target = message.reply_to_message.from_user
        target_id = target.id
        full_name = await get_full_name(target)

        if target_id in admin_ids:
            sent = await bot_answer(message, "Нельзя банить администраторов.", wait=True)
            await save_timed_message(chat_id, sent.message_id)
            return

        await bot.ban_chat_member(chat_id, target_id)

        sent = await bot_answer(message, f"Пользователь {full_name} был забанен.", wait=True)
        await save_timed_message(chat_id, sent.message_id)
    except Exception as e:
        print("Ошибка /ban:", e)


# Команда /kick: кикает пользователя из reply-сообщения (через ban+unban).
@router.message(Command("kick"))
async def handle_kick_command(message: Message, bot: Bot):
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if await is_user_muted(chat_id, message.from_user.id):
            return

        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [a.user.id for a in admins]
        if not await is_command_admin(
            bot, chat_id, message.from_user.id, owner_id=COSMOS_ID
        ):
            return

        if not message.reply_to_message:
            return

        target = message.reply_to_message.from_user
        target_id = target.id
        full_name = await get_full_name(target)

        if target_id in admin_ids:
            sent = await bot_answer(message, "Нельзя кикать администраторов.", wait=True)
            await save_timed_message(chat_id, sent.message_id)
            return

        await bot.ban_chat_member(chat_id, target_id)
        await bot.unban_chat_member(chat_id, target_id)

        sent = await bot_answer(message, f"Пользователь {full_name} был кикнут из чата.", wait=True)
        await save_timed_message(chat_id, sent.message_id)
    except Exception as e:
        print("Ошибка /kick:", e)


# Команда /mute: выдает мут на указанное количество минут пользователю из reply.
@router.message(Command("mute"))
async def handle_mute_command(message: Message, bot: Bot):
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if await is_user_muted(chat_id, message.from_user.id):
            return

        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [a.user.id for a in admins]
        if not await is_command_admin(
            bot, chat_id, message.from_user.id, owner_id=COSMOS_ID
        ):
            return

        if not message.reply_to_message:
            return

        args = message.text.split()
        if len(args) != 2 or not args[1].isdigit():
            return

        minutes = int(args[1])
        if minutes <= 0:
            return

        target = message.reply_to_message.from_user
        target_id = target.id
        full_name = await get_full_name(target)

        if target_id in admin_ids:
            sent = await bot_answer(message, "Нельзя выдавать мут администраторам.", wait=True)
            await save_timed_message(chat_id, sent.message_id)
            return

        mute_until = int(time.time()) + minutes * 60

        async with db() as cur:
            await cur.execute(
                "SELECT 1 FROM mutes WHERE chat_id = ? AND user_id = ?",
                (chat_id, target_id)
            )
            row = await cur.fetchone()
            if row:
                await cur.execute(
                    "UPDATE mutes SET until = ? WHERE chat_id = ? AND user_id = ?",
                    (mute_until, chat_id, target_id)
                )
            else:
                await cur.execute(
                    "INSERT INTO mutes (chat_id, user_id, until) VALUES (?, ?, ?)",
                    (chat_id, target_id, mute_until)
                )

        sent = await bot_answer(
            message,
            f"Пользователь {full_name} получил мут на {minutes} минут.",
            wait=True
        )
        await save_timed_message(chat_id, sent.message_id)
    except Exception as e:
        print("Ошибка /mute:", e)


# Команда /unmute: снимает мут с пользователя из reply.
@router.message(Command("unmute"))
async def handle_unmute_command(message: Message, bot: Bot):
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if await is_user_muted(chat_id, message.from_user.id):
            return

        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = [a.user.id for a in admins]
        if not await is_command_admin(
            bot, chat_id, message.from_user.id, owner_id=COSMOS_ID
        ):
            return

        if not message.reply_to_message:
            return

        target = message.reply_to_message.from_user
        target_id = target.id
        full_name = await get_full_name(target)

        async with db() as cur:
            await cur.execute(
                "DELETE FROM mutes WHERE chat_id = ? AND user_id = ?",
                (chat_id, target_id)
            )

        sent = await bot_answer(
            message,
            f"С пользователя {full_name} снят мут.",
            wait=True
        )
        await save_timed_message(chat_id, sent.message_id)
    except Exception as e:
        print("Ошибка /unmute:", e)
