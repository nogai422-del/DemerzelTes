# Редактирование шаблона анкеты (form_text) в админ-панели.

import re
import hmac
import secrets
from flask import Blueprint, render_template, request, redirect, session
from bot.database import db

edit_form_bp = Blueprint("edit_form", __name__)


# Обрабатывает редактирование шаблона анкеты.
@edit_form_bp.route("/edit_form", methods=["GET", "POST"])
async def edit_form():

    if "username" not in session:
        return redirect("/login")

    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(sent_token, session_token):
            return redirect("/edit_form?csrf=0")

        content = request.form.get("content", "").strip()

        if not content:
            return "Ошибка: Нет данных для сохранения.", 400

        content_for_db = re.sub(
            r"\{quote\}(.*?)\{\/quote\}",
            r"<blockquote expandable>\1</blockquote>",
            content,
            flags=re.S | re.I
        )

        async with db() as cur:
            await cur.execute(
                """
                UPDATE form_text
                SET content = ?
                WHERE rowid = (
                    SELECT rowid
                    FROM form_text
                    ORDER BY rowid
                    LIMIT 1
                )
                """,
                (content_for_db,)
            )
            if cur.rowcount == 0:
                await cur.execute(
                    "INSERT INTO form_text (content) VALUES (?)",
                    (content_for_db,)
                )

        return redirect("/edit_form?saved=1")

    async with db() as cur:
        await cur.execute("SELECT content FROM form_text ORDER BY rowid LIMIT 1")
        row = await cur.fetchone()

    stored_content = row[0] if row else ""

    display_content = re.sub(
        r"<blockquote\s+expandable>(.*?)</blockquote>",
        r"{quote}\1{/quote}",
        stored_content,
        flags=re.S | re.I
    )

    min_height = 150
    text_length = len(display_content)
    additional_height = (text_length // 100) + 1
    textarea_height = min_height + additional_height * 20

    saved = request.args.get("saved")

    return render_template(
        "edit_form.html",
        content=display_content,
        textarea_height=textarea_height,
        saved=saved,
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
