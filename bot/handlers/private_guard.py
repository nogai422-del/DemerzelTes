# Блокирует обработку апдейтов из личных чатов.

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import ChatMemberUpdated, Message

router = Router()


# Игнорирует сообщения из личных чатов.
@router.message(F.chat.type == ChatType.PRIVATE)
async def ignore_private_messages(_message: Message):
    return


# Игнорирует chat_member-события из личных чатов.
@router.chat_member(F.chat.type == ChatType.PRIVATE)
async def ignore_private_chat_member(_event: ChatMemberUpdated):
    return


# Игнорирует my_chat_member-события из личных чатов.
@router.my_chat_member(F.chat.type == ChatType.PRIVATE)
async def ignore_private_my_chat_member(_event: ChatMemberUpdated):
    return
