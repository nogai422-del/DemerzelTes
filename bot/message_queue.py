# Очередь отправки сообщений: анти-флуд и последовательность по chat_id.

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Optional

from aiogram.types import Message
from aiogram.exceptions import TelegramRetryAfter

_locks: dict[int, asyncio.Lock] = {}
_pending_tasks: set[asyncio.Task] = set()
_queued: dict[int, int] = {}
SEND_TIMEOUT_SECONDS = 90
MAX_QUEUED_PER_CHAT = 64
MAX_BACKGROUND_SENDS = 256
MAX_FLOOD_RETRIES = 2


def _is_group_chat_id(chat_id: int) -> bool:
    return int(chat_id) < 0


def _get_lock(chat_id: int) -> asyncio.Lock:
    chat_id = int(chat_id)
    lock = _locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[chat_id] = lock
    return lock


async def _send(chat_id: int, method: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    chat_id = int(chat_id)
    if _queued.get(chat_id, 0) >= MAX_QUEUED_PER_CHAT:
        raise RuntimeError(f'Send queue full for chat_id={chat_id}')
    _queued[chat_id] = _queued.get(chat_id, 0)+1
    lock = _get_lock(chat_id)
    async def deliver():
        async with lock:
            for attempt in range(MAX_FLOOD_RETRIES+1):
                try:
                    return await method(*args, **kwargs)
                except TelegramRetryAfter as exc:
                    print(f"[FLOODWAIT] chat_id={chat_id} wait={exc.retry_after}s")
                    if attempt == MAX_FLOOD_RETRIES or exc.retry_after > 30:
                        raise
                    await asyncio.sleep(exc.retry_after)
    try:
        # The deadline includes time spent waiting behind another send.
        return await asyncio.wait_for(deliver(), timeout=SEND_TIMEOUT_SECONDS)
    finally:
        _queued[chat_id] -= 1
        if not _queued[chat_id]:
            _queued.pop(chat_id, None)
        # Не сохраняем блокировки для чатов навсегда.
        if not lock.locked() and not getattr(lock, "_waiters", None):
            _locks.pop(chat_id, None)


def _fire(coro: Awaitable[Any]) -> asyncio.Task | None:
    if len(_pending_tasks) >= MAX_BACKGROUND_SENDS:
        if inspect.iscoroutine(coro):
            coro.close()
        print('[SEND_OVERLOAD] background send limit reached')
        return None
    async def _wrap() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("Ошибка отправки в message_queue:", exc)

    task = asyncio.create_task(_wrap())
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task


async def shutdown_message_queue() -> None:
    """Отменяет оставшиеся фоновые отправки при остановке приложения."""
    tasks = tuple(_pending_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending_tasks.clear()
    _locks.clear()


async def bot_answer(message: Message, text: str, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not message.chat:
        raise ValueError("Message.chat is None")
    if not _is_group_chat_id(message.chat.id):
        return None
    coro = _send(message.chat.id, message.answer, text, **kwargs)
    return await coro if wait else (_fire(coro) and None)


async def bot_reply(message: Message, text: str, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not message.chat:
        raise ValueError("Message.chat is None")
    if not _is_group_chat_id(message.chat.id):
        return None
    coro = _send(message.chat.id, message.reply, text, **kwargs)
    return await coro if wait else (_fire(coro) and None)


async def bot_send_photo(message: Message, photo, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not message.chat:
        raise ValueError("Message.chat is None")
    if not _is_group_chat_id(message.chat.id):
        return None
    coro = _send(message.chat.id, message.bot.send_photo, message.chat.id, photo, **kwargs)
    return await coro if wait else (_fire(coro) and None)


async def bot_send_photo_to_chat(bot, chat_id: int, photo, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not _is_group_chat_id(chat_id):
        return None
    coro = _send(chat_id, bot.send_photo, chat_id, photo, **kwargs)
    return await coro if wait else (_fire(coro) and None)


async def bot_send_message(bot, chat_id: int, text: str, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not _is_group_chat_id(chat_id):
        return None
    coro = _send(chat_id, bot.send_message, chat_id, text, **kwargs)
    return await coro if wait else (_fire(coro) and None)


async def bot_send_document(bot, chat_id: int, document, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not _is_group_chat_id(chat_id):
        return None
    coro = _send(chat_id, bot.send_document, chat_id, document, **kwargs)
    return await coro if wait else (_fire(coro) and None)


async def bot_send_animation_to_chat(bot, chat_id: int, animation, *, wait: bool = False, **kwargs) -> Optional[Any]:
    if not _is_group_chat_id(chat_id):
        return None
    coro = _send(chat_id, bot.send_animation, chat_id, animation, **kwargs)
    return await coro if wait else (_fire(coro) and None)
