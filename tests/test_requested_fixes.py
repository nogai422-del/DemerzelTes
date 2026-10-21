"""Regression checks for the moderation and donation requests of 21 October."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

DATA = tempfile.TemporaryDirectory()
os.environ.update(DATA_DIR=DATA.name, BOT_TOKEN='123456:TEST_TOKEN', COSMOS_ID='999',
                  SOURCE_CHAT_ID='-1001', BACKUP_CHANNEL_ID='-1002', LOG_CHANNEL_ID='-1003')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiogram.enums import ChatMemberStatus
from bot.database import db
from bot import donations, onboarding
from bot.handlers import commands, moderation, reactions
from bot.settings import set_restrict_new_members_telegram

CHAT = -1001
NOW = 2_000_000_000


def user(user_id):
    return NS(id=user_id,first_name='Test',last_name='',username=None,is_bot=False)


class RequestedFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await donations.ensure_donation_schema()
        await moderation.ensure_warning_schema()
        async with db() as cur:
            for table in ('chat_users','donation_grants','donation_usage','permission_messages','mutes','bv_messages','form_text'):
                await cur.execute(f'DELETE FROM {table}')
        await set_restrict_new_members_telegram(True)

    async def test_pre_bv_messages_are_blocked(self):
        message = NS(chat=NS(id=CHAT), from_user=user(44), content_type='text', bot=NS())
        with patch.object(moderation, 'get_permission_level', AsyncMock(return_value=0)), patch.object(moderation, 'is_command_admin', AsyncMock(return_value=False)), patch.object(moderation, 'safe_delete', AsyncMock()) as delete, patch.object(moderation, 'sync_form_permissions', AsyncMock()) as sync, patch.object(moderation, 'send_restriction_warning', AsyncMock()), patch.object(moderation, 'moderation_handle_text', AsyncMock()) as text:
            self.assertTrue(await moderation.moderation_handle_message(message))
            delete.assert_awaited_once_with(message)
            sync.assert_awaited_once_with(message.bot, CHAT, 44, 'new')
            text.assert_not_awaited()

    async def test_exact_duration_for_all_categories(self):
        with patch.object(donations.time,'time',return_value=NOW):
            for category in donations.CATEGORY_TITLES:
                await donations.record_donation_grant(CHAT,10,category,NOW+6*86400,6)
                expiry, days = await donations.set_donation_grant_duration(CHAT,10,category,28)
                self.assertEqual((expiry,days),(NOW+28*86400,28))
                async with db() as cur:
                    await cur.execute('SELECT valid_until,package_days FROM donation_grants WHERE category=?',(category,))
                    self.assertEqual(tuple(await cur.fetchone()),(expiry,28))

    async def test_daily_counter_under_concurrency(self):
        with patch.object(donations.time,'time',return_value=NOW):
            await donations.set_donation_grant_duration(CHAT,10,'emoji',28,5)
            results = await asyncio.gather(*(donations.check_and_update_usage_limit(CHAT,10,'emoji') for _ in range(20)))
            self.assertEqual(sum(r[0] for r in results),5)
            self.assertEqual(max(r[2] for r in results),5)

    async def test_two_day_and_expired_notifications_once(self):
        await donations.set_expiry_notification_settings(enabled=True,min_package_days=1,notice_days=2)
        with patch.object(donations.time,'time',return_value=NOW):
            for category in donations.CATEGORY_TITLES:
                await donations.record_donation_grant(CHAT,11,category,NOW+2*86400,28)
                await donations.record_donation_grant(CHAT,12,category,NOW-1,28)
                await donations.record_donation_grant(CHAT,13,category,NOW+2*86400+1,28)
            with patch.object(donations,'_send_notification',AsyncMock()) as send:
                await donations.process_due_donation_notifications(NS())
                self.assertEqual(send.await_count,8)
                self.assertEqual({call.args[2] for call in send.call_args_list},{'expired','preexpiry'})
                await donations.process_due_donation_notifications(NS())
                self.assertEqual(send.await_count,8)

    async def test_reaction_never_replies_to_or_deletes_victims_message(self):
        async with db() as cur:
            await commands._save_filled_form(cur,CHAT,20,'Verified')
        event=NS(user=user(20),chat=NS(id=CHAT),message_id=555,new_reaction=[NS()])
        bot=NS(delete_message_reaction=AsyncMock(),delete_message=AsyncMock())
        with patch.object(reactions,'_warning_allowed',return_value=True), patch.object(donations,'_get_user_name',AsyncMock(return_value='Test')), patch.object(donations,'send_notification_card',AsyncMock(return_value=NS(message_id=900))) as card:
            await reactions.enforce_reaction_media_level(event,bot)
            bot.delete_message_reaction.assert_awaited_once_with(chat_id=CHAT,message_id=555,user_id=20)
            bot.delete_message.assert_not_awaited()
            self.assertIsNone(card.call_args.kwargs['reply_parameters'])

    async def test_permissions_for_all_stages_and_mutes(self):
        bot=NS(get_chat_member=AsyncMock(return_value=NS(status=ChatMemberStatus.MEMBER)),restrict_chat_member=AsyncMock())
        for stage, text, media in [('new',False,False),('filling',True,False),('saved',True,True)]:
            await onboarding.sync_form_permissions(bot,CHAT,30,stage)
            permissions=bot.restrict_chat_member.call_args.kwargs['permissions']
            self.assertEqual(permissions.can_send_messages,text)
            self.assertEqual(permissions.can_send_photos,media)
            self.assertEqual(permissions.can_react_to_messages,media)
        bot.restrict_chat_member.reset_mock()
        async with db() as cur:
            await cur.execute('INSERT INTO mutes VALUES(?,?,?)',(CHAT,30,NOW+100000000))
        await onboarding.sync_form_permissions(bot,CHAT,30,'saved')
        bot.restrict_chat_member.assert_not_awaited()
        bot.get_chat_member.return_value=NS(status=ChatMemberStatus.ADMINISTRATOR)
        await onboarding.sync_form_permissions(bot,CHAT,31,'new')
        bot.restrict_chat_member.assert_not_awaited()

    async def test_bv_by_id_opens_only_text_and_keeps_saved_users_unlocked(self):
        target=user(40)
        bot=NS(get_chat_member=AsyncMock(return_value=NS(status=ChatMemberStatus.MEMBER,user=target)),restrict_chat_member=AsyncMock(),delete_message=AsyncMock())
        message=NS(chat=NS(id=CHAT),from_user=user(10),reply_to_message=None,text='/bv 40',bot=bot)
        with patch.object(commands,'safe_delete',AsyncMock()), patch.object(commands,'is_user_muted',AsyncMock(return_value=False)), patch.object(commands,'clear_warning',AsyncMock()), patch.object(commands,'bot_answer',AsyncMock(return_value=NS(message_id=100))):
            await commands.handle_bv_command(message)
            perms=bot.restrict_chat_member.call_args.kwargs['permissions']
            self.assertTrue(perms.can_send_messages)
            self.assertFalse(perms.can_react_to_messages)
            async with db() as cur:
                await commands._save_filled_form(cur,CHAT,40,'Approved')
            await commands.handle_bv_command(message)
            self.assertTrue(bot.restrict_chat_member.call_args.kwargs['permissions'].can_react_to_messages)

    async def test_ban_and_kick_not_scheduled_for_deletion(self):
        bot=NS(get_chat_administrators=AsyncMock(return_value=[]),ban_chat_member=AsyncMock(),unban_chat_member=AsyncMock())
        message=NS(chat=NS(id=CHAT),from_user=user(10),reply_to_message=NS(from_user=user(50)))
        with patch.object(commands,'safe_delete',AsyncMock()), patch.object(commands,'is_user_muted',AsyncMock(return_value=False)), patch.object(commands,'is_command_admin',AsyncMock(return_value=True)), patch.object(commands,'bot_answer',AsyncMock(return_value=NS(message_id=900))), patch.object(commands,'save_timed_message',AsyncMock()) as timer:
            await commands.handle_ban_command(message,bot)
            await commands.handle_kick_command(message,bot)
            timer.assert_not_awaited()

    async def test_deleted_emoji_message_is_logged(self):
        message=NS(chat=NS(id=CHAT,title='Test group'),from_user=user(10),text='Text 😀',caption=None,bot=NS())
        with patch.object(moderation,'safe_delete',AsyncMock()), patch.object(moderation,'is_user_muted',AsyncMock(return_value=False)), patch.object(moderation,'detect_badword_details',AsyncMock(return_value=None)), patch.object(moderation,'message_has_link',AsyncMock(return_value=False)), patch.object(moderation,'has_emoji_permission',AsyncMock(return_value=False)), patch.object(moderation,'send_restriction_warning',AsyncMock()), patch.object(moderation,'bot_send_message',AsyncMock()) as log:
            self.assertTrue(await moderation.moderation_handle_text(message,2))
            self.assertIn('#emoji_deleted',log.call_args.args[2])
            self.assertIn('Text 😀',log.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
