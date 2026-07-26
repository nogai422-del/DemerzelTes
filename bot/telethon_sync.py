"""Синхронизация каталога участников с Telegram через Telethon."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from bot.database import db
from bot.telethon_settings import get_telethon_settings

_sync_lock = asyncio.Lock()


async def configured() -> bool:
    settings = await get_telethon_settings()
    return bool(settings.get("enabled") and settings.get("api_id") and settings.get("api_hash") and settings.get("session"))


def _build_client(settings: dict):
    from telethon import TelegramClient
    session_value = str(settings.get("session") or "").strip()
    if settings.get("session_type") == "string":
        from telethon.sessions import StringSession
        session_value = StringSession(session_value)
    return TelegramClient(session_value, int(settings["api_id"]), settings["api_hash"])


async def ensure_sync_schema() -> None:
    async with db() as cur:
        await cur.execute('''CREATE TABLE IF NOT EXISTS member_sync_state (
            chat_id INTEGER PRIMARY KEY, last_started_at INTEGER NOT NULL DEFAULT 0,
            last_finished_at INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT '',
            found_count INTEGER NOT NULL DEFAULT 0, new_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0, missing_count INTEGER NOT NULL DEFAULT 0,
            error_text TEXT NOT NULL DEFAULT ''
        )''')
        await cur.execute("PRAGMA table_info(admin_members)")
        cols={str(r[1]) for r in await cur.fetchall()}
        for name, ddl in {
            'is_chat_member':'INTEGER NOT NULL DEFAULT 1',
            'last_member_sync_at':'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if name not in cols:
                await cur.execute(f'ALTER TABLE admin_members ADD COLUMN {name} {ddl}')


async def get_sync_state(chat_id: int) -> dict:
    await ensure_sync_schema()
    async with db() as cur:
        await cur.execute('SELECT * FROM member_sync_state WHERE chat_id=?',(int(chat_id),))
        row=await cur.fetchone()
    return dict(row) if row else {}


async def test_telethon_connection() -> dict:
    settings = await get_telethon_settings()
    missing = [key for key in ("api_id", "api_hash", "session") if not settings.get(key)]
    if missing:
        raise RuntimeError("Не заполнены обязательные поля: " + ", ".join(missing))
    client = _build_client(settings)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия Telethon не авторизована. Сначала создайте авторизованную session-файл или StringSession.")
        me = await client.get_me()
        return {
            "authorized": True,
            "user_id": int(me.id),
            "username": me.username or "",
            "name": " ".join(x for x in (me.first_name, me.last_name) if x).strip(),
        }
    finally:
        await client.disconnect()


async def sync_chat_members(chat_id: int | None = None) -> dict:
    settings = await get_telethon_settings()
    if not settings.get("enabled"):
        raise RuntimeError("Telethon выключен в административной панели")
    if not all(settings.get(k) for k in ("api_id", "api_hash", "session")):
        raise RuntimeError('Не заданы API ID, API Hash и сессия Telethon')
    if not chat_id:
        try:
            chat_id = int(str(settings.get("default_chat_id") or "").strip())
        except ValueError:
            chat_id = 0
    if not chat_id:
        raise RuntimeError("Не выбран Telegram-чат")

    from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth

    async with _sync_lock:
        await ensure_sync_schema()
        started=int(time.time())
        async with db() as cur:
            await cur.execute('''INSERT INTO member_sync_state(chat_id,last_started_at,status,error_text)
                VALUES(?,?,'running','') ON CONFLICT(chat_id) DO UPDATE SET
                last_started_at=excluded.last_started_at,status='running',error_text='' ''',(int(chat_id),started))
        client=_build_client(settings)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Сессия Telethon не авторизована")
            members=[]
            async for user in client.iter_participants(int(chat_id)):
                status='unknown'; last_seen=''
                if isinstance(user.status, UserStatusOnline): status='online'
                elif isinstance(user.status, UserStatusOffline):
                    status='offline'; last_seen=user.status.was_online.astimezone(timezone.utc).isoformat() if user.status.was_online else ''
                elif isinstance(user.status, UserStatusRecently): status='recently'
                elif isinstance(user.status, UserStatusLastWeek): status='last_week'
                elif isinstance(user.status, UserStatusLastMonth): status='last_month'
                name=' '.join(x for x in (user.first_name,user.last_name) if x).strip()
                members.append((int(user.id), user.username or '', name, status, last_seen))
            now=int(time.time()); ids={m[0] for m in members}; new=updated=0
            async with db() as cur:
                for uid,username,name,status,last_seen in members:
                    await cur.execute('SELECT username,name,telegram_status FROM admin_members WHERE user_id=?',(uid,))
                    old=await cur.fetchone()
                    if old is None: new+=1
                    elif (old[0],old[1],old[2]) != (username,name,status): updated+=1
                    await cur.execute('''INSERT INTO admin_members(user_id,username,name,telegram_status,telegram_last_seen_at,telegram_status_checked_at,imported_at,is_chat_member,last_member_sync_at)
                    VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,name=excluded.name,
                    telegram_status=excluded.telegram_status,telegram_last_seen_at=excluded.telegram_last_seen_at,
                    telegram_status_checked_at=excluded.telegram_status_checked_at,is_chat_member=1,last_member_sync_at=excluded.last_member_sync_at''',
                    (uid,username,name,status,last_seen,datetime.now().isoformat(timespec='seconds'),now,now))
                await cur.execute('SELECT user_id FROM admin_members WHERE is_chat_member=1')
                known={int(r[0]) for r in await cur.fetchall()}; missing=known-ids
                if missing:
                    await cur.executemany('UPDATE admin_members SET is_chat_member=0,last_member_sync_at=? WHERE user_id=?',[(now,uid) for uid in missing])
                await cur.execute('''UPDATE member_sync_state SET last_finished_at=?,status='ok',found_count=?,new_count=?,updated_count=?,missing_count=?,error_text='' WHERE chat_id=?''',
                    (now,len(members),new,updated,len(missing),int(chat_id)))
            return {'found':len(members),'new':new,'updated':updated,'missing':len(missing),'finished_at':now}
        except Exception as exc:
            async with db() as cur:
                await cur.execute("UPDATE member_sync_state SET last_finished_at=?,status='error',error_text=? WHERE chat_id=?",(int(time.time()),str(exc)[:1000],int(chat_id)))
            raise
        finally:
            await client.disconnect()


async def telethon_auto_sync_loop() -> None:
    """Периодически синхронизирует основной чат согласно настройкам панели."""
    while True:
        try:
            settings = await get_telethon_settings()
            interval = max(15, int(settings.get("sync_interval_minutes") or 360))
            if settings.get("enabled") and settings.get("auto_sync_enabled"):
                chat_id = int(str(settings.get("default_chat_id") or "0"))
                if chat_id:
                    try:
                        await sync_chat_members(chat_id)
                    except Exception as exc:
                        print(f"Автосинхронизация Telethon завершилась ошибкой: {exc}")
            await asyncio.sleep(interval * 60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Ошибка цикла Telethon: {exc}")
            await asyncio.sleep(900)
