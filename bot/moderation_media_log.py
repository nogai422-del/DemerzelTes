"""Сохраняет удаляемые модерацией вложения в существующей группе логов."""
import html

from aiogram.types import ReplyParameters

from bot.message_queue import _fire, _send
from bot.utils import safe_delete
from env_config import require_int_env

LOG_CHANNEL_ID = require_int_env('LOG_CHANNEL_ID')
SOURCE_CHAT_ID = require_int_env('SOURCE_CHAT_ID')
MEDIA_TITLES = {
    'photo': 'Фото', 'video': 'Видео', 'animation': 'GIF',
    'voice': 'Голосовое сообщение', 'video_note': 'Кружок',
    'audio': 'Аудио', 'document': 'Файл', 'sticker': 'Стикер',
}


async def delete_with_media_log(message):
    """Удаляет сразу, затем отправляет файл по file_id через ограниченную очередь.

    Отправка по file_id не зависит от уже удалённого исходного сообщения.
    Ошибка или недоступность группы логов не задерживает модерацию.
    """
    kind = str(getattr(message.content_type, 'value', message.content_type))
    attachment = getattr(message, kind, None) if kind in MEDIA_TITLES else None
    if kind == 'photo' and attachment:
        attachment = attachment[-1]
    file_id = getattr(attachment, 'file_id', None)
    user = getattr(message, 'from_user', None)
    should_log = (file_id and user and message.chat.id == SOURCE_CHAT_ID
                  and LOG_CHANNEL_ID and LOG_CHANNEL_ID != SOURCE_CHAT_ID)
    await safe_delete(message)
    if not should_log:
        return None

    name = ' '.join(filter(None, (user.first_name, getattr(user, 'last_name', None))))
    username = getattr(user, 'username', None)
    identity = html.escape(name or str(user.id))
    text = (f'#deleted_media\n<b>Удалено модерацией: {MEDIA_TITLES[kind]}</b>\n'
            f'<a href="tg://user?id={user.id}">{identity}</a>'
            f' · <code>{user.id}</code>')
    if username:
        text += f' · @{html.escape(username)}'
    text += (f'\nЧат: <code>{message.chat.id}</code>'
             f'\nСообщение: <code>{message.message_id}</code>')
    if getattr(message, 'media_group_id', None):
        text += f'\nАльбом: <code>{html.escape(str(message.media_group_id))}</code>'

    async def deliver():
        kwargs = {'disable_notification': True}
        if kind not in ('video_note', 'sticker'):
            kwargs.update(caption=getattr(message, 'caption', None), parse_mode=None)
        copied = await _send(LOG_CHANNEL_ID, getattr(message.bot, 'send_' + kind), LOG_CHANNEL_ID, file_id, **kwargs)
        await _send(LOG_CHANNEL_ID, message.bot.send_message, LOG_CHANNEL_ID, text, parse_mode='HTML',
                    reply_parameters=ReplyParameters(message_id=copied.message_id),
                    disable_notification=True)

    return _fire(deliver())
