"""Авторизация Telethon и синхронизация каталога участников Telegram."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from bot.database import db
from bot.telethon_settings import (
    clear_auth_state,
    get_auth_state,
    get_telethon_settings,
    save_auth_state,
    save_telethon_settings,
)

_sync_lock = asyncio.Lock()
_auth_lock = asyncio.Lock()
_qr_auth = {"client": None, "qr": None, "created_at": 0.0}


async def configured() -> bool:
    settings = await get_telethon_settings()
    return bool(settings.get("enabled") and settings.get("api_id") and settings.get("api_hash") and settings.get("session"))


def _build_client(settings: dict, session_value: str | None = None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    value = str(session_value if session_value is not None else settings.get("session") or "").strip()
    return TelegramClient(StringSession(value), int(settings["api_id"]), settings["api_hash"])


async def begin_qr_authorization() -> dict:
    """Создаёт QR-ссылку для входа через официальное приложение Telegram."""
    settings = await get_telethon_settings()
    missing = [key for key in ("api_id", "api_hash") if not settings.get(key)]
    if missing:
        raise RuntimeError("Сначала сохраните API ID и API Hash")

    async with _auth_lock:
        await cancel_qr_authorization()
        client = _build_client(settings, "")
        try:
            await client.connect()
            qr = await client.qr_login()
            _qr_auth.update({"client": client, "qr": qr, "created_at": time.time()})
            await save_auth_state(status="qr_waiting", error_text="", pending_session=client.session.save())
            return {"status": "qr_waiting", "url": qr.url}
        except Exception:
            await client.disconnect()
            raise


async def refresh_qr_authorization() -> dict:
    qr = _qr_auth.get("qr")
    client = _qr_auth.get("client")
    if not qr or not client:
        return await begin_qr_authorization()
    async with _auth_lock:
        await qr.recreate()
        _qr_auth["created_at"] = time.time()
        await save_auth_state(status="qr_waiting", error_text="")
        return {"status": "qr_waiting", "url": qr.url}


async def check_qr_authorization() -> dict:
    """Проверяет, был ли QR подтверждён в Telegram."""
    async with _auth_lock:
        qr = _qr_auth.get("qr")
        client = _qr_auth.get("client")
        if not qr or not client:
            raise RuntimeError("QR-сессия не найдена. Создайте новый QR-код")
        try:
            await qr.wait(timeout=3)
            result = await _finish_authorization(client)
            _qr_auth.update({"client": None, "qr": None, "created_at": 0.0})
            return result
        except asyncio.TimeoutError:
            return {"status": "waiting"}
        except Exception as exc:
            await save_auth_state(error_text=str(exc)[:1000])
            raise


async def cancel_qr_authorization() -> None:
    client = _qr_auth.get("client")
    _qr_auth.update({"client": None, "qr": None, "created_at": 0.0})
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


def get_active_qr_url() -> str:
    qr = _qr_auth.get("qr")
    return str(getattr(qr, "url", "") or "")


async def begin_telethon_authorization(phone: str) -> dict:
    """Отправляет код на номер и сохраняет временную StringSession в общей БД."""
    settings = await get_telethon_settings()
    missing = [key for key in ("api_id", "api_hash") if not settings.get(key)]
    if missing:
        raise RuntimeError("Сначала сохраните API ID и API Hash")
    phone = str(phone or "").strip().replace(" ", "")
    if not phone.startswith("+") or len(phone) < 8:
        raise RuntimeError("Укажите номер телефона в международном формате, например +491234567890")

    async with _auth_lock:
        client = _build_client(settings, "")
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            pending = client.session.save()
            delivery_type = type(getattr(sent, "type", None)).__name__.replace("SentCodeType", "").lower()
            next_delivery_type = type(getattr(sent, "next_type", None)).__name__.replace("CodeType", "").lower() if getattr(sent, "next_type", None) else ""
            await save_auth_state(
                status="code_sent",
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                pending_session=pending,
                error_text="",
                delivery_type=delivery_type,
                next_delivery_type=next_delivery_type,
                timeout_seconds=str(getattr(sent, "timeout", 0) or 0),
            )
            await save_telethon_settings({"phone": phone})
            return {"status": "code_sent", "phone": phone}
        except Exception as exc:
            await save_auth_state(status="error", error_text=str(exc)[:1000])
            raise
        finally:
            await client.disconnect()


async def confirm_telethon_code(code: str) -> dict:
    """Подтверждает код; при включённой 2FA переводит процесс на ввод пароля."""
    from telethon.errors import SessionPasswordNeededError

    settings = await get_telethon_settings()
    state = await get_auth_state()
    if state.get("status") != "code_sent":
        raise RuntimeError("Сначала запросите новый код подтверждения")
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not code:
        raise RuntimeError("Введите код подтверждения Telegram")

    async with _auth_lock:
        client = _build_client(settings, state.get("pending_session", ""))
        try:
            await client.connect()
            try:
                await client.sign_in(
                    phone=state.get("phone", ""),
                    code=code,
                    phone_code_hash=state.get("phone_code_hash", ""),
                )
            except SessionPasswordNeededError:
                await save_auth_state(
                    status="password_needed",
                    pending_session=client.session.save(),
                    error_text="",
                )
                return {"status": "password_needed"}
            return await _finish_authorization(client)
        except Exception as exc:
            await save_auth_state(error_text=str(exc)[:1000])
            raise
        finally:
            await client.disconnect()


async def confirm_telethon_password(password: str) -> dict:
    settings = await get_telethon_settings()
    state = await get_auth_state()
    if state.get("status") != "password_needed":
        raise RuntimeError("Пароль двухэтапной аутентификации сейчас не запрошен")
    if not password:
        raise RuntimeError("Введите пароль двухэтапной аутентификации")

    async with _auth_lock:
        client = _build_client(settings, state.get("pending_session", ""))
        try:
            await client.connect()
            await client.sign_in(password=password)
            return await _finish_authorization(client)
        except Exception as exc:
            await save_auth_state(error_text=str(exc)[:1000])
            raise
        finally:
            await client.disconnect()


async def _finish_authorization(client) -> dict:
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не подтвердил авторизацию сессии")
    me = await client.get_me()
    name = " ".join(x for x in (me.first_name, me.last_name) if x).strip()
    await save_telethon_settings({
        "session": client.session.save(),
        "session_type": "string",
        "phone": getattr(me, "phone", "") or "",
        "authorized_user_id": str(me.id),
        "authorized_username": me.username or "",
        "authorized_name": name,
        "enabled": "1",
    })
    await clear_auth_state()
    return {"status": "authorized", "user_id": int(me.id), "username": me.username or "", "name": name}


async def disconnect_telethon_session() -> None:
    await save_telethon_settings({
        "enabled": "0", "auto_sync_enabled": "0", "session": "",
        "authorized_user_id": "", "authorized_username": "", "authorized_name": "",
    })
    await clear_auth_state()


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
        raise RuntimeError("Сессия ещё не авторизована через административную панель")
    client = _build_client(settings)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия Telethon больше не авторизована. Выполните подключение заново.")
        me = await client.get_me()
        return {"authorized": True, "user_id": int(me.id), "username": me.username or "", "name": " ".join(x for x in (me.first_name, me.last_name) if x).strip()}
    finally:
        await client.disconnect()


async def sync_chat_members(chat_id: int | None = None) -> dict:
    settings = await get_telethon_settings()
    if not settings.get("enabled"):
        raise RuntimeError("Telethon выключен в административной панели")
    if not all(settings.get(k) for k in ("api_id", "api_hash", "session")):
        raise RuntimeError('Telethon ещё не авторизован через административную панель')
    if not chat_id:
        try: chat_id = int(str(settings.get("default_chat_id") or "").strip())
        except ValueError: chat_id = 0
    if not chat_id: raise RuntimeError("Не выбран Telegram-чат")

    from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth
    async with _sync_lock:
        await ensure_sync_schema(); started=int(time.time())
        async with db() as cur:
            await cur.execute('''INSERT INTO member_sync_state(chat_id,last_started_at,status,error_text)
                VALUES(?,?,'running','') ON CONFLICT(chat_id) DO UPDATE SET
                last_started_at=excluded.last_started_at,status='running',error_text='' ''',(int(chat_id),started))
        client=_build_client(settings)
        try:
            await client.connect()
            if not await client.is_user_authorized(): raise RuntimeError("Сессия Telethon не авторизована")
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
                    await cur.execute('SELECT username,name,telegram_status FROM admin_members WHERE user_id=?',(uid,)); old=await cur.fetchone()
                    if old is None: new+=1
                    elif (old[0],old[1],old[2]) != (username,name,status): updated+=1
                    await cur.execute('''INSERT INTO admin_members(user_id,username,name,telegram_status,telegram_last_seen_at,telegram_status_checked_at,imported_at,is_chat_member,last_member_sync_at)
                    VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,name=excluded.name,
                    telegram_status=excluded.telegram_status,telegram_last_seen_at=excluded.telegram_last_seen_at,
                    telegram_status_checked_at=excluded.telegram_status_checked_at,is_chat_member=1,last_member_sync_at=excluded.last_member_sync_at''',
                    (uid,username,name,status,last_seen,datetime.now().isoformat(timespec='seconds'),now,now))
                await cur.execute('SELECT user_id FROM admin_members WHERE is_chat_member=1'); known={int(r[0]) for r in await cur.fetchall()}; missing=known-ids
                if missing: await cur.executemany('UPDATE admin_members SET is_chat_member=0,last_member_sync_at=? WHERE user_id=?',[(now,uid) for uid in missing])
                await cur.execute('''UPDATE member_sync_state SET last_finished_at=?,status='ok',found_count=?,new_count=?,updated_count=?,missing_count=?,error_text='' WHERE chat_id=?''',(now,len(members),new,updated,len(missing),int(chat_id)))
            return {'found':len(members),'new':new,'updated':updated,'missing':len(missing),'finished_at':now}
        except Exception as exc:
            async with db() as cur: await cur.execute("UPDATE member_sync_state SET last_finished_at=?,status='error',error_text=? WHERE chat_id=?",(int(time.time()),str(exc)[:1000],int(chat_id)))
            raise
        finally: await client.disconnect()


async def telethon_auto_sync_loop() -> None:
    while True:
        try:
            settings = await get_telethon_settings(); interval = max(15, int(settings.get("sync_interval_minutes") or 360))
            if settings.get("enabled") and settings.get("auto_sync_enabled"):
                chat_id = int(str(settings.get("default_chat_id") or "0"))
                if chat_id:
                    try: await sync_chat_members(chat_id)
                    except Exception as exc: print(f"Автосинхронизация Telethon завершилась ошибкой: {exc}")
            await asyncio.sleep(interval * 60)
        except asyncio.CancelledError: raise
        except Exception as exc:
            print(f"Ошибка цикла Telethon: {exc}"); await asyncio.sleep(900)
