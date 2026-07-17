# Команды бота: анкеты, рейтинг, статистика и административные действия.

from aiogram import Router, Bot
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, ChatPermissions

from bot.message_queue import bot_answer, bot_reply, bot_send_photo

from bot.database import db
from bot.utils import (
    save_timed_message, get_old_messages, update_old_message, get_full_name,
    is_in_chat_member, safe_delete, is_command_admin, resolve_bot_image_path,
)
from bot.handlers.moderation import is_user_muted, send_restriction_warning
from bot.warning_state import clear_warning, mark_form_saved, mark_form_started
from env_config import require_int_env

import asyncio
import html
import time

_bv_locks: dict[tuple[int, int], asyncio.Lock] = {}

router = Router()

COSMOS_ID = require_int_env("COSMOS_ID")


# Сохраняет текст анкеты пользователя в chat_users.
async def _save_filled_form(cur, chat_id: int, user_id: int, filled_form_text: str):
    await cur.execute(
        "SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=? LIMIT 1",
        (chat_id, user_id),
    )
    exists = await cur.fetchone()

    if exists:
        await cur.execute(
            """UPDATE chat_users
               SET filled_form_text=?, form_stage='saved',
                   form_saved_at=CAST(strftime('%s','now') AS INTEGER)
               WHERE chat_id=? AND user_id=?""",
            (filled_form_text, chat_id, user_id),
        )
    else:
        await cur.execute(
            """INSERT INTO chat_users (
                   chat_id, user_id, filled_form_text, form_stage, form_saved_at
               ) VALUES (?, ?, ?, 'saved', CAST(strftime('%s','now') AS INTEGER))""",
            (chat_id, user_id, filled_form_text),
        )


# Команда /save: сохраняет анкету пользователя из reply-сообщения.
@router.message(Command("save"))
async def handle_save_command(message: Message, bot):
    try:
        await safe_delete(message)

        if await is_command_admin(
            bot, message.chat.id, message.from_user.id, owner_id=COSMOS_ID
        ):
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

        async with db() as cur:
            allowed = await _has_view_permission(cur, chat_id, requester_id)

        if not allowed:
            await send_restriction_warning(message, "view")
            return

        if not replied_message:
            sent = await bot_answer(
                message,
                "Для просмотра анкеты пользователя вызовите команду /view в ответ на его сообщение.",
                wait=True
            )
            await save_timed_message(chat_id, sent.message_id)
            return

        target_user = replied_message.from_user
        target_id = target_user.id
        full_name = await get_full_name(target_user)

        async with db() as cur:
            await cur.execute(
                "SELECT filled_form_text FROM chat_users WHERE chat_id=? AND user_id=?",
                (chat_id, target_id),
            )
            row = await cur.fetchone()

        if row and row[0]:
            old_message_ids = await get_old_messages(chat_id)

            for old_id in old_message_ids:
                try:
                    await bot.delete_message(chat_id, old_id)
                except Exception:
                    pass

            safe_name = html.escape(full_name)
            safe_form = html.escape(row[0])

            text = (
                f"Я нашла анкету пользователя {safe_name}\n\n"
                f"<blockquote expandable>{safe_form}</blockquote>"
            )

            sent = await bot_answer(
                message,
                text,
                wait=True,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

            await update_old_message(chat_id, sent.message_id)

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
            current_score, current_level = stats_row
        else:
            current_score, current_level = 0, 0

        full_name = await get_full_name(message.from_user)
        safe_full_name = html.escape(full_name)
        user_name_link = f'<a href="tg://user?id={user_id}">{safe_full_name}</a>'

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

            if level_data:
                rank_name, image_path, button_text, button_url = level_data

                keyboard = (
                    InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
                    )
                    if button_text and button_url
                    else None
                )

                photo = FSInputFile(resolve_bot_image_path(f"bot/images/{image_path}"))

                sent = await bot_send_photo(
                    message,
                    photo,
                    caption=(
                        f"{user_name_link}, ваше текущее воплощение: {rank_name}\n\n"
                        f"Количество очков: {current_score}"
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    wait=True,
                )
            else:
                # Защита от неконсистентных данных: уровень есть, а записи в levels нет.
                sent = await bot_answer(
                    message,
                    "Произошла ошибка: данные уровня не найдены.",
                    wait=True,
                )

        if sent:
            await save_timed_message(chat_id, sent.message_id)

    except Exception as e:
        print("Ошибка /stats:", e)


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
