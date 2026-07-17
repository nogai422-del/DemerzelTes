# Контроль реакций пользователей по уровню «Медиа 2».

import asyncio
import time

from aiogram import Bot, Router
from aiogram.types import MessageReactionUpdated

from bot.database import db
from bot.donations import send_reaction_denied_notification
from bot.handlers.moderation import send_restriction_warning_to_chat
from bot.warning_state import has_completed_form

router = Router()

# Защищает чат от спама одинаковыми предупреждениями, если пользователь
# несколько раз подряд пытается поставить реакцию без доступа.
_WARNING_COOLDOWN_SECONDS = 15
_last_warning_at: dict[tuple[int, int], float] = {}


def _warning_allowed(chat_id: int, user_id: int) -> bool:
    now = time.monotonic()
    key = (int(chat_id), int(user_id))
    previous = _last_warning_at.get(key, 0.0)
    if now - previous < _WARNING_COOLDOWN_SECONDS:
        return False
    _last_warning_at[key] = now

    # Периодически чистим старые записи, чтобы словарь не рос бесконечно.
    if len(_last_warning_at) > 5000:
        threshold = now - 3600
        stale = [item for item, ts in _last_warning_at.items() if ts < threshold]
        for item in stale:
            _last_warning_at.pop(item, None)
    return True


async def _has_media_level_two(chat_id: int, user_id: int) -> bool:
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
    return bool(row and int(row[0] or 0) >= 2)


@router.message_reaction()
async def enforce_reaction_media_level(
    event: MessageReactionUpdated,
    bot: Bot,
) -> None:
    """Удаляет новую реакцию пользователя без уровня «Медиа 2»."""
    user = event.user
    if user is None or user.is_bot:
        # Реакции от имени каналов/чатов нельзя сопоставить с обычным user_id.
        return

    # Пустой new_reaction означает, что пользователь сам убрал реакцию.
    if not event.new_reaction:
        return

    chat_id = int(event.chat.id)
    user_id = int(user.id)

    if await _has_media_level_two(chat_id, user_id):
        return

    try:
        await bot.delete_message_reaction(
            chat_id=chat_id,
            message_id=int(event.message_id),
            user_id=user_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(
            "Ошибка удаления реакции без Медиа 2 "
            f"chat_id={chat_id} message_id={event.message_id} user_id={user_id}: {exc}"
        )
        return

    if not _warning_allowed(chat_id, user_id):
        return

    try:
        if not await has_completed_form(chat_id, user_id):
            await send_restriction_warning_to_chat(
                bot,
                chat_id=chat_id,
                user=user,
                permission_type="onboarding",
                force_permission_type=True,
            )
        else:
            await send_reaction_denied_notification(
                bot,
                chat_id=chat_id,
                user_id=user_id,
                message_id=int(event.message_id),
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(
            "Ошибка предупреждения о реакции без Медиа 2 "
            f"chat_id={chat_id} user_id={user_id}: {exc}"
        )
