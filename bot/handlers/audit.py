# Аудит изменений участников чата: join/leave/ban и изменения прав.

import asyncio

from dotenv import load_dotenv

from aiogram import Router
from aiogram.types import ChatMemberUpdated, ChatPermissions
from aiogram.enums import ChatMemberStatus
from aiogram.utils.formatting import html_decoration as hd

from bot.database import db
from bot.settings import get_restrict_new_members_telegram
from bot.message_queue import bot_send_message
from bot.utils import send_welcome_for_member
from env_config import require_int_env

load_dotenv()

LOG_CHANNEL_ID = require_int_env("LOG_CHANNEL_ID")
SOURCE_CHAT_ID = require_int_env("SOURCE_CHAT_ID")

router = Router()

KICK_GROUP_WINDOW = 1.0
PENDING_KICK: dict[tuple[int, int], asyncio.Task] = {}

USER_RIGHTS = [
    ("can_send_messages", "Отправка сообщений"),
    ("can_send_photos", "Фотографий"),
    ("can_send_videos", "Видео"),
    ("can_send_video_notes", "Видеосообщений"),
    ("can_send_audios", "Музыки"),
    ("can_send_voice_notes", "Голосовых сообщений"),
    ("can_send_documents", "Файлов"),
    ("can_send_other_messages", "Стикеров и GIF"),
    ("can_add_web_page_previews", "Предпросмотр ссылок"),
    ("can_send_polls", "Создание опросов"),
    ("can_invite_users", "Добавление участников"),
    ("can_pin_messages", "Закрепление сообщений"),
    ("can_change_info", "Изменение профиля группы"),
]

ADMIN_RIGHTS = [
    ("can_change_info", "Изменение профиля группы"),
    ("can_delete_messages", "Удаление сообщений"),
    ("can_restrict_members", "Блокировка пользователей"),
    ("can_invite_users", "Пригласительные ссылки"),
    ("can_pin_messages", "Закрепление сообщений"),
    ("can_post_stories", "Публикация историй"),
    ("can_edit_stories", "Изменение чужих историй"),
    ("can_delete_stories", "Удаление чужих историй"),
    ("can_manage_video_chats", "Управление видеочатами"),
    ("is_anonymous", "Анонимность"),
    ("can_promote_members", "Добавление администраторов"),
]

USER_FIELDS = {f for f, _ in USER_RIGHTS}
ADMIN_FIELDS = {f for f, _ in ADMIN_RIGHTS}
ALL_RIGHT_FIELDS = USER_FIELDS | ADMIN_FIELDS


# Формирует HTML-ссылку на пользователя Telegram.
def user_link(user, fallback_username=None):
    name = hd.quote(user.full_name)
    username = user.username or fallback_username
    text = f'<a href="tg://user?id={user.id}">👤 {name}</a>'
    if username:
        text += f" @{username}"
    return text


# Формирует человекочитаемый тег чата для лога.
def tag_chat(chat_id: int) -> str:
    return f"#c{abs(int(chat_id))}"


# Формирует человекочитаемый тег пользователя для лога.
def tag_user(user_id: int) -> str:
    return f"#u{int(user_id)}"


# Снимает снапшот ключевых полей участника чата.
def snapshot(member, rights_map):
    lines = []
    for field, label in rights_map:
        if not hasattr(member, field):
            continue
        v = getattr(member, field, None)
        if isinstance(v, bool):
            lines.append(f"{'+' if v else '-'} {label}")
    return lines


# Сравнивает булевы поля до/после и возвращает изменения.
def diff_bool_fields(old, new):
    added, removed = [], []

    for f in ALL_RIGHT_FIELDS:
        if not hasattr(old, f) or not hasattr(new, f):
            continue

        ov = getattr(old, f, None)
        nv = getattr(new, f, None)

        if isinstance(ov, bool) and isinstance(nv, bool) and ov != nv:
            (added if nv else removed).append(f)

    return added, removed


# Вычисляет итоговый статус участника по данным Telegram.
def effective_status(member) -> ChatMemberStatus:
    """
    Нормализация edge-case Telegram:
    restricted + is_member=False фактически означает "уже не участник" (как LEFT).
    """
    if member.status == ChatMemberStatus.RESTRICTED:
        if getattr(member, "is_member", True) is False:
            return ChatMemberStatus.LEFT
    return member.status


# Проверяет, считается ли участник "фактически в чате" для JOIN/LEAVE логики.
def is_in_chat(member) -> bool:
    """
    "Фактически в чате" — для логики JOIN/LEAVE.
    """
    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return False
    if member.status == ChatMemberStatus.RESTRICTED:
        return getattr(member, "is_member", True) is True
    return True


# Формирует и отправляет аудит-лог события в отдельный лог-канал.
async def send_log(event, event_tag, old, new, added, removed, change_lines):
    user = new.user
    actor = event.from_user
    chat = event.chat
    fallback_username = getattr(old.user, "username", None)

    rights_lines = None
    rights_title = None

    FORCE_USER_SNAPSHOT = {"member_restricted", "member_unrestricted"}
    FORCE_ADMIN_SNAPSHOT = {"admin_granted", "admin_revoked"}

    if event_tag in FORCE_ADMIN_SNAPSHOT or new.status == ChatMemberStatus.ADMINISTRATOR:
        rights_title = "Права администратора:"
        rights_lines = snapshot(new, ADMIN_RIGHTS)

    elif event_tag in FORCE_USER_SNAPSHOT:
        rights_title = "Права участника:"
        rights_lines = snapshot(new, USER_RIGHTS)

    elif added or removed:
        if new.status == ChatMemberStatus.ADMINISTRATOR:
            rights_title = "Права администратора:"
            rights_lines = snapshot(new, ADMIN_RIGHTS)
        else:
            rights_title = "Права участника:"
            rights_lines = snapshot(new, USER_RIGHTS)

    blocks = []

    blocks.append(f"{hd.quote(chat.title or str(chat.id))}\n{tag_chat(chat.id)}")
    blocks.append(f"{user_link(user, fallback_username)}\n{tag_user(user.id)}")

    if actor and actor.id != user.id and not (event.invite_link and event_tag in ("new_member", "member_rejoined")):
        blocks.append(f"By: {user_link(actor)}\n{tag_user(actor.id)}")

    if change_lines:
        blocks.append("\n".join(change_lines))

    if rights_lines:
        blocks.append(rights_title + "\n" + "\n".join(rights_lines))

    blocks.append(f"#{event_tag}")

    await bot_send_message(
        event.bot,
        LOG_CHANNEL_ID,
        "\n\n".join(blocks),
        wait=True,
        parse_mode="HTML",
    )


# Планирует отложенный бан/кик пользователя.
async def schedule_ban(key, event, old, new, added, removed, change_lines):
    try:
        await asyncio.sleep(KICK_GROUP_WINDOW)
        await send_log(event, "member_banned", old, new, added, removed, change_lines)
    except asyncio.CancelledError:
        pass
    finally:
        PENDING_KICK.pop(key, None)


# Логирует изменения статуса и прав участника.
@router.chat_member()
async def log_member_changes(event: ChatMemberUpdated):
    if event.chat.id != SOURCE_CHAT_ID:
        return

    old = event.old_chat_member
    new = event.new_chat_member

    old_s = effective_status(old)
    new_s = effective_status(new)

    user = new.user
    chat = event.chat

    added, removed = diff_bool_fields(old, new)
    change_lines = []

    # Сценарий 1: пользователь вошел/вернулся в чат.
    joined_now = (
        old_s in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
        and new_s in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED)
        and is_in_chat(new)
    )

    if joined_now:
        async with db() as cur:
            await cur.execute(
                "SELECT 1 FROM chat_users WHERE chat_id = ? AND user_id = ? LIMIT 1",
                (chat.id, user.id),
            )
            exists = await cur.fetchone()

            if not exists:
                await cur.execute(
                    "INSERT INTO chat_users (chat_id, user_id) VALUES (?, ?)",
                    (chat.id, user.id),
                )
                event_tag = "new_member"
            else:
                event_tag = "member_rejoined"

        # Старый режим принудительно урезал права участника средствами Telegram
        # сразу после входа. По умолчанию он выключен, чтобы не конфликтовать с
        # новым внутренним алгоритмом доступов. Код сохранён и доступен как
        # аварийный переключатель в админ-панели.
        if await get_restrict_new_members_telegram():
            try:
                await event.bot.restrict_chat_member(
                    chat_id=chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_photos=False,
                        can_send_videos=False,
                        can_send_video_notes=False,
                        can_send_audios=False,
                        can_send_voice_notes=False,
                        can_send_documents=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False,
                        can_send_polls=False,
                        can_invite_users=False,
                        can_pin_messages=False,
                        can_change_info=False,
                    ),
                    use_independent_chat_permissions=True,
                )
            except Exception as e:
                print(
                    "Не удалось выставить text-only права при входе "
                    f"user_id={user.id}: {e}"
                )

        if event.invite_link and event.invite_link.creator:
            change_lines.append(f"Added by: {user_link(event.invite_link.creator)}")

        # Приветствие отправляем по факту входа/возврата (статус участника), а не по системному сообщению.
        await send_welcome_for_member(event.bot, chat.id, user)
        await send_log(event, event_tag, old, new, added, removed, change_lines)
        return

    # Сценарий 2: сначала фиксируем KICKED как "возможный бан", подтверждаем через короткое окно.
    if new_s == ChatMemberStatus.KICKED:
        key = (chat.id, user.id)

        if key in PENDING_KICK:
            PENDING_KICK[key].cancel()

        task = asyncio.create_task(
            schedule_ban(key, event, old, new, added, removed, change_lines)
        )
        PENDING_KICK[key] = task
        return

    # Сценарий 3: обработка выхода/кика/разбана после окна агрегации.
    if new_s == ChatMemberStatus.LEFT:
        key = (chat.id, user.id)

        if key in PENDING_KICK:
            PENDING_KICK[key].cancel()
            PENDING_KICK.pop(key, None)
            event_tag = "member_kicked"

        elif old_s == ChatMemberStatus.KICKED:
            event_tag = "member_unbanned"

        else:
            event_tag = "member_left"

        await send_log(event, event_tag, old, new, added, removed, change_lines)
        return

    # Сценарий 4: изменения ролей администратора.
    if old_s != ChatMemberStatus.ADMINISTRATOR and new_s == ChatMemberStatus.ADMINISTRATOR:
        await send_log(event, "admin_granted", old, new, added, removed, change_lines)
        return

    if old_s == ChatMemberStatus.ADMINISTRATOR and new_s != ChatMemberStatus.ADMINISTRATOR:
        await send_log(event, "admin_revoked", old, new, added, removed, change_lines)
        return

    if old_s != ChatMemberStatus.RESTRICTED and new_s == ChatMemberStatus.RESTRICTED:
        await send_log(event, "member_restricted", old, new, added, removed, change_lines)
        return

    if old_s == ChatMemberStatus.RESTRICTED and new_s == ChatMemberStatus.MEMBER:
        await send_log(event, "member_unrestricted", old, new, added, removed, change_lines)
        return

    if added or removed:
        await send_log(event, "rights_changed", old, new, added, removed, change_lines)
        return
