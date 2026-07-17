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
        print("Telegram polling уже запущен другим процессом; этот экземпляр обслуживает только web")
        await asyncio.Event().wait()
        return

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

    daily_task = asyncio.create_task(run_daily(bot))
    wisdom_task = asyncio.create_task(wisdom_loop(bot))
    scheduled_task = asyncio.create_task(scheduled_messages_loop(bot))
    donation_task = asyncio.create_task(donation_notifications_loop(bot))

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["chat_member", "my_chat_member", "message", "message_reaction"],
        )

    finally:
        for task in (daily_task, wisdom_task, scheduled_task, donation_task):
            task.cancel()

        await asyncio.gather(
            daily_task, wisdom_task, scheduled_task, donation_task, return_exceptions=True
        )
