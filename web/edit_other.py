# Редактирование общих настроек бота (welcome, rules, reminders, timers).

import hmac
import secrets
from flask import Blueprint, render_template, request, redirect, session
from bot.database import db

edit_other_bp = Blueprint("edit_other", __name__)


# Безопасно приводит значение к int с дефолтом.
def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Обрабатывает форму общих настроек и сохраняет их в БД.
@edit_other_bp.route("/edit_other", methods=["GET", "POST"])
async def edit_other():

    if "username" not in session:
        return redirect("/login")

    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(sent_token, session_token):
            return redirect("/edit_other?csrf=0")

        other = request.form

        welcome_text = other.get("other[welcome_text]", "")
        welcome_button = other.get("other[welcome_button]", "")
        welcome_url = other.get("other[welcome_url]", "")
        welcome_button2 = other.get("other[welcome_button2]", "")
        welcome_url2 = other.get("other[welcome_url2]", "")

        rules_text = other.get("other[rules_text]", "")
        rules_button = other.get("other[rules_button]", "")
        rules_url = other.get("other[rules_url]", "")

        rating_info_text = other.get("other[rating_info_text]", "")
        rating_button = other.get("other[rating_button]", "")
        rating_url = other.get("other[rating_url]", "")

        myth_text = other.get("other[myth_text]", "")
        myth_button = other.get("other[myth_button]", "")
        myth_url = other.get("other[myth_url]", "")

        rules_reminder_message_number = to_int(other.get("other[rules_reminder_message_number]", 0))
        rating_reminder_message_number = to_int(other.get("other[rating_reminder_message_number]", 0))
        myth_reminder_message_number = to_int(other.get("other[myth_reminder_message_number]", 0))

        wisdom_timer_minutes = to_int(other.get("other[wisdom_timer_minutes]", 0))

        async with db() as cur:
            await cur.execute("""
                UPDATE other SET
                    welcome_text = ?,
                    welcome_button = ?,
                    welcome_url = ?,
                    welcome_button2 = ?,
                    welcome_url2 = ?,
                    rules_text = ?,
                    rules_button = ?,
                    rules_url = ?,
                    rating_info_text = ?,
                    rating_button = ?,
                    rating_url = ?,
                    myth_text = ?,
                    myth_button = ?,
                    myth_url = ?,
                    rules_reminder_message_number = ?,
                    rating_reminder_message_number = ?,
                    myth_reminder_message_number = ?,
                    wisdom_timer_minutes = ?
                WHERE rowid = (
                    SELECT rowid
                    FROM other
                    ORDER BY rowid
                    LIMIT 1
                )
            """, (
                welcome_text,
                welcome_button,
                welcome_url,
                welcome_button2,
                welcome_url2,
                rules_text,
                rules_button,
                rules_url,
                rating_info_text,
                rating_button,
                rating_url,
                myth_text,
                myth_button,
                myth_url,
                rules_reminder_message_number,
                rating_reminder_message_number,
                myth_reminder_message_number,
                wisdom_timer_minutes
            ))
            if cur.rowcount == 0:
                await cur.execute("""
                    INSERT INTO other (
                        welcome_text,
                        welcome_button,
                        welcome_url,
                        welcome_button2,
                        welcome_url2,
                        rules_text,
                        rules_button,
                        rules_url,
                        rating_info_text,
                        rating_button,
                        rating_url,
                        myth_text,
                        myth_button,
                        myth_url,
                        rules_reminder_message_number,
                        rating_reminder_message_number,
                        myth_reminder_message_number,
                        wisdom_timer_minutes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    welcome_text,
                    welcome_button,
                    welcome_url,
                    welcome_button2,
                    welcome_url2,
                    rules_text,
                    rules_button,
                    rules_url,
                    rating_info_text,
                    rating_button,
                    rating_url,
                    myth_text,
                    myth_button,
                    myth_url,
                    rules_reminder_message_number,
                    rating_reminder_message_number,
                    myth_reminder_message_number,
                    wisdom_timer_minutes
                ))

        return redirect("/edit_other?saved=1")

    columns = [
        "welcome_text",
        "welcome_button",
        "welcome_url",
        "welcome_button2",
        "welcome_url2",
        "rules_text",
        "rules_button",
        "rules_url",
        "rating_info_text",
        "rating_button",
        "rating_url",
        "myth_text",
        "myth_button",
        "myth_url",
        "rules_reminder_message_number",
        "rating_reminder_message_number",
        "myth_reminder_message_number",
        "wisdom_timer_minutes",
    ]

    async with db() as cur:
        await cur.execute(f"SELECT {', '.join(columns)} FROM other ORDER BY rowid LIMIT 1")
        row = await cur.fetchone()

    other_data = dict(zip(columns, row)) if row else {}

    return render_template(
        "edit_other.html",
        other=other_data,
        saved=request.args.get("saved"),
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
