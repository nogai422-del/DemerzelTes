"""Админ-панель гибких шаблонов сообщений /viewd и /viewmd."""

import hmac
import os
import secrets
import time
import uuid

from flask import Blueprint, redirect, render_template, request, session

from bot.database import db
from bot.message_templates import DEFAULTS, ensure_message_template_schema

message_settings_bp = Blueprint("message_settings", __name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "bot", "images", "message_media")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}


def _checked(name: str) -> int:
    return 1 if request.form.get(name) == "1" else 0


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(request.form.get(name, default)), maximum))
    except (TypeError, ValueError):
        return default


@message_settings_bp.route("/message_settings", methods=["GET", "POST"])
async def message_settings():
    if "username" not in session:
        return redirect("/login")

    await ensure_message_template_schema()
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            return redirect("/message_settings?csrf=0")

        async with db() as cur:
            for key in DEFAULTS:
                prefix = f"template[{key}]"
                await cur.execute(
                    "SELECT media_path FROM message_templates WHERE template_key = ?",
                    (key,),
                )
                row = await cur.fetchone()
                media_path = str(row[0] if row else "")

                if request.form.get(f"{prefix}[remove_media]") == "1":
                    media_path = ""

                upload = request.files.get(f"{prefix}[media]")
                if upload and upload.filename:
                    ext = upload.filename.rsplit(".", 1)[-1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        filename = f"{key}_{uuid.uuid4().hex}.{ext}"
                        upload.save(os.path.join(UPLOAD_DIR, filename))
                        media_path = f"message_media/{filename}"

                await cur.execute(
                    """
                    UPDATE message_templates SET
                        enabled = ?, message = ?, empty_text = ?, usage_text = ?,
                        media_path = ?, delete_seconds = ?, show_delete_notice = ?,
                        disable_preview = ?, silent = ?, protect_content = ?,
                        button1_text = ?, button1_url = ?, button2_text = ?,
                        button2_url = ?, updated_at = ?
                    WHERE template_key = ?
                    """,
                    (
                        _checked(f"{prefix}[enabled]"),
                        request.form.get(f"{prefix}[message]", ""),
                        request.form.get(f"{prefix}[empty_text]", ""),
                        request.form.get(f"{prefix}[usage_text]", ""),
                        media_path,
                        _bounded_int(f"{prefix}[delete_seconds]", 30, 0, 86400),
                        _checked(f"{prefix}[show_delete_notice]"),
                        _checked(f"{prefix}[disable_preview]"),
                        _checked(f"{prefix}[silent]"),
                        _checked(f"{prefix}[protect_content]"),
                        request.form.get(f"{prefix}[button1_text]", ""),
                        request.form.get(f"{prefix}[button1_url]", ""),
                        request.form.get(f"{prefix}[button2_text]", ""),
                        request.form.get(f"{prefix}[button2_url]", ""),
                        int(time.time()),
                        key,
                    ),
                )
        return redirect("/message_settings?saved=1")

    async with db() as cur:
        await cur.execute("SELECT * FROM message_templates ORDER BY template_key")
        templates = {str(row["template_key"]): dict(row) for row in await cur.fetchall()}

    return render_template(
        "message_settings.html",
        templates=templates,
        saved=request.args.get("saved"),
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
