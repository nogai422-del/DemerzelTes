# Очередь отправки сообщений: анти-флуд и последовательность по chat_id.

import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

from aiogram.types import Message
from aiogram.exceptions import TelegramRetryAfter

_locks = defaultdict(asyncio.Lock)


# Проверяет, что chat_id относится к группе/супергруппе.
def _is_group_chat_id(chat_id: int) -> bool:
    return int(chat_id) < 0


# Отправляет запрос в Telegram с последовательностью по чату и ретраем после FloodWait.
async def _send(
    chat_id: int,
    method: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Reactive send:
    - один чат -> строго по очереди
    - если Telegram говорит FloodWait (429) -> ждём retry_after и пробуем снова (бесконечно)
    """
    async with _locks[chat_id]:
        while True:
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as e:
                print(f"[FLOODWAIT] chat_id={chat_id} wait={e.retry_after}s")
                await asyncio.sleep(e.retry_after)


# Запускает фоновую задачу отправки с обработкой исключений.
def _fire(coro: Awaitable[Any]) -> asyncio.Task:

    # Оборачивает фоновую корутину и перехватывает ошибки отправки.
    async def _wrap() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            return
        except Exception as e:
            print("Ошибка отправки в message_queue:", e)

    return asyncio.create_task(_wrap())


# Отправляет ответ в текущий чат через очередь отправки.
async def bot_answer(message: Message, text: str, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not message.chat:
        raise ValueError("Message.chat is None")
    if not _is_group_chat_id(message.chat.id):
        return None

    coro = _send(message.chat.id, message.answer, text, **kwargs)
    if wait:
        return await coro
    _fire(coro)
    return None


# Отправляет reply в текущий чат через очередь отправки.
async def bot_reply(message: Message, text: str, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not message.chat:
        raise ValueError("Message.chat is None")
    if not _is_group_chat_id(message.chat.id):
        return None

    coro = _send(message.chat.id, message.reply, text, **kwargs)
    if wait:
        return await coro
    _fire(coro)
    return None


# Отправляет фото в чат через очередь отправки.
async def bot_send_photo(message: Message, photo, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not message.chat:
        raise ValueError("Message.chat is None")
    if not _is_group_chat_id(message.chat.id):
        return None

    chat_id = message.chat.id
    bot = message.bot

    coro = _send(chat_id, bot.send_photo, chat_id, photo, **kwargs)
    if wait:
        return await coro
    _fire(coro)
    return None


# Отправляет фото в указанный чат через очередь отправки.
async def bot_send_photo_to_chat(bot, chat_id: int, photo, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not _is_group_chat_id(chat_id):
        return None

    coro = _send(chat_id, bot.send_photo, chat_id, photo, **kwargs)
    if wait:
        return await coro
    _fire(coro)
    return None


# Отправляет текст в указанный чат через очередь отправки.
async def bot_send_message(bot, chat_id: int, text: str, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not _is_group_chat_id(chat_id):
        return None

    coro = _send(chat_id, bot.send_message, chat_id, text, **kwargs)

    if wait:
        return await coro

    _fire(coro)
    return None


# Отправляет документ в указанный чат через очередь отправки.
async def bot_send_document(bot, chat_id: int, document, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not _is_group_chat_id(chat_id):
        return None

    coro = _send(chat_id, bot.send_document, chat_id, document, **kwargs)
    if wait:
        return await coro
    _fire(coro)
    return None
