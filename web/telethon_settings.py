"""Раздел настройки и авторизации Telethon в административной панели."""
from __future__ import annotations

import hmac
import secrets
from flask import Blueprint, redirect, render_template, request, session

from bot.telethon_settings import (
    get_auth_state,
    get_telethon_settings,
    mask_secret,
    save_telethon_settings,
)
from bot.telethon_sync import (
    begin_telethon_authorization,
    confirm_telethon_code,
    confirm_telethon_password,
    disconnect_telethon_session,
    get_sync_state,
    sync_chat_members,
    test_telethon_connection,
)

telethon_settings_bp = Blueprint("telethon_settings", __name__)


def _auth() -> bool:
    return "username" in session


def _csrf_ok() -> bool:
    return hmac.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", ""))


def _set_error(exc: Exception) -> None:
    session["telethon_last_error"] = str(exc)[:700]


@telethon_settings_bp.route("/telethon_settings", methods=["GET", "POST"])
async def telethon_settings():
    if not _auth():
        return redirect("/login")
    session.setdefault("csrf_token", secrets.token_urlsafe(32))
    notice = request.args.get("notice", "")
    error = request.args.get("error", "")

    if request.method == "POST":
        if not _csrf_ok():
            return redirect("/telethon_settings?error=csrf")
        action = request.form.get("action", "save")

        if action == "save":
            api_hash = request.form.get("api_hash", "").strip()
            try:
                interval = int(request.form.get("sync_interval_minutes", "360") or 360)
            except ValueError:
                interval = 360
            values = {
                "enabled": "1" if request.form.get("enabled") == "1" else "0",
                "api_id": request.form.get("api_id", "").strip(),
                "session_type": "string",
                "phone": request.form.get("phone", "").strip(),
                "default_chat_id": request.form.get("default_chat_id", "").strip(),
                "auto_sync_enabled": "1" if request.form.get("auto_sync_enabled") == "1" else "0",
                "sync_interval_minutes": str(max(15, min(10080, interval))),
            }
            if api_hash:
                values["api_hash"] = api_hash
            await save_telethon_settings(values)
            return redirect("/telethon_settings?notice=saved")

        if action == "request_code":
            try:
                # Сначала сохраняем API-параметры и телефон из этой же формы.
                values = {
                    "api_id": request.form.get("api_id", "").strip(),
                    "phone": request.form.get("phone", "").strip(),
                    "session_type": "string",
                }
                api_hash = request.form.get("api_hash", "").strip()
                if api_hash:
                    values["api_hash"] = api_hash
                await save_telethon_settings(values)
                result = await begin_telethon_authorization(values["phone"])
                return redirect("/telethon_settings?notice=code_sent")
            except Exception as exc:
                _set_error(exc)
                return redirect("/telethon_settings?error=request_code")

        if action == "confirm_code":
            try:
                result = await confirm_telethon_code(request.form.get("telegram_code", ""))
                if result.get("status") == "password_needed":
                    return redirect("/telethon_settings?notice=password_needed")
                return redirect(f"/telethon_settings?notice=authorized_{result['user_id']}")
            except Exception as exc:
                _set_error(exc)
                return redirect("/telethon_settings?error=confirm_code")

        if action == "confirm_password":
            try:
                result = await confirm_telethon_password(request.form.get("telegram_password", ""))
                return redirect(f"/telethon_settings?notice=authorized_{result['user_id']}")
            except Exception as exc:
                _set_error(exc)
                return redirect("/telethon_settings?error=confirm_password")

        if action == "disconnect":
            try:
                await disconnect_telethon_session()
                return redirect("/telethon_settings?notice=disconnected")
            except Exception as exc:
                _set_error(exc)
                return redirect("/telethon_settings?error=disconnect")

        if action == "test":
            try:
                result = await test_telethon_connection()
                return redirect(f"/telethon_settings?notice=test_ok_{result['user_id']}")
            except Exception as exc:
                _set_error(exc)
                return redirect("/telethon_settings?error=test")

        if action == "sync":
            try:
                result = await sync_chat_members()
                return redirect(f"/telethon_settings?notice=synced_{result['found']}_{result['new']}_{result['updated']}_{result['missing']}")
            except Exception as exc:
                _set_error(exc)
                return redirect("/telethon_settings?error=sync")

    current = await get_telethon_settings()
    auth_state = await get_auth_state()
    try:
        chat_id = int(str(current.get("default_chat_id") or "0"))
    except ValueError:
        chat_id = 0
    sync_state = await get_sync_state(chat_id) if chat_id else {}
    return render_template(
        "telethon_settings.html",
        settings=current,
        auth_state=auth_state,
        masked_api_hash=mask_secret(current.get("api_hash", "")),
        masked_session=mask_secret(current.get("session", "")),
        sync_state=sync_state,
        notice=notice,
        error=error,
        last_error=session.pop("telethon_last_error", ""),
        csrf_token=session["csrf_token"],
    )
