# Модерация контента и права пользователей: фильтры, лимиты и предупреждения.

from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram import Router, Bot
from aiogram.utils.formatting import html_decoration as hd

from bot.database import db
from bot.donations import (
    check_and_update_usage_limit,
    extend_donation_grant,
    get_active_donation_statuses,
    get_donation_view_timers,
    get_usage_limits,
    has_active_donation,
    send_usage_limit_notification,
    send_test_donation_notification,
    revoke_donation_grant,
    revoke_all_donation_grants,
)
from bot.message_queue import (
    bot_answer, bot_send_message
)
from bot.message_templates import get_message_template, render_template, send_configured_message
from bot.notification_delivery import send_notification_card
from bot.donation_revoke_settings import get_revoke_settings, log_revoke_action, render_revoke_text
from bot.handlers.badword_detector import detect_badword_details
from bot.handlers.emoji_detector import message_emoji_count
from bot.utils import (
    normalize_telegram_button_url,
    save_timed_message, get_full_name, safe_delete, is_command_admin,
)
from bot.warning_state import (
    FORM_STAGE_FILLING,
    FORM_STAGE_NEW,
    FORM_STAGE_SAVED,
    ONBOARDING_PERMISSION_TYPE,
    ensure_warning_schema,
    form_filling_permission_type,
    get_form_stage,
    post_save_permission_type,
    replace_warning,
)
from env_config import require_int_env

import asyncio
import re
import time

router = Router()
LOG_CHANNEL_ID = require_int_env("LOG_CHANNEL_ID")
SOURCE_CHAT_ID = require_int_env("SOURCE_CHAT_ID")
COSMOS_ID = require_int_env("COSMOS_ID")


def parse_timed_limit_args(text: str | None, default_limit: int) -> tuple[int, int] | None:
    """Разбирает ``/command 28d 75``; старый формат ``/command 28`` сохранён."""
    parts = (text or "").split()
    if len(parts) not in (2, 3):
        return None

    duration_match = re.fullmatch(r"(\d+)(?:d|д)?", parts[1].lower())
    if not duration_match:
        return None

    days = int(duration_match.group(1))
    if len(parts) == 3:
        if not parts[2].isdigit():
            return None
        daily_limit = int(parts[2])
    else:
        daily_limit = int(default_limit)

    if not (1 <= days <= 36500 and 1 <= daily_limit <= 100000):
        return None
    return days, daily_limit


# Формирует человекочитаемый тег чата для логов.
def tag_chat(chat_id: int) -> str:
    return f"#c{abs(int(chat_id))}"


# Формирует человекочитаемый тег пользователя для логов.
def tag_user(user_id: int) -> str:
    return f"#u{int(user_id)}"


# Отправляет лог удаления сообщения за мат в лог-канал.
async def send_badword_deleted_log(
    message: Message,
    trigger_word: str,
    canonical_word: str,
    trigger_type: str,
    message_text: str,
) -> None:
    if not LOG_CHANNEL_ID:
        return
    if message.chat.id != SOURCE_CHAT_ID:
        return
    if not message.from_user:
        return

    try:
        user = message.from_user
        chat_title = hd.quote(message.chat.title or str(message.chat.id))
        full_name = hd.quote(await get_full_name(user))
        trigger = hd.quote(trigger_word.strip()[:120] or "-")
        canonical = hd.quote(canonical_word.strip()[:120] or "-")
        trigger_type_label = "префикс" if trigger_type == "prefix" else "точное слово"

        source_text = (message_text or "").strip()
        if not source_text:
            source_text = "<без текста>"
        if len(source_text) > 3500:
            source_text = source_text[:3500] + "..."

        quoted_text = hd.quote(source_text)
        event_time = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime())

        blocks = [
            f"{chat_title}\n{tag_chat(message.chat.id)}",
            f'<a href="tg://user?id={user.id}">👤 {full_name}</a>\n{tag_user(user.id)}',
            "Удалено сообщение за мат",
            f"Время: {event_time}",
            f"Триггер: <code>{trigger}</code> (<code>{canonical}</code>)",
            f"Тип триггера: {trigger_type_label}",
            f"<blockquote expandable>{quoted_text}</blockquote>",
            "#badword_deleted",
        ]

        await bot_send_message(
            message.bot,
            LOG_CHANNEL_ID,
            "\n\n".join(blocks),
            wait=True,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"Ошибка отправки badword-лога: {e}")


# Проверяет наличие ссылок в entities/caption_entities сообщения.
async def message_has_link(message: Message) -> bool:
    """
    Проверяет, есть ли в сообщении ссылка, по данным Telegram:
      - entities / caption_entities с type in ("url", "text_link").

    Без регэкспов — только то, что распарсил сам Телеграм.
    Работает и для текста, и для подписи к медиа.
    """
    entities = message.entities or []
    caption_entities = getattr(message, "caption_entities", None) or []

    for ent in list(entities) + list(caption_entities):
        if ent.type in ("url", "text_link"):
            return True

    return False


# Возвращает permission_level пользователя в чате (0/1/2) для правил модерации.
async def get_permission_level(chat_id: int, user_id: int) -> int:
    """
    Уровень прав (permission_level) в chat_users:
      0 — медиа запрещены; мат и ссылки запрещены
      1 — Медиа 1: фото, видео и GIF/анимации
      2 — Медиа 2: всё из Медиа 1, документы, аудио/музыка,
          реакции, мат и ссылки

    Голосовые сообщения, смайлики и кружки выдаются отдельными
    срочными разрешениями. Тег учитывается отдельно командой /tag.
    """
    async with db() as cur:
        await cur.execute(
            """
            SELECT permission_level
            FROM chat_users
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        )
        row = await cur.fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0


# Устанавливает permission_level пользователю (создает запись в chat_users при отсутствии).
async def set_permission_level(chat_id: int, user_id: int, level: int) -> None:
    """
    Устанавливает permission_level (0/1/2).
    Если юзера нет в chat_users — создаёт с нулями.
    """
    async with db() as cur:
        await cur.execute(
            """
            SELECT 1 FROM chat_users
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        )
        row = await cur.fetchone()

        if row is None:
            await cur.execute(
                """
                INSERT INTO chat_users (chat_id, user_id, score, level, permission_level)
                VALUES (?, ?, 0, 0, ?)
                """,
                (chat_id, user_id, level),
            )
        else:
            await cur.execute(
                """
                UPDATE chat_users
                SET permission_level = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (level, chat_id, user_id),
            )


# Проверяет право пользователя использовать /view (поле can_view_forms).
async def has_view_permission(chat_id: int, user_id: int) -> bool:
    """
    Есть ли у пользователя право смотреть анкеты (/view).
    Хранится в chat_users.can_view_forms (0/1).
    """
    async with db() as cur:
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


# Выдает/снимает право пользователя на просмотр анкет (/view).
async def set_view_permission(chat_id: int, user_id: int, allowed: bool = True) -> None:
    """
    Устанавливает право смотреть анкеты для пользователя.
    """
    value = 1 if allowed else 0
    async with db() as cur:
        await cur.execute(
            """
            SELECT 1 FROM chat_users
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        )
        row = await cur.fetchone()

        if row is None:
            await cur.execute(
                """
                INSERT INTO chat_users (chat_id, user_id, can_view_forms)
                VALUES (?, ?, ?)
                """,
                (chat_id, user_id, value),
            )
        else:
            await cur.execute(
                """
                UPDATE chat_users
                SET can_view_forms = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (value, chat_id, user_id),
            )


# Проверяет отдельные срочные донаты через единую таблицу donation_grants.
async def has_voice_permission(chat_id: int, user_id: int) -> bool:
    return await has_active_donation(chat_id, user_id, "voice")


async def has_emoji_permission(chat_id: int, user_id: int) -> bool:
    return await has_active_donation(chat_id, user_id, "emoji")


async def has_video_note_permission(chat_id: int, user_id: int) -> bool:
    return await has_active_donation(chat_id, user_id, "video_note")


# Проверяет активный мут пользователя и чистит просроченный мут.
async def is_user_muted(chat_id: int, user_id: int) -> bool:
    """
    True  — если пользователь сейчас в муте.
    False — если не в муте (или мут истёк; запись удаляется).
    """
    async with db() as cur:
        await cur.execute(
            """
            SELECT until, CAST(strftime('%s','now') AS INTEGER) AS now
            FROM mutes
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        )
        row = await cur.fetchone()

        if not row:
            return False

        mute_until = int(row[0])
        now = int(row[1])

        if mute_until <= now:
            try:
                await cur.execute(
                    """
                    DELETE FROM mutes
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (chat_id, user_id),
                )
            except Exception as e:
                print(f"Ошибка при очистке просроченного мута: {e}")
            return False

        return True


# Загружает шаблон предупреждения для конкретного типа ограничения (link/badword и т.д.).
async def get_permission_settings(permission_type: str):
    """Читает текст, изображение и кнопку для указанного ограничения."""
    await ensure_warning_schema()
    async with db() as cur:
        await cur.execute(
            """
            SELECT message, image_path, button_text, button_url
            FROM permission_types
            WHERE media_type = ?
            """,
            (permission_type,),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "message": row[0],
        "image_path": row[1],
        "button_text": row[2],
        "button_url": row[3],
    }


async def send_restriction_warning_to_chat(
    bot: Bot,
    *,
    chat_id: int,
    user,
    permission_type: str,
    media_group_id: str | None = None,
    message_thread_id: int | None = None,
    force_permission_type: bool = False,
) -> bool:
    """Отправляет одно актуальное предупреждение без тихих удалений.

    До /bv используется общий шаблон, между /bv и /save — отдельный
    промежуточный, после /save — обычный шаблон. Неизвестные типы Telegram
    используют общий шаблон ``other_media``. Ошибка картинки, HTML или кнопки
    не отменяет уведомление: бот повторяет отправку в упрощённом виде.
    """
    user_id = int(user.id)
    effective_type = str(permission_type or "").strip()
    form_stage = None

    if not force_permission_type and effective_type != "view":
        form_stage = await get_form_stage(chat_id, user_id)
        if form_stage == FORM_STAGE_NEW:
            effective_type = ONBOARDING_PERMISSION_TYPE
        elif form_stage == FORM_STAGE_FILLING:
            effective_type = form_filling_permission_type(effective_type)
        elif form_stage == FORM_STAGE_SAVED:
            effective_type = post_save_permission_type(effective_type)
    elif effective_type != "view":
        effective_type = post_save_permission_type(effective_type)

    settings = await get_permission_settings(effective_type)
    if not settings and form_stage == FORM_STAGE_FILLING:
        # Защита для старой базы без промежуточных шаблонов.
        effective_type = ONBOARDING_PERMISSION_TYPE
        settings = await get_permission_settings(effective_type)
    if not settings and form_stage == FORM_STAGE_SAVED:
        effective_type = post_save_permission_type("")
        settings = await get_permission_settings(effective_type)

    full_name = await get_full_name(user)
    safe_name = hd.quote(full_name)
    user_link = f'<a href="tg://user?id={user_id}">{safe_name}</a>'

    if settings:
        caption = str(settings.get("message") or "")
        button_text = str(settings.get("button_text") or "").strip()
        button_url = normalize_telegram_button_url(settings.get("button_url"))
        image_path = str(settings.get("image_path") or "").strip()
    else:
        # Последний встроенный fallback: сообщение не должно удаляться молча,
        # даже если администратор случайно удалил строку шаблона из SQLite.
        caption = (
            "{user}, это действие недоступно на вашем текущем уровне. "
            "Сообщение удалено."
        )
        button_text = ""
        button_url = ""
        image_path = ""

    caption = caption.replace("{user}", user_link)
    caption = caption.replace("{user_id}", str(user_id))
    caption = caption.replace("{full_name}", safe_name)
    if not caption.strip():
        caption = f"{user_link}, это действие сейчас недоступно. Сообщение удалено."

    keyboard = (
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
        )
        if button_text and button_url
        else None
    )

    async def _sender():
        return await send_notification_card(
            bot,
            chat_id=int(chat_id),
            text=caption,
            image_path=image_path,
            reply_markup=keyboard,
            message_thread_id=message_thread_id,
            context=f"restriction:{effective_type}:stage={form_stage or 'forced'}",
        )

    sent = await replace_warning(
        bot,
        chat_id=int(chat_id),
        user_id=user_id,
        media_group_id=media_group_id,
        sender=_sender,
    )
    # None означает, что это следующая часть уже обработанного альбома.
    return sent is not None or bool(media_group_id)


# Отправляет warning по ограничению и заменяет предыдущее сообщение пользователя.
async def send_restriction_warning(message: Message, permission_type: str) -> bool:
    if not message.from_user:
        return False
    try:
        return await send_restriction_warning_to_chat(
            message.bot,
            chat_id=message.chat.id,
            user=message.from_user,
            permission_type=permission_type,
            media_group_id=getattr(message, "media_group_id", None),
            message_thread_id=getattr(message, "message_thread_id", None),
        )
    except Exception as exc:
        print(
            "Ошибка отправки предупреждения "
            f"permission_type={permission_type} chat_id={message.chat.id}: {exc}"
        )
        return False


# Выдает/снимает разрешение на медиа по reply-команде.
@router.message(Command("media"))
async def media_permission_handler(message: Message, bot: Bot):
    """
    /media <0|1|2> — только в ответ на сообщение.
    Устанавливает permission_level пользователю.
    """
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if await is_user_muted(chat_id, message.from_user.id):
            return

        if not message.reply_to_message:
            return

        if not await is_command_admin(bot, chat_id, message.from_user.id, owner_id=COSMOS_ID):
            return

        # Ожидаем формат команды: /media <0|1|2>.
        args = message.text.split()
        if len(args) != 2 or args[1] not in ("0", "1", "2"):
            return

        level = int(args[1])
        target = message.reply_to_message.from_user

        await set_permission_level(chat_id, target.id, level)

        full_name = await get_full_name(target)

        level_descriptions = {
            0: "медиа отключены",
            1: "картинки, видео и GIF",
            2: (
                "картинки, видео, GIF, файлы, аудио/музыка, "
                "реакции, мат и ссылки"
            ),
        }
        await bot_answer(
            message,
            (
                f"Пользователю <b>{full_name}</b> установлен уровень "
                f"<b>Медиа {level}</b>.\n\n"
                f"Доступно: <b>{level_descriptions[level]}</b>."
            ),
            parse_mode="HTML",
        )

    except Exception as e:
        print("Ошибка /media:", e)


def _resolve_donation_command_target(message: Message, settings):
    """Цель команды снятия доната с учётом настроек reply/ID."""
    if settings.get("allow_reply") and message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user, message.reply_to_message.from_user.id

    parts = (message.text or "").split()
    numeric = next((part for part in parts[1:] if part.lstrip("-").isdigit()), None)
    if settings.get("allow_user_id") and numeric is not None:
        return None, int(numeric)
    return None, None


async def _donation_revoke_guard(message: Message, bot: Bot, command_key: str):
    """Проверки доступа, состояния команды и способа выбора пользователя."""
    if not message.from_user:
        return None
    settings = await get_revoke_settings()
    command_settings = settings["commands"].get(command_key, {})
    if not int(command_settings.get("enabled", 1)):
        return None

    chat_id = message.chat.id
    if await is_user_muted(chat_id, message.from_user.id):
        return None

    actor_id = int(message.from_user.id)
    mode = settings.get("access_mode", "admins")
    allowed = actor_id == int(COSMOS_ID)
    if mode == "admins":
        allowed = allowed or await is_command_admin(bot, chat_id, actor_id, owner_id=COSMOS_ID)
    elif mode == "allowlist":
        allowed = allowed or actor_id in settings.get("allowed_ids", set())
    if not allowed:
        return None

    target_user, target_id = _resolve_donation_command_target(message, settings)
    if target_id is None:
        if settings.get("notify_chat", 1):
            await bot_answer(message, "Выберите пользователя ответом на сообщение или укажите Telegram ID. Проверьте разрешённые способы в админ-панели.")
        return None
    return chat_id, target_user, target_id, settings


async def _target_display_name(target_user, target_id: int) -> str:
    if target_user is not None:
        return hd.quote(await get_full_name(target_user))
    return f"ID {target_id}"


async def _finish_revoke(message: Message, bot: Bot, *, settings, target_id: int, name: str, donation: str, existed: bool, action: str, details: str):
    template = settings["success_template"] if existed else settings["missing_template"]
    text = render_revoke_text(template, user=name, donation=donation, command=action)
    if settings.get("notify_chat", 1):
        await bot_answer(message, text, parse_mode="HTML")
    if settings.get("notify_target"):
        try:
            await bot.send_message(target_id, text, parse_mode="HTML")
        except Exception:
            pass
    await log_revoke_action(
        source="telegram", admin_id=message.from_user.id,
        admin_name=await get_full_name(message.from_user), chat_id=message.chat.id,
        target_id=target_id, action=action, details=details, success=True,
    )


async def _handle_revoke_timed_donation(message: Message, bot: Bot, *, category: str, title: str) -> None:
    try:
        await safe_delete(message)
        resolved = await _donation_revoke_guard(message, bot, category)
        if not resolved:
            return
        chat_id, target_user, target_id, settings = resolved
        existed = await revoke_donation_grant(chat_id, target_id, category)
        name = await _target_display_name(target_user, target_id)
        await _finish_revoke(message, bot, settings=settings, target_id=target_id, name=name,
            donation=title, existed=existed, action=category, details="removed" if existed else "missing")
    except Exception as exc:
        print(f"Ошибка снятия доната {category}:", exc)


@router.message(Command("delem", "delemoji"))
async def delete_emoji_donation_handler(message: Message, bot: Bot):
    await _handle_revoke_timed_donation(message, bot, category="emoji", title="Эмодзи")


@router.message(Command("delgs", "delvoice"))
async def delete_voice_donation_handler(message: Message, bot: Bot):
    await _handle_revoke_timed_donation(message, bot, category="voice", title="Голосовые сообщения")


@router.message(Command("delcircle", "delcircles", "delvideo_note"))
async def delete_circle_donation_handler(message: Message, bot: Bot):
    await _handle_revoke_timed_donation(message, bot, category="video_note", title="Кружки")


@router.message(Command("deltag"))
async def delete_tag_donation_handler(message: Message, bot: Bot):
    await _handle_revoke_timed_donation(message, bot, category="tag", title="Тег")


@router.message(Command("delm", "delmedia"))
async def delete_media_donation_handler(message: Message, bot: Bot):
    try:
        await safe_delete(message)
        resolved = await _donation_revoke_guard(message, bot, "media")
        if not resolved: return
        chat_id, target_user, target_id, settings = resolved
        old_level = await get_permission_level(chat_id, target_id)
        await set_permission_level(chat_id, target_id, 0)
        name = await _target_display_name(target_user, target_id)
        await _finish_revoke(message, bot, settings=settings, target_id=target_id, name=name,
            donation="Медиа", existed=old_level > 0, action="media", details=f"media {old_level}->0")
    except Exception as exc:
        print("Ошибка /delm:", exc)


@router.message(Command("delm2", "delmedia2"))
async def delete_media2_donation_handler(message: Message, bot: Bot):
    try:
        await safe_delete(message)
        resolved = await _donation_revoke_guard(message, bot, "media2")
        if not resolved: return
        chat_id, target_user, target_id, settings = resolved
        old_level = await get_permission_level(chat_id, target_id)
        new_level = 0 if settings.get("media2_behavior") == "remove" else (1 if old_level >= 2 else old_level)
        await set_permission_level(chat_id, target_id, new_level)
        name = await _target_display_name(target_user, target_id)
        await _finish_revoke(message, bot, settings=settings, target_id=target_id, name=name,
            donation="Медиа 2", existed=old_level >= 2, action="media2", details=f"media {old_level}->{new_level}")
    except Exception as exc:
        print("Ошибка /delm2:", exc)


@router.message(Command("delmax"))
async def delete_all_donations_handler(message: Message, bot: Bot):
    try:
        await safe_delete(message)
        resolved = await _donation_revoke_guard(message, bot, "all")
        if not resolved: return
        chat_id, target_user, target_id, settings = resolved
        parts = [p.lower() for p in (message.text or "").split()[1:]]
        if settings.get("require_delmax_confirmation") and "confirm" not in parts and "подтверждаю" not in parts:
            if settings.get("notify_chat", 1):
                await bot_answer(message, "Это действие снимет все донаты. Повторите команду, добавив <code>confirm</code>: <code>/delmax confirm ID</code>, либо ответьте на сообщение командой <code>/delmax confirm</code>.", parse_mode="HTML")
            return
        categories = await revoke_all_donation_grants(chat_id, target_id)
        old_level = await get_permission_level(chat_id, target_id)
        await set_permission_level(chat_id, target_id, 0)
        name = await _target_display_name(target_user, target_id)
        existed = bool(categories or old_level > 0)
        await _finish_revoke(message, bot, settings=settings, target_id=target_id, name=name,
            donation="Все донаты", existed=existed, action="all", details=f"categories={','.join(categories)}; media {old_level}->0")
    except Exception as exc:
        print("Ошибка /delmax:", exc)


# Выдает/продлевает доступ к голосовым сообщениям.
@router.message(Command("voice"))
async def voice_allow_handler(message: Message, bot: Bot):
    """``/voice 28d 90`` — срок и персональный суточный лимит ГС."""
    try:
        await safe_delete(message)
        chat_id = message.chat.id
        if await is_user_muted(chat_id, message.from_user.id):
            return
        if not message.reply_to_message:
            return
        if not await is_command_admin(bot, chat_id, message.from_user.id, owner_id=COSMOS_ID):
            return

        default_limit = (await get_usage_limits())["voice"]
        parsed = parse_timed_limit_args(message.text, default_limit)
        if parsed is None:
            await bot_answer(
                message,
                "Формат: <code>/voice 28d 90</code>, где 28d — срок, 90 — лимит ГС в сутки.",
                parse_mode="HTML",
            )
            return
        days, daily_limit = parsed

        target_user = message.reply_to_message.from_user
        valid_until, _ = await extend_donation_grant(
            chat_id, target_user.id, "voice", days, daily_limit=daily_limit
        )
        full_name = hd.quote(await get_full_name(target_user))
        formatted_date = time.strftime("%d.%m.%Y", time.localtime(valid_until))
        await bot_answer(
            message,
            f'<a href="tg://user?id={target_user.id}">{full_name}</a>, вам выданы '
            f"голосовые на {days} дней (до {formatted_date}). "
            f"Суточный лимит: {daily_limit}.",
            parse_mode="HTML",
        )
    except Exception as e:
        print("Ошибка /voice:", e)


# Выдает/продлевает доступ к эмодзи.
@router.message(Command("emoji"))
async def emoji_allow_handler(message: Message, bot: Bot):
    """``/emoji 28d 75`` — срок и персональный суточный лимит смайлов."""
    try:
        await safe_delete(message)
        chat_id = message.chat.id
        if await is_user_muted(chat_id, message.from_user.id):
            return
        if not message.reply_to_message:
            return
        if not await is_command_admin(bot, chat_id, message.from_user.id, owner_id=COSMOS_ID):
            return

        default_limit = (await get_usage_limits())["emoji"]
        parsed = parse_timed_limit_args(message.text, default_limit)
        if parsed is None:
            await bot_answer(
                message,
                "Формат: <code>/emoji 28d 75</code>, где 28d — срок, 75 — лимит смайлов в сутки.",
                parse_mode="HTML",
            )
            return
        days, daily_limit = parsed

        target_user = message.reply_to_message.from_user
        valid_until, _ = await extend_donation_grant(
            chat_id, target_user.id, "emoji", days, daily_limit=daily_limit
        )
        full_name = hd.quote(await get_full_name(target_user))
        formatted_date = time.strftime("%d.%m.%Y", time.localtime(valid_until))
        await bot_answer(
            message,
            f'<a href="tg://user?id={target_user.id}">{full_name}</a>, вам выданы '
            f"смайлики на {days} дней (до {formatted_date}). "
            f"Суточный лимит: {daily_limit}.",
            parse_mode="HTML",
        )
    except Exception as e:
        print("Ошибка /emoji:", e)


# Выдает/продлевает отдельное разрешение на кружки.
@router.message(Command("circle", "circles", "video_note"))
async def video_note_allow_handler(message: Message, bot: Bot):
    """``/circle 30d 105`` — срок и персональный суточный лимит кружков."""
    try:
        await safe_delete(message)
        chat_id = message.chat.id
        if await is_user_muted(chat_id, message.from_user.id):
            return
        if not message.reply_to_message:
            return
        if not await is_command_admin(bot, chat_id, message.from_user.id, owner_id=COSMOS_ID):
            return

        default_limit = (await get_usage_limits())["video_note"]
        parsed = parse_timed_limit_args(message.text, default_limit)
        if parsed is None:
            await bot_answer(
                message,
                "Формат: <code>/circle 30d 105</code>, где 30d — срок, 105 — лимит кружков в сутки.",
                parse_mode="HTML",
            )
            return
        days, daily_limit = parsed

        target_user = message.reply_to_message.from_user
        valid_until, _ = await extend_donation_grant(
            chat_id, target_user.id, "video_note", days, daily_limit=daily_limit
        )
        full_name = hd.quote(await get_full_name(target_user))
        formatted_date = time.strftime("%d.%m.%Y", time.localtime(valid_until))
        await bot_answer(
            message,
            f'<a href="tg://user?id={target_user.id}">{full_name}</a>, вам выданы '
            f"кружки на {days} дней (до {formatted_date}). "
            f"Суточный лимит: {daily_limit}.",
            parse_mode="HTML",
        )
    except Exception as e:
        print("Ошибка /circle:", e)


# Номинально учитывает срок тега, который администратор выдаёт вручную в Telegram.
@router.message(Command("tag"))
async def tag_allow_handler(message: Message, bot: Bot):
    """
    /tag [дней] — только в ответ на сообщение. По умолчанию выдаёт 28 дней.
    Команда не меняет Telegram-тег, а ведёт срок и отправляет уведомления.
    """
    try:
        await safe_delete(message)

        chat_id = message.chat.id
        if await is_user_muted(chat_id, message.from_user.id):
            return
        if not message.reply_to_message:
            return

        if not await is_command_admin(bot, chat_id, message.from_user.id, owner_id=COSMOS_ID):
            return

        args = (message.text or "").split()
        if len(args) == 1:
            days = 28
        elif len(args) == 2:
            duration_match = re.fullmatch(r"(\d+)(?:d|д)?", args[1].lower())
            if not duration_match:
                return
            days = int(duration_match.group(1))
        else:
            return

        if days <= 0:
            return

        target_user = message.reply_to_message.from_user
        valid_until, _ = await extend_donation_grant(
            chat_id, target_user.id, "tag", days
        )

        full_name = hd.quote(await get_full_name(target_user))
        formatted_date = time.strftime("%d.%m.%Y", time.localtime(valid_until))
        await bot_answer(
            message,
            f'<a href="tg://user?id={target_user.id}">{full_name}</a>, '
            f"вам выдан тег на {days} дней (до {formatted_date}).",
            parse_mode="HTML",
        )
    except Exception as e:
        print("Ошибка /tag:", e)


# Реакции входят в уровень «Медиа 2» и отдельно по сроку не выдаются.


# Скрытая админ-команда для проверки шаблонов донат-уведомлений.
@router.message(Command("donattest", "dntest"))
async def donation_notification_test_handler(message: Message, bot: Bot):
    """
    /donattest <категория> [expired|pre|denied]

    Скрытая проверка шаблонов без изменения реального доната.
    Доступна администраторам чата и владельцу из COSMOS_ID.
    """
    if not message.from_user:
        return

    try:
        # Владелец из .env может тестировать команду, даже если его аккаунту
        # ещё не выданы права администратора в группе.
        is_owner = int(message.from_user.id) == int(COSMOS_ID)
        is_chat_admin = await is_command_admin(
            bot, message.chat.id, message.from_user.id, owner_id=COSMOS_ID
        )

        if not (is_owner or is_chat_admin):
            await safe_delete(message)
            return

        args = (message.text or "").split()
        if len(args) < 2 or len(args) > 3:
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "Формат: <code>/donattest voice [expired|pre]</code>\n"
                    "Для проверки запрета реакций: "
                    "<code>/donattest reaction denied</code>\n"
                    "Срочные донаты: <code>voice</code>, <code>emoji</code>, "
                    "<code>tag</code>, <code>circle</code>.\n"
                    "Лимиты: <code>/donattest voice limit</code>, "
                    "<code>/donattest emoji limit</code> или "
                    "<code>/donattest circle limit</code>.\n"
                    "Проверка запрета реакций: <code>reaction denied</code>."
                ),
                parse_mode="HTML",
                message_thread_id=message.message_thread_id,
            )
            await safe_delete(message)
            return

        category_aliases = {
            "voice": "voice",
            "гс": "voice",
            "голос": "voice",
            "emoji": "emoji",
            "эмодзи": "emoji",
            "смайлики": "emoji",
            "tag": "tag",
            "тег": "tag",
            "circle": "video_note",
            "circles": "video_note",
            "video_note": "video_note",
            "кружок": "video_note",
            "кружки": "video_note",
            "reaction": "reaction",
            "reactions": "reaction",
            "реакция": "reaction",
            "реакции": "reaction",
        }
        event_aliases = {
            "expired": "expired",
            "end": "expired",
            "конец": "expired",
            "истек": "expired",
            "истёк": "expired",
            "pre": "preexpiry",
            "preexpiry": "preexpiry",
            "3d": "preexpiry",
            "3": "preexpiry",
            "скоро": "preexpiry",
            "limit": "limit_exhausted",
            "лимит": "limit_exhausted",
            "исчерпан": "limit_exhausted",
            "закончился": "limit_exhausted",
            "denied": "denied",
            "deny": "denied",
            "запрет": "denied",
            "нельзя": "denied",
        }

        category = category_aliases.get(args[1].lower())
        event_type = (
            event_aliases.get(args[2].lower(), "")
            if len(args) == 3
            else "expired"
        )
        if (
            category is None
            or not event_type
            or (category == "reaction" and event_type != "denied")
            or (category != "reaction" and event_type == "denied")
            or (
                event_type == "limit_exhausted"
                and category not in ("voice", "emoji", "video_note")
            )
        ):
            await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    "Неизвестная категория или тип. Примеры: "
                    "<code>/donattest voice pre</code> или "
                    "<code>/donattest reaction denied</code>."
                ),
                parse_mode="HTML",
                message_thread_id=message.message_thread_id,
            )
            await safe_delete(message)
            return

        target_message = message.reply_to_message or message
        target_user = (
            message.reply_to_message.from_user
            if message.reply_to_message and message.reply_to_message.from_user
            else message.from_user
        )

        await send_test_donation_notification(
            bot,
            chat_id=message.chat.id,
            user_id=target_user.id,
            category=category,
            event_type=event_type,
            reply_to_message_id=target_message.message_id,
            message_thread_id=message.message_thread_id,
        )
        print(
            "Тест донат-уведомления отправлен: "
            f"chat_id={message.chat.id} user_id={target_user.id} "
            f"category={category} event_type={event_type} "
            f"thread_id={message.message_thread_id}"
        )
        await safe_delete(message)

    except Exception as e:
        print(f"Ошибка /donattest: {e}")
        # Ошибка отправляется напрямую в тот же топик. Так она не потеряется
        # в фоне очереди и пользователь увидит причину (HTML, URL, фото и т. п.).
        try:
            await bot.send_message(
                chat_id=message.chat.id,
                text=f"Не удалось отправить тест: <code>{hd.quote(str(e))}</code>",
                parse_mode="HTML",
                message_thread_id=message.message_thread_id,
            )
        except Exception as feedback_error:
            print(f"Не удалось показать ошибку /donattest в чате: {feedback_error}")
        await safe_delete(message)


# Фоновые задачи удаления сообщений просмотра донатов.
_VIEW_DELETE_TASKS: set[asyncio.Task] = set()


def _schedule_view_message_delete(
    bot: Bot, chat_id: int, message_id: int, delay_seconds: int
) -> None:
    """Удаляет сообщение после настроенной задержки, не блокируя обработчик."""
    if delay_seconds <= 0:
        return

    async def _delete_later() -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Сообщение мог удалить администратор или сам Telegram.
            pass

    task = asyncio.create_task(_delete_later())
    _VIEW_DELETE_TASKS.add(task)
    task.add_done_callback(_VIEW_DELETE_TASKS.discard)


async def _build_donation_view_values(
    chat_id: int,
    user_id: int,
    *,
    full_name: str,
    username: str | None = None,
    empty_text: str = "Активных донат-функций сейчас нет.",
) -> dict[str, str]:
    """Готовит безопасные переменные для гибкого шаблона /viewd и /viewmd."""
    level = await get_permission_level(chat_id, user_id)
    grants = await get_active_donation_statuses(chat_id, user_id)

    safe_name = hd.quote(full_name or "Пользователь")
    identity = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
    if username:
        identity += f" · @{hd.quote(username)}"
    identity += f" · <code>{user_id}</code>"

    lines: list[str] = []
    if level > 0:
        lines.append(f"• <b>Медиа {level}</b> — бессрочно")

    for grant in grants:
        valid_until = time.strftime(
            "%d.%m.%Y %H:%M", time.localtime(grant["valid_until"])
        )
        line = f"• <b>{hd.quote(grant['title'])}</b> — до {valid_until}"
        if grant["daily_limit"] > 0:
            used = min(int(grant["used_today"]), int(grant["daily_limit"]))
            line += f"\n  Сегодня: {used}/{grant['daily_limit']}"
        lines.append(line)

    if not lines:
        lines.append(empty_text)

    return {
        "identity": identity,
        "full_name": safe_name,
        "username": hd.quote(username or ""),
        "user_id": str(user_id),
        "donation_lines": "\n".join(lines),
        "permanent_level": str(level),
    }


async def _send_timed_donation_view(
    message: Message,
    bot: Bot,
    *,
    template: dict,
    text: str,
) -> None:
    delay_seconds = max(0, min(int(template.get("delete_seconds", 30)), 86400))
    if delay_seconds > 0 and template.get("show_delete_notice"):
        text += f"\n\n<i>Сообщение удалится через {delay_seconds} сек.</i>"

    sent = await send_configured_message(
        bot,
        message.chat.id,
        template,
        text,
        message_thread_id=message.message_thread_id,
    )
    if sent is not None:
        _schedule_view_message_delete(
            bot, message.chat.id, sent.message_id, delay_seconds
        )


# Показывает пользователю его активные донат-функции и сроки.
@router.message(Command("viewd"))
async def view_donations_handler(message: Message, bot: Bot):
    try:
        await safe_delete(message)
        if not message.from_user:
            return

        template = await get_message_template("viewd")
        if not template.get("enabled"):
            return
        values = await _build_donation_view_values(
            message.chat.id,
            message.from_user.id,
            full_name=await get_full_name(message.from_user),
            username=message.from_user.username,
            empty_text=str(template.get("empty_text") or ""),
        )
        text = render_template(str(template.get("message") or ""), values)
        await _send_timed_donation_view(message, bot, template=template, text=text)
    except Exception as e:
        print("Ошибка /viewd:", e)


# Позволяет настоящим администраторам посмотреть донаты другого пользователя.
# Использование: ответом на сообщение пользователя или /viewmd <Telegram ID>.
@router.message(Command("viewmd"))
async def view_member_donations_handler(message: Message, bot: Bot):
    try:
        await safe_delete(message)
        if not message.from_user:
            return
        if not await is_command_admin(
            bot, message.chat.id, message.from_user.id, owner_id=COSMOS_ID
        ):
            return

        template = await get_message_template("viewmd")
        if not template.get("enabled"):
            return

        target_user = None
        target_user_id: int | None = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
            target_user_id = target_user.id
        else:
            for entity in message.entities or []:
                mentioned = getattr(entity, "user", None)
                if mentioned and mentioned.id != message.from_user.id:
                    target_user = mentioned
                    target_user_id = mentioned.id
                    break

            if target_user_id is None:
                parts = (message.text or "").split(maxsplit=1)
                argument = parts[1].strip() if len(parts) == 2 else ""
                if re.fullmatch(r"-?\d+", argument):
                    target_user_id = int(argument)
                    try:
                        member = await bot.get_chat_member(message.chat.id, target_user_id)
                        target_user = member.user
                    except Exception:
                        target_user = None

        if target_user_id is None:
            usage = str(template.get("usage_text") or "")
            if usage:
                await _send_timed_donation_view(
                    message, bot, template=template, text=usage
                )
            return

        full_name = await get_full_name(target_user) if target_user else "Пользователь"
        username = target_user.username if target_user else None
        values = await _build_donation_view_values(
            message.chat.id,
            target_user_id,
            full_name=full_name,
            username=username,
            empty_text=str(template.get("empty_text") or ""),
        )
        text = render_template(str(template.get("message") or ""), values)
        await _send_timed_donation_view(message, bot, template=template, text=text)
    except Exception as e:
        print("Ошибка /viewmd:", e)


# Выдает право смотреть анкеты через /view.
@router.message(Command("canview"))
async def canview_allow_handler(message: Message, bot):
    try:
        await safe_delete(message)

        chat_id = message.chat.id

        if not message.reply_to_message:
            return

        if not await is_command_admin(bot, chat_id, message.from_user.id, owner_id=COSMOS_ID):
            return

        target = message.reply_to_message.from_user

        await set_view_permission(chat_id, target.id, True)

        full_name = await get_full_name(target)

        await bot_answer(
            message,
            f"Пользователю <b>{full_name}</b> выдано право просматривать анкеты.",
            parse_mode="HTML",
        )

    except Exception as e:
        print("Ошибка /canview:", e)


# Проводит текстовую модерацию: мут, мат, ссылки и лимиты эмодзи.
async def moderation_handle_text(message: Message, level: int) -> bool:
    """
    Общая текстовая модерация:
      - МУТ (таблица mutes)
      - мат (badword_handler)
      - ссылки (message_has_link)
      - эмодзи (право + лимит)

    Работает и для "голого" текста, и для подписи к медиа.
    Использует text = message.text или message.caption.
    """
    chat_id = message.chat.id
    user_id = message.from_user.id

    if await is_user_muted(chat_id, user_id):
        await safe_delete(message)
        return True

    text = message.text or message.caption or ""

    if text:
        badword_details = await detect_badword_details(text)
    else:
        badword_details = None

    if badword_details:
        if level < 2:
            trigger_word, canonical_word, trigger_type = badword_details
            await safe_delete(message)
            await send_badword_deleted_log(
                message,
                trigger_word,
                canonical_word,
                trigger_type,
                text,
            )
            await send_restriction_warning(message, "badword")
            return True

    if text and await message_has_link(message):
        if level < 2:
            await safe_delete(message)
            await send_restriction_warning(message, "link")
            return True

    emojis_count = await message_emoji_count(message)

    if emojis_count > 0:
        if not await has_emoji_permission(chat_id, user_id):
            await safe_delete(message)

            await send_restriction_warning(message, "emoji")
            return True

        allowed, limit, used_today = await check_and_update_usage_limit(
            chat_id, user_id, "emoji", emojis_count
        )
        if not allowed:
            await safe_delete(message)

            await send_usage_limit_notification(
                message.bot,
                chat_id=chat_id,
                user_id=user_id,
                category="emoji",
                limit=limit,
                used_today=used_today,
                reply_to_message_id=message.message_id,
                message_thread_id=message.message_thread_id,
            )
            return True

    return False


# Главный роутер модерации по типам контента сообщения.
async def moderation_handle_message(message: Message) -> bool:
    """
    Возвращает True, если модерация всё обработала
    (сообщение удалено, предупреждение отправлено)
    и дальше обрабатывать НЕ нужно.
    False — если всё ок, можно продолжать.
    """
    chat_id = message.chat.id
    user_id = message.from_user.id
    content_type = message.content_type
    level = await get_permission_level(chat_id, user_id)

    # Сначала единая текстовая проверка (мат/ссылки/эмодзи/мут).
    if await moderation_handle_text(message, level):
        return True

    # Ниже — проверки по типам вложений и медиа.
    if content_type == "sticker":
        await safe_delete(message)
        await send_restriction_warning(message, "sticker")
        return True

    if content_type == "audio":
        # Музыка и аудиофайлы входят только в «Медиа 2».
        if level < 2:
            await safe_delete(message)
            await send_restriction_warning(message, "audio")
            return True
        return False

    if content_type == "voice":
        if not await has_voice_permission(chat_id, user_id):
            await safe_delete(message)
            await send_restriction_warning(message, "voice")
            return True

        allowed, limit, used_today = await check_and_update_usage_limit(
            chat_id, user_id, "voice", 1
        )
        if not allowed:
            await safe_delete(message)
            await send_usage_limit_notification(
                message.bot,
                chat_id=chat_id,
                user_id=user_id,
                category="voice",
                limit=limit,
                used_today=used_today,
                reply_to_message_id=message.message_id,
                message_thread_id=message.message_thread_id,
            )
            return True

        return False

    if content_type == "video_note":
        if not await has_video_note_permission(chat_id, user_id):
            await safe_delete(message)
            await send_restriction_warning(message, "video_note")
            return True

        allowed, limit, used_today = await check_and_update_usage_limit(
            chat_id, user_id, "video_note", 1
        )
        if not allowed:
            await safe_delete(message)
            await send_usage_limit_notification(
                message.bot,
                chat_id=chat_id,
                user_id=user_id,
                category="video_note",
                limit=limit,
                used_today=used_today,
                reply_to_message_id=message.message_id,
                message_thread_id=message.message_thread_id,
            )
            return True
        return False

    if content_type == "document":
        # Любые файлы/документы доступны только на «Медиа 2».
        if level < 2:
            await safe_delete(message)
            await send_restriction_warning(message, "document")
            return True
        return False

    allowed_level0 = {"text"}
    if level == 0 and content_type not in allowed_level0:
        await safe_delete(message)
        await send_restriction_warning(message, content_type)
        return True

    # «Медиа 1»: только картинки, видео и GIF/анимации.
    allowed_level1 = {"text", "photo", "video", "animation"}
    if level == 1 and content_type not in allowed_level1:
        await safe_delete(message)
        await send_restriction_warning(message, content_type)
        return True

    return False
