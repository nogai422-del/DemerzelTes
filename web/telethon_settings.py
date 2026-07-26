"""Раздел настройки Telethon в административной панели."""
from __future__ import annotations

import hmac
import secrets
from flask import Blueprint, redirect, render_template, request, session

from bot.telethon_settings import get_telethon_settings, mask_secret, save_telethon_settings
from bot.telethon_sync import get_sync_state, sync_chat_members, test_telethon_connection

telethon_settings_bp = Blueprint("telethon_settings", __name__)


def _auth() -> bool:
    return "username" in session


def _csrf_ok() -> bool:
    return hmac.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", ""))


@telethon_settings_bp.route("/telethon_settings", methods=["GET", "POST"])
async def telethon_settings():
    if not _auth():
        return redirect("/login")
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    notice = request.args.get("notice", "")
    error = request.args.get("error", "")

    current = await get_telethon_settings()
    if request.method == "POST":
        if not _csrf_ok():
            return redirect("/telethon_settings?error=csrf")
        action = request.form.get("action", "save")
        if action == "save":
            api_hash = request.form.get("api_hash", "").strip()
            session_value = request.form.get("session_value", "").strip()
            try:
                interval = int(request.form.get("sync_interval_minutes", "360") or 360)
            except ValueError:
                interval = 360
            values = {
                "enabled": "1" if request.form.get("enabled") == "1" else "0",
                "api_id": request.form.get("api_id", "").strip(),
                "session_type": request.form.get("session_type", "file") if request.form.get("session_type") in {"file", "string"} else "file",
                "default_chat_id": request.form.get("default_chat_id", "").strip(),
                "auto_sync_enabled": "1" if request.form.get("auto_sync_enabled") == "1" else "0",
                "sync_interval_minutes": str(max(15, min(10080, interval))),
            }
            # Пустое поле секрета означает «оставить прежнее значение».
            if api_hash:
                values["api_hash"] = api_hash
            if session_value:
                values["session"] = session_value
            await save_telethon_settings(values)
            return redirect("/telethon_settings?notice=saved")
        if action == "test":
            try:
                result = await test_telethon_connection()
                return redirect(f"/telethon_settings?notice=test_ok_{result['user_id']}")
            except Exception as exc:
                session["telethon_last_error"] = str(exc)[:500]
                return redirect("/telethon_settings?error=test")
        if action == "sync":
            try:
                result = await sync_chat_members()
                return redirect(f"/telethon_settings?notice=synced_{result['found']}_{result['new']}_{result['updated']}_{result['missing']}")
            except Exception as exc:
                session["telethon_last_error"] = str(exc)[:500]
                return redirect("/telethon_settings?error=sync")

    current = await get_telethon_settings()
    try:
        chat_id = int(str(current.get("default_chat_id") or "0"))
    except ValueError:
        chat_id = 0
    sync_state = await get_sync_state(chat_id) if chat_id else {}
    return render_template(
        "telethon_settings.html",
        settings=current,
        masked_api_hash=mask_secret(current.get("api_hash", "")),
        masked_session=mask_secret(current.get("session", "")),
        sync_state=sync_state,
        notice=notice,
        error=error,
        last_error=session.pop("telethon_last_error", ""),
        csrf_token=session["csrf_token"],
    )
