"""Права Telegram для этапов получения, заполнения и сохранения анкеты."""
import time
from aiogram.types import ChatPermissions
from bot.database import db
from bot.settings import get_restrict_new_members_telegram
from bot.warning_state import FORM_STAGE_NEW, FORM_STAGE_FILLING, FORM_STAGE_SAVED, get_form_stage


def form_permissions(stage: str) -> ChatPermissions:
    saved = stage == FORM_STAGE_SAVED
    return ChatPermissions(
        can_send_messages=stage in (FORM_STAGE_FILLING, FORM_STAGE_SAVED),
        can_send_photos=saved, can_send_videos=saved, can_send_video_notes=saved,
        can_send_audios=saved, can_send_voice_notes=saved, can_send_documents=saved,
        can_send_other_messages=saved, can_add_web_page_previews=saved,
        can_send_polls=saved, can_react_to_messages=saved,
    )


async def sync_form_permissions(bot, chat_id: int, user_id: int, stage: str | None = None) -> bool:
    if not await get_restrict_new_members_telegram():
        return False
    member = await bot.get_chat_member(chat_id, user_id)
    if getattr(member.status, 'value', member.status) in ('creator', 'administrator', 'left', 'kicked'):
        return False
    async with db() as cur:
        await cur.execute('SELECT until FROM mutes WHERE chat_id=? AND user_id=?', (chat_id,user_id))
        mute = await cur.fetchone()
    if mute and int(mute[0]) > int(time.time()):
        return False
    if stage is None:
        stage = await get_form_stage(chat_id, user_id)
    await bot.restrict_chat_member(chat_id, user_id, permissions=form_permissions(stage), use_independent_chat_permissions=True)
    return True
