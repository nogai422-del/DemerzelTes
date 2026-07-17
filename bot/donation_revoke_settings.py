"""Настройки и аудит административного снятия донатов."""

import json
import time
from bot.database import db

COMMANDS = {
    "emoji": {"command": "delem", "title": "Эмодзи", "aliases": "delemoji"},
    "voice": {"command": "delgs", "title": "Голосовые", "aliases": "delvoice"},
    "video_note": {"command": "delcircle", "title": "Кружки", "aliases": "delcircles,delvideo_note"},
    "tag": {"command": "deltag", "title": "Тег", "aliases": ""},
    "media": {"command": "delm", "title": "Медиа", "aliases": "delmedia"},
    "media2": {"command": "delm2", "title": "Медиа 2", "aliases": "delmedia2"},
    "all": {"command": "delmax", "title": "Все донаты", "aliases": ""},
}

DEFAULT_SUCCESS = "У пользователя <b>{user}</b> снят донат «<b>{donation}</b>»."
DEFAULT_MISSING = "У пользователя <b>{user}</b> донат «<b>{donation}</b>» не был активен."
DEFAULT_ERROR = "Не удалось снять донат. Повторите попытку или проверьте журнал панели."

_READY = False

async def ensure_donation_revoke_schema():
    global _READY
    if _READY:
        return
    async with db() as cur:
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS donation_revoke_global_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                access_mode TEXT NOT NULL DEFAULT 'admins',
                allowed_user_ids TEXT NOT NULL DEFAULT '',
                allow_reply INTEGER NOT NULL DEFAULT 1,
                allow_user_id INTEGER NOT NULL DEFAULT 1,
                notify_target INTEGER NOT NULL DEFAULT 0,
                notify_chat INTEGER NOT NULL DEFAULT 1,
                require_delmax_confirmation INTEGER NOT NULL DEFAULT 1,
                media2_behavior TEXT NOT NULL DEFAULT 'downgrade',
                success_template TEXT NOT NULL DEFAULT '',
                missing_template TEXT NOT NULL DEFAULT '',
                error_template TEXT NOT NULL DEFAULT ''
            )
        """)
        await cur.execute("""
            INSERT OR IGNORE INTO donation_revoke_global_settings
            (id, success_template, missing_template, error_template)
            VALUES (1, ?, ?, ?)
        """, (DEFAULT_SUCCESS, DEFAULT_MISSING, DEFAULT_ERROR))
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS donation_revoke_commands (
                command_key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                command_name TEXT NOT NULL,
                aliases TEXT NOT NULL DEFAULT ''
            )
        """)
        for key, item in COMMANDS.items():
            await cur.execute("""
                INSERT OR IGNORE INTO donation_revoke_commands
                (command_key, enabled, command_name, aliases) VALUES (?,1,?,?)
            """, (key, item['command'], item['aliases']))
        await cur.execute("""
            CREATE TABLE IF NOT EXISTS donation_revoke_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                admin_id INTEGER,
                admin_name TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 1
            )
        """)
    _READY = True

async def get_revoke_settings():
    await ensure_donation_revoke_schema()
    async with db() as cur:
        await cur.execute("SELECT * FROM donation_revoke_global_settings WHERE id=1")
        global_row = dict(await cur.fetchone())
        await cur.execute("SELECT * FROM donation_revoke_commands")
        commands = {r['command_key']: dict(r) for r in await cur.fetchall()}
    global_row['commands'] = commands
    global_row['allowed_ids'] = {
        int(x.strip()) for x in str(global_row.get('allowed_user_ids','')).replace(';',',').split(',')
        if x.strip().lstrip('-').isdigit()
    }
    return global_row

async def log_revoke_action(*, source, admin_id, admin_name, chat_id, target_id, action, details='', success=True):
    await ensure_donation_revoke_schema()
    async with db() as cur:
        await cur.execute("""
            INSERT INTO donation_revoke_audit
            (created_at,source,admin_id,admin_name,chat_id,target_id,action,details,success)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (int(time.time()), source, admin_id, admin_name or '', chat_id, target_id, action, details, 1 if success else 0))

def render_revoke_text(template: str, *, user: str, donation: str, command: str = '') -> str:
    values = {'user': user, 'donation': donation, 'command': command}
    try:
        return str(template).format_map(values)
    except (KeyError, ValueError):
        return str(template)
