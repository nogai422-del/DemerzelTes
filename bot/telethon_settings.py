"""Настройки Telethon, редактируемые из административной панели."""
from __future__ import annotations

import os
from bot.database import db

_DEFAULTS = {
    "enabled": "0",
    "api_id": "",
    "api_hash": "",
    "session": "",
    "session_type": "file",
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


async def get_telethon_settings(*, include_secrets: bool = True) -> dict:
    await ensure_telethon_settings_schema()
    async with db() as cur:
        await cur.execute("SELECT setting_key, setting_value FROM telethon_settings")
        values = {str(row[0]): str(row[1] or "") for row in await cur.fetchall()}
    result = {**_DEFAULTS, **values}

    # Переменные окружения остаются аварийным/первичным источником, если поле в БД пустое.
    env_fallbacks = {
        "api_id": "TELETHON_API_ID",
        "api_hash": "TELETHON_API_HASH",
        "session": "TELETHON_SESSION",
        "default_chat_id": "TELETHON_CHAT_ID",
    }
    for key, env_name in env_fallbacks.items():
        if not result.get(key):
            result[key] = (os.getenv(env_name) or "").strip()

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


def mask_secret(value: str, visible: int = 4) -> str:
    value = str(value or "")
    if not value:
        return "не задан"
    if len(value) <= visible * 2:
        return "•" * len(value)
    return f"{value[:visible]}{'•' * 10}{value[-visible:]}"
