# Редактирование сообщений ограничений и медиа для предупреждений модерации.

import os
import re
import uuid
import hmac
import secrets
from flask import Blueprint, render_template, request, redirect, session
from bot.database import db
from bot.donations import ensure_donation_schema
from bot.warning_state import ensure_warning_schema, ONBOARDING_PERMISSION_TYPE

edit_permission_bp = Blueprint("edit_permission", __name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "bot/images/permission_images")
DONATION_UPLOAD_DIR = os.path.join(BASE_DIR, "bot/images/donation_images")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DONATION_UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}


# Преобразует данные в нужный формат.
def convert_user_tag(msg: str) -> str:
    return re.sub(
        r"\{user\}",
        '<a href="tg://user?id={user_id}">{full_name}</a>',
        msg
    )


# Готовит сообщение прав к отображению в веб-форме.
def display_message(msg: str) -> str:
    return re.sub(
        r'<a href="tg:\/\/user\?id=\{user_id\}">\{full_name\}<\/a>',
        "{user}",
        msg
    )


async def _save_reaction_denied_template() -> None:
    """Сохраняет предупреждение о реакции без уровня «Медиа 2»."""
    prefix = "reaction_denied"
    message = request.form.get(f"{prefix}[message]", "")
    button1_text = request.form.get(f"{prefix}[button1_text]", "")
    button1_url = request.form.get(f"{prefix}[button1_url]", "")
    button2_text = request.form.get(f"{prefix}[button2_text]", "")
    button2_url = request.form.get(f"{prefix}[button2_url]", "")

    async with db() as cur:
        await cur.execute(
            """
            SELECT image_path
            FROM donation_notification_templates
            WHERE category = 'reaction' AND event_type = 'denied'
            """
        )
        row = await cur.fetchone()
        image_path = (row[0] if row else "") or ""

        if request.form.get(f"{prefix}[remove_image]") == "1":
            image_path = ""

        upload = request.files.get(f"{prefix}[upload]")
        if upload and upload.filename:
            extension = upload.filename.rsplit(".", 1)[-1].lower()
            if extension in ALLOWED_EXTENSIONS:
                filename = f"donation_{uuid.uuid4().hex}.{extension}"
                upload.save(os.path.join(DONATION_UPLOAD_DIR, filename))
                image_path = f"donation_images/{filename}"

        await cur.execute(
            """
            INSERT INTO donation_notification_templates (
                category, event_type, title, message, image_path,
                button1_text, button1_url, button2_text, button2_url
            ) VALUES (
                'reaction', 'denied', 'Реакции — попытка без Медиа 2',
                ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(category, event_type) DO UPDATE SET
                title = excluded.title,
                message = excluded.message,
                image_path = excluded.image_path,
                button1_text = excluded.button1_text,
                button1_url = excluded.button1_url,
                button2_text = excluded.button2_text,
                button2_url = excluded.button2_url
            """,
            (
                message,
                image_path,
                button1_text,
                button1_url,
                button2_text,
                button2_url,
            ),
        )


async def _load_reaction_denied_template() -> dict:
    """Загружает предупреждение о реакции без уровня «Медиа 2»."""
    async with db() as cur:
        await cur.execute(
            """
            SELECT message, image_path,
                   button1_text, button1_url, button2_text, button2_url
            FROM donation_notification_templates
            WHERE category = 'reaction' AND event_type = 'denied'
            """
        )
        row = await cur.fetchone()

    if not row:
        return {
            "message": "{user}, реакции доступны только с уровнем Медиа 2. Ваша реакция удалена.",
            "image_path": "",
            "button1_text": "",
            "button1_url": "",
            "button2_text": "",
            "button2_url": "",
        }

    return {
        "message": row[0] or "",
        "image_path": row[1] or "",
        "button1_text": row[2] or "",
        "button1_url": row[3] or "",
        "button2_text": row[4] or "",
        "button2_url": row[5] or "",
    }


# Обрабатывает редактирование текстов ограничений и кнопок.
@edit_permission_bp.route("/edit_permission_messages", methods=["GET", "POST"])
async def edit_permission_messages():

    if "username" not in session:
        return redirect("/login")

    await ensure_donation_schema()
    await ensure_warning_schema()
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(sent_token, session_token):
            return redirect("/edit_permission_messages?csrf=0")

        media_rows = {}

        for key in request.form:
            if not key.startswith("media["):
                continue

            parts = re.findall(r"\[(.*?)\]", key)
            if len(parts) != 2:
                continue

            media_type, field = parts
            media_rows.setdefault(media_type, {})
            media_rows[media_type][field] = request.form.get(key)

        async with db() as cur:
            for media_type, row in media_rows.items():
                message = row.get("message", "")
                image_path = row.get("image_path", "")
                button_text = row.get("button_text", "")
                button_url = row.get("button_url", "")

                file = request.files.get(f"media[{media_type}][upload]")

                if media_type != "emoji" and file and file.filename:
                    ext = file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        new_name = f"img_{uuid.uuid4().hex}.{ext}"
                        full_path = os.path.join(UPLOAD_DIR, new_name)
                        file.save(full_path)
                        image_path = f"permission_images/{new_name}"

                message = convert_user_tag(message)

                await cur.execute("""
                    UPDATE permission_types SET
                        message     = ?,
                        image_path  = ?,
                        button_text = ?,
                        button_url  = ?
                    WHERE media_type = ?
                """, (
                    message,
                    image_path,
                    button_text,
                    button_url,
                    media_type
                ))

        await _save_reaction_denied_template()
        return redirect("/edit_permission_messages?saved=1")

    async with db() as cur:
        await cur.execute(
            """
            SELECT * FROM permission_types
            ORDER BY CASE WHEN media_type = ? THEN 0 ELSE 1 END,
                     title COLLATE NOCASE ASC
            """,
            (ONBOARDING_PERMISSION_TYPE,),
        )
        rows = await cur.fetchall()

    media_list = []
    for r in rows:
        media_list.append({
            "media_type": r[0],
            "title": r[1],
            "message": display_message(r[2] or ""),
            "image_path": r[3],
            "button_text": r[4],
            "button_url": r[5],
        })

    reaction_denied = await _load_reaction_denied_template()

    return render_template(
        "edit_permission_messages.html",
        media_list=media_list,
        reaction_denied=reaction_denied,
        saved=request.args.get("saved"),
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
