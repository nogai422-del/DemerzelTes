# Запуск Telegram-бота: регистрация роутеров и фоновых задач.

import asyncio
import os

from aiogram import Dispatcher

from bot.bot_init import bot
from bot.backup_utils import run_daily

from bot.handlers.private_guard import router as private_guard_router
from bot.handlers.commands import router as commands_router
from bot.handlers.echo import router as echo_router
from bot.handlers.moderation import router as moderation_router
from bot.handlers.audit import router as chat_member_router
from bot.handlers.chat_lock import router as chat_lock_router
from bot.handlers.reactions import router as reactions_router

from bot.handlers.echo import wisdom_loop
from bot.scheduled_messages import scheduled_messages_loop
from bot.donations import donation_notifications_loop, ensure_donation_schema
from bot.settings import ensure_chat_behavior_schema
from bot.warning_state import ensure_warning_schema
from bot.message_queue import shutdown_message_queue
from bot.telethon_sync import telethon_auto_sync_loop
from bot.runtime import PollingProgress, UpdateDeadline, heartbeat_loop, supervise_background

dp = Dispatcher()


def _acquire_polling_lock():
    """Не даёт двум процессам на одном persistent volume одновременно читать getUpdates."""
    try:
        import fcntl
    except ImportError:
        return None
    data_dir = os.getenv("DATA_DIR", "database")
    os.makedirs(data_dir, exist_ok=True)
    handle = open(os.path.join(data_dir, "telegram_polling.lock"), "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return False


# Точка запуска и координации основных задач модуля.
async def main():
    polling_lock = _acquire_polling_lock()
    if polling_lock is False:
        raise RuntimeError("Telegram polling lock is held by another worker")

    await ensure_donation_schema()
    await ensure_chat_behavior_schema()
    await ensure_warning_schema()

    dp.include_router(private_guard_router)
    dp.include_router(chat_lock_router)
    dp.include_router(chat_member_router)
    dp.include_router(reactions_router)
    dp.include_router(moderation_router)
    dp.include_router(commands_router)
    dp.include_router(echo_router)

    bot.session.middleware(PollingProgress())
    dp.update.outer_middleware(UpdateDeadline())
    factories = {
        'backup': lambda: run_daily(bot),
        'wisdom': lambda: wisdom_loop(bot),
        'scheduled': lambda: scheduled_messages_loop(bot),
        'donations': lambda: donation_notifications_loop(bot),
        'telethon': telethon_auto_sync_loop,
        'heartbeat': heartbeat_loop,
    }
    background = [asyncio.create_task(supervise_background(name, factory), name=name)
                  for name, factory in factories.items()]

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["chat_member", "my_chat_member", "message", "message_reaction"],
            polling_timeout=20,
            tasks_concurrency_limit=32,
            handle_signals=False,
        )

    finally:
        for task in background:
            task.cancel()

        await asyncio.gather(
            *background, return_exceptions=True
        )
        await shutdown_message_queue()
        await bot.session.close()
        if polling_lock:
            polling_lock.close()
