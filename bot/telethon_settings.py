"""Настройки и состояние авторизации Telethon в общей базе Demerzel."""
from __future__ import annotations

import os
import time
from bot.database import db

_DEFAULTS = {
    "enabled": "0",
    "api_id": "",
    "api_hash": "",
    "session": "",
    "session_type": "string",
    "phone": "",
    "authorized_user_id": "",
    "authorized_username": "",
    "authorized_name": "",
    "default_chat_id": "",
    "auto_sync_enabled": "0",
    "sync_interval_minutes": "360",
}


async def ensure_telethon_settings_schema() -> None:
    async with db() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS telethon_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            )
            """
        )
        await cur.executemany(
            "INSERT OR IGNORE INTO telethon_settings(setting_key, setting_value) VALUES(?, ?)",
            list(_DEFAULTS.items()),
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS telethon_auth_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                status TEXT NOT NULL DEFAULT 'idle',
                phone TEXT NOT NULL DEFAULT '',
                phone_code_hash TEXT NOT NULL DEFAULT '',
                pending_session TEXT NOT NULL DEFAULT '',
                error_text TEXT NOT NULL DEFAULT '',
                delivery_type TEXT NOT NULL DEFAULT '',
                next_delivery_type TEXT NOT NULL DEFAULT '',
                timeout_seconds INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await cur.execute(
            """INSERT OR IGNORE INTO telethon_auth_state
               (singleton,status,updated_at) VALUES(1,'idle',?)""",
            (int(time.time()),),
        )
        await cur.execute("PRAGMA table_info(telethon_auth_state)")
        columns = {str(row[1]) for row in await cur.fetchall()}
        for name, ddl in {
            "delivery_type": "TEXT NOT NULL DEFAULT ''",
            "next_delivery_type": "TEXT NOT NULL DEFAULT ''",
            "timeout_seconds": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                await cur.execute(f"ALTER TABLE telethon_auth_state ADD COLUMN {name} {ddl}")


async def get_telethon_settings(*, include_secrets: bool = True) -> dict:
    await ensure_telethon_settings_schema()
    async with db() as cur:
        await cur.execute("SELECT setting_key, setting_value FROM telethon_settings")
        values = {str(row[0]): str(row[1] or "") for row in await cur.fetchall()}
    result = {**_DEFAULTS, **values}

    env_fallbacks = {
        "api_id": "TELETHON_API_ID",
        "api_hash": "TELETHON_API_HASH",
        "session": "TELETHON_SESSION",
        "default_chat_id": "TELETHON_CHAT_ID",
    }
    for key, env_name in env_fallbacks.items():
        if not result.get(key):
            result[key] = (os.getenv(env_name) or "").strip()

    result["session_type"] = "string"
    result["enabled"] = result.get("enabled") == "1"
    result["auto_sync_enabled"] = result.get("auto_sync_enabled") == "1"
    try:
        result["sync_interval_minutes"] = max(15, int(result.get("sync_interval_minutes") or 360))
    except ValueError:
        result["sync_interval_minutes"] = 360

    if not include_secrets:
        result["api_hash"] = ""
        result["session"] = ""
    return result


async def save_telethon_settings(values: dict) -> None:
    await ensure_telethon_settings_schema()
    allowed = set(_DEFAULTS)
    rows = []
    for key, value in values.items():
        if key not in allowed:
            continue
        if key == "session_type":
            value = "string"
        rows.append((key, str(value if value is not None else "")))
    if not rows:
        return
    async with db() as cur:
        await cur.executemany(
            """
            INSERT INTO telethon_settings(setting_key, setting_value, updated_at)
            VALUES(?, ?, CAST(strftime('%s','now') AS INTEGER))
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value=excluded.setting_value,
                updated_at=excluded.updated_at
            """,
            rows,
        )


async def get_auth_state() -> dict:
    await ensure_telethon_settings_schema()
    async with db() as cur:
        await cur.execute("SELECT * FROM telethon_auth_state WHERE singleton=1")
        row = await cur.fetchone()
    return dict(row) if row else {"status": "idle"}


async def save_auth_state(**values) -> None:
    await ensure_telethon_settings_schema()
    allowed = {"status", "phone", "phone_code_hash", "pending_session", "error_text", "delivery_type", "next_delivery_type", "timeout_seconds"}
    values = {k: str(v if v is not None else "") for k, v in values.items() if k in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key}=?" for key in values)
    params = list(values.values()) + [int(time.time())]
    async with db() as cur:
        await cur.execute(
            f"UPDATE telethon_auth_state SET {assignments}, updated_at=? WHERE singleton=1",
            params,
        )


async def clear_auth_state() -> None:
    await save_auth_state(
        status="idle", phone="", phone_code_hash="", pending_session="", error_text="", delivery_type="", next_delivery_type="", timeout_seconds="0"
    )


def mask_secret(value: str, visible: int = 4) -> str:
    value = str(value or "")
    if not value:
        return "не задан"
    if len(value) <= visible * 2:
        return "•" * len(value)
    return f"{value[:visible]}{'•' * 10}{value[-visible:]}"
