# Настройка уведомлений о скором окончании и истечении донат-пакетов.

import hmac
import secrets

from flask import Blueprint, redirect, render_template, request, session

from bot.database import db
from bot.donations import (
    CATEGORY_EVENT_TYPES,
    CATEGORY_TITLES,
    EVENT_TITLES,
    ensure_donation_schema,
    get_donation_view_timers,
    get_expiry_notification_settings,
    get_usage_limits,
    set_donation_view_timers,
    set_expiry_notification_settings,
    set_usage_limits,
)
from bot.utils import normalize_telegram_button_url
from bot.notification_delivery import notification_upload_dir, store_notification_upload

edit_donation_notifications_bp = Blueprint(
    "edit_donation_notifications", __name__
)

UPLOAD_DIR = str(notification_upload_dir("donation_images"))



@edit_donation_notifications_bp.route(
    "/edit_donation_notifications", methods=["GET", "POST"]
)
async def edit_donation_notifications():
    if "username" not in session:
        return redirect("/login")

    await ensure_donation_schema()
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(
            sent_token, session_token
        ):
            return redirect("/edit_donation_notifications?csrf=0")

        def _positive_int(name: str, default: int) -> int:
            try:
                return max(1, min(int(request.form.get(name, default)), 100000))
            except (TypeError, ValueError):
                return default

        current_limits = await get_usage_limits()
        await set_usage_limits(
            voice=_positive_int(
                "limits[voice]", current_limits.get("voice", 20)
            ),
            emoji=_positive_int(
                "limits[emoji]", current_limits.get("emoji", 50)
            ),
            video_note=_positive_int(
                "limits[video_note]", current_limits.get("video_note", 10)
            ),
        )

        def _timer_seconds(name: str, default: int) -> int:
            try:
                return max(0, min(int(request.form.get(name, default)), 86400))
            except (TypeError, ValueError):
                return default

        current_timers = await get_donation_view_timers()
        await set_donation_view_timers(
            viewd=_timer_seconds(
                "view_timers[viewd]", current_timers.get("viewd", 30)
            ),
            viewmd=_timer_seconds(
                "view_timers[viewmd]", current_timers.get("viewmd", 30)
            ),
        )

        current_expiry_settings = await get_expiry_notification_settings()

        def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(
                    minimum,
                    min(int(request.form.get(name, default)), maximum),
                )
            except (TypeError, ValueError):
                return default

        await set_expiry_notification_settings(
            enabled=request.form.get("expiry_settings[enabled]") == "1",
            min_package_days=_bounded_int(
                "expiry_settings[min_package_days]",
                int(current_expiry_settings.get("min_package_days", 28)),
                28,
                3650,
            ),
            notice_days=_bounded_int(
                "expiry_settings[notice_days]",
                int(current_expiry_settings.get("notice_days", 3)),
                2,
                3,
            ),
        )

        try:
            async with db() as cur:
                for category in CATEGORY_TITLES:
                    for event_type in CATEGORY_EVENT_TYPES[category]:
                        prefix = f"template[{category}][{event_type}]"
                        message = request.form.get(f"{prefix}[message]", "")
                        button1_text = request.form.get(f"{prefix}[button1_text]", "")
                        button1_url = normalize_telegram_button_url(
                            request.form.get(f"{prefix}[button1_url]", "")
                        )
                        button2_text = request.form.get(f"{prefix}[button2_text]", "")
                        button2_url = normalize_telegram_button_url(
                            request.form.get(f"{prefix}[button2_url]", "")
                        )

                        await cur.execute(
                            """
                            SELECT image_path
                            FROM donation_notification_templates
                            WHERE category = ? AND event_type = ?
                            """,
                            (category, event_type),
                        )
                        row = await cur.fetchone()
                        image_path = (row[0] if row else "") or ""

                        if request.form.get(f"{prefix}[remove_image]") == "1":
                            image_path = ""

                        upload = request.files.get(f"{prefix}[upload]")
                        if upload and upload.filename:
                            filename = store_notification_upload(
                                upload.stream,
                                upload.filename,
                                UPLOAD_DIR,
                                prefix="donation",
                                preserve_animation=True,
                            )
                            image_path = f"donation_images/{filename}"

                        title = (
                            f"{CATEGORY_TITLES[category]} — {EVENT_TITLES[event_type]}"
                        )
                        await cur.execute(
                            """
                            INSERT INTO donation_notification_templates (
                                category, event_type, title, message, image_path,
                                button1_text, button1_url, button2_text, button2_url
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                category,
                                event_type,
                                title,
                                message,
                                image_path,
                                button1_text,
                                button1_url,
                                button2_text,
                                button2_url,
                            ),
                        )

        except ValueError as exc:
            print(f"Ошибка изображения донат-уведомления: {exc}")
            return redirect("/edit_donation_notifications?image_error=1")

        return redirect("/edit_donation_notifications?saved=1")

    async with db() as cur:
        await cur.execute(
            """
            SELECT category, event_type, title, message, image_path,
                   button1_text, button1_url, button2_text, button2_url
            FROM donation_notification_templates
            """
        )
        rows = await cur.fetchall()

    limits = await get_usage_limits()
    view_timers = await get_donation_view_timers()
    expiry_settings = await get_expiry_notification_settings()

    by_key = {(row[0], row[1]): row for row in rows}
    categories = []
    for category, category_title in CATEGORY_TITLES.items():
        templates = []
        for event_type in CATEGORY_EVENT_TYPES[category]:
            event_title = EVENT_TITLES[event_type]
            row = by_key.get((category, event_type))
            templates.append(
                {
                    "category": category,
                    "category_title": category_title,
                    "event_type": event_type,
                    "event_title": event_title,
                    "title": row[2] if row else f"{category_title} — {event_title}",
                    "message": row[3] if row else "",
                    "image_path": row[4] if row else "",
                    "button1_text": row[5] if row else "",
                    "button1_url": row[6] if row else "",
                    "button2_text": row[7] if row else "",
                    "button2_url": row[8] if row else "",
                }
            )
        categories.append(
            {
                "category": category,
                "title": category_title,
                "templates": templates,
            }
        )

    return render_template(
        "edit_donation_notifications.html",
        categories=categories,
        limits=limits,
        view_timers=view_timers,
        expiry_settings=expiry_settings,
        saved=request.args.get("saved"),
        csrf=request.args.get("csrf"),
        image_error=request.args.get("image_error"),
        csrf_token=session["csrf_token"],
    )
