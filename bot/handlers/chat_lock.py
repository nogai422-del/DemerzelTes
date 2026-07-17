# Логика блокировки чата: /lock, /unlock и ранняя остановка message-пайплайна.

from aiogram.filters import Command
from aiogram.types import Message
from aiogram import Router

from bot.utils import safe_delete, is_command_admin
from bot.database import db
from bot.message_queue import bot_answer
from env_config import require_int_env

router = Router()
COSMOS_ID = require_int_env("COSMOS_ID")


# Команда /lock: включает блокировку обычных сообщений в текущем чате.
@router.message(Command("lock"))
async def cmd_lock(message: Message):
    try:
        if not await is_command_admin(
            message.bot, message.chat.id, message.from_user.id, owner_id=COSMOS_ID
        ):
            return

        async with db() as cur:
            await cur.execute("""
                INSERT INTO chat_lock (chat_id, locked)
                VALUES (?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET locked = 1
            """, (message.chat.id,))

        await bot_answer(message, "Чат заблокирован", wait=True)
    except Exception as e:
        print(f"Ошибка в /lock: {e}")
    finally:
        await safe_delete(message)


# Команда /unlock: отключает блокировку сообщений в текущем чате.
@router.message(Command("unlock"))
async def cmd_unlock(message: Message):
    try:
        if not await is_command_admin(
            message.bot, message.chat.id, message.from_user.id, owner_id=COSMOS_ID
        ):
            return

        async with db() as cur:
            await cur.execute("""
                INSERT INTO chat_lock (chat_id, locked)
                VALUES (?, 0)
                ON CONFLICT(chat_id) DO UPDATE SET locked = 0
            """, (message.chat.id,))

        await bot_answer(message, "Чат разблокирован", wait=True)
    except Exception as e:
        print(f"Ошибка в /unlock: {e}")
    finally:
        await safe_delete(message)


# Проверяет состояние lock и удаляет сообщение, если чат сейчас заблокирован.
async def handle_chat_lock(message: Message) -> bool:
    """
    True  -> сообщение удалено (или попытались удалить)
    False -> ничего не делали (нет лока/админ/не группа/нельзя удалить)
    """
    if not message.from_user:
        return False

    chat_id = message.chat.id

    async with db() as cur:
        await cur.execute(
            "SELECT locked FROM chat_lock WHERE chat_id=?",
            (chat_id,)
        )
        row = await cur.fetchone()

    if not row or not row["locked"]:
        return False

    if await is_command_admin(
        message.bot, chat_id, message.from_user.id, owner_id=COSMOS_ID
    ):
        return False

    await safe_delete(message)
    return True
