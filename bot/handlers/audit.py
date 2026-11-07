# Аудит изменений участников чата: join/leave/ban и изменения прав.

import asyncio
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from aiogram import Router
from aiogram.types import ChatMemberUpdated, ChatPermissions
from aiogram.enums import ChatMemberStatus
from aiogram.utils.formatting import html_decoration as hd

from bot.database import db
from bot.onboarding import sync_form_permissions
from bot.message_queue import bot_send_message
from bot.utils import send_welcome_for_member
from env_config import require_int_env

load_dotenv()

LOG_CHANNEL_ID = require_int_env("LOG_CHANNEL_ID")
SOURCE_CHAT_ID = require_int_env("SOURCE_CHAT_ID")

router = Router()

KICK_GROUP_WINDOW = 1.0
PENDING_KICK: dict[tuple[int, int], asyncio.Task] = {}

EVENT_TITLES = {
    'new_member': 'Вступил в группу',
    'member_rejoined': 'Вернулся в группу',
    'member_left': 'Вышел из группы',
    'member_banned': 'Забанен',
    'member_kicked': 'Удалён из группы',
    'member_unbanned': 'Разбанен',
    'admin_granted': 'Назначен администратором',
    'admin_revoked': 'Снят с должности администратора',
    'member_restricted': 'Ограничены права',
    'member_unrestricted': 'Ограничения сняты',
    'rights_changed': 'Изменены права',
}
EVENT_DETAILS = {
    'member_rejoined': 'Участник повторно вступил в группу.',
    'member_banned': 'Участник удалён. Повторный вход в группу запрещён.',
    'member_kicked': 'Участник удалён, но может вступить снова.',
    'member_unbanned': 'Повторный вход разрешён. Участник ещё не вернулся в группу.',
}
STATUS_TITLES = {
    'creator': 'Владелец', 'administrator': 'Администратор', 'member': 'Участник',
    'restricted': 'Участник с ограничениями', 'left': 'Не в группе', 'kicked': 'Забанен',
}
MOSCOW = timezone(timedelta(hours=3))


def status_title(member):
    status = effective_status(member)
    return STATUS_TITLES.get(getattr(status, 'value', status), 'Статус изменён')


def event_date_text(value):
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, datetime) or value.year <= 1970:
        return 'Без срока'
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')

USER_RIGHTS = [
    ("can_send_messages", "Отправка сообщений"),
    ("can_send_photos", "Фото"),
    ("can_send_videos", "Видео"),
    ("can_send_video_notes", "Кружки"),
    ("can_send_audios", "Музыка и аудиофайлы"),
    ("can_send_voice_notes", "Голосовые сообщения"),
    ("can_send_documents", "Файлы"),
    ("can_send_other_messages", "Стикеры и GIF"),
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
            lines.append(f"{'✅ Разрешено' if v else '⛔ Запрещено'}: {label}")
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
        rights_title = "Права участника в Telegram:"
        rights_lines = snapshot(new, USER_RIGHTS)

    elif added or removed:
        if new.status == ChatMemberStatus.ADMINISTRATOR:
            rights_title = "Права администратора:"
            rights_lines = snapshot(new, ADMIN_RIGHTS)
        else:
            rights_title = "Права участника в Telegram:"
            rights_lines = snapshot(new, USER_RIGHTS)

    blocks = [f"<b>{EVENT_TITLES.get(event_tag, 'Изменился статус участника')}</b>"]
    if event_tag in EVENT_DETAILS:
        blocks.append(EVENT_DETAILS[event_tag])
    blocks.append(f"Участник: {user_link(user, fallback_username)}\nID участника: <code>{user.id}</code>")
    blocks.append(f"Группа: {hd.quote(chat.title or str(chat.id))}\nID группы: <code>{chat.id}</code>")
    blocks.append(f"Статус: {status_title(old)} → {status_title(new)}")
    if getattr(event, 'date', None):
        blocks.append(f"Время события: {event_date_text(event.date)}")
    if event_tag in ('member_banned', 'member_restricted'):
        blocks.append(f"Срок ограничения: {event_date_text(getattr(new, 'until_date', None))}")

    if actor and actor.id != user.id and not (event.invite_link and event_tag in ("new_member", "member_rejoined")):
        blocks.append(f"Действие выполнил: {user_link(actor)}\nID: <code>{actor.id}</code>")

    if change_lines:
        blocks.append("\n".join(change_lines))

    labels = dict(USER_RIGHTS + ADMIN_RIGHTS)
    if added:
        blocks.append('Разрешено: ' + ', '.join(labels[field] for field in labels if field in added))
    if removed:
        blocks.append('Запрещено: ' + ', '.join(labels[field] for field in labels if field in removed))
    if rights_lines:
        blocks.append(rights_title + "\n" + "\n".join(rights_lines))
        if rights_title == "Права участника в Telegram:":
            blocks.append("Донатные разрешения отдельно проверяет Демерзель.")

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

        try:
            await sync_form_permissions(event.bot, chat.id, user.id)
        except Exception as exc:
            print(f"Не удалось применить права анкеты user_id={user.id}: {exc}")

        if event.invite_link and event.invite_link.creator:
            change_lines.append(f"Автор пригласительной ссылки: {user_link(event.invite_link.creator)}")

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
