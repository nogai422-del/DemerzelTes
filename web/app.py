# Flask-приложение админ-панели: сборка приложения и подключение blueprint-ов.

from datetime import timedelta
import os

from flask import Flask, render_template, redirect, request, session, jsonify
from dotenv import load_dotenv
from env_config import require_env

load_dotenv()

from bot.database import db
from bot.donations import (
    ensure_donation_schema,
    get_expiry_notification_settings,
)
from bot.settings import (
    ensure_chat_behavior_schema,
    get_restrict_new_members_telegram,
)


# Создает Flask-приложение и регистрирует маршруты.
def create_app():
    app = Flask(__name__)
    app.secret_key = require_env("FLASK_SECRET_KEY")
    app.config.update(
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )

    from .auth import auth_bp
    app.register_blueprint(auth_bp)

    from .edit_form import edit_form_bp
    app.register_blueprint(edit_form_bp)

    from .image import image_bp
    app.register_blueprint(image_bp)

    from .edit_levels import edit_levels_bp
    app.register_blueprint(edit_levels_bp)

    from .images import images_bp
    app.register_blueprint(images_bp)

    from .edit_permission_messages import edit_permission_bp
    app.register_blueprint(edit_permission_bp)

    from .edit_donation_notifications import edit_donation_notifications_bp
    app.register_blueprint(edit_donation_notifications_bp)

    from .edit_other import edit_other_bp
    app.register_blueprint(edit_other_bp)

    from .scheduled_messages import scheduled_messages_bp
    app.register_blueprint(scheduled_messages_bp)

    from .edit_badwords import edit_badwords_bp
    app.register_blueprint(edit_badwords_bp)

    from .appearance import appearance_bp, get_ui_settings
    app.register_blueprint(appearance_bp)

    from .message_settings import message_settings_bp
    app.register_blueprint(message_settings_bp)

    from .donation_revoke import donation_revoke_bp
    app.register_blueprint(donation_revoke_bp)

    from .members import members_bp
    app.register_blueprint(members_bp)

    @app.context_processor
    def inject_ui_settings():
        return {"ui_settings": get_ui_settings()}

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if response.mimetype == "text/html" and request.path != "/health":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health():
        required = ("chat_users", "other", "scheduled_campaigns", "form_text", "levels")
        async with db() as cur:
            await cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {str(row[0]) for row in await cur.fetchall()}
        missing = [name for name in required if name not in tables]
        status = 503 if missing else 200
        return jsonify({"status": "error" if missing else "ok", "missing_tables": missing}), status

    # Редиректит на стартовую страницу панели после авторизации.
    @app.route("/")
    async def index():
        if "username" not in session:
            return redirect("/login")

        await ensure_donation_schema()
        await ensure_chat_behavior_schema()
        expiry_settings = await get_expiry_notification_settings()
        restrict_new_members = await get_restrict_new_members_telegram()

        async with db() as cur:
            async def table_exists(name: str) -> bool:
                await cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (name,),
                )
                return await cur.fetchone() is not None

            await cur.execute(
                "SELECT COUNT(*) FROM donation_grants WHERE valid_until > CAST(strftime('%s','now') AS INTEGER)"
            )
            active_donations = int((await cur.fetchone())[0])

            chat_users = 0
            if await table_exists("chat_users"):
                await cur.execute("SELECT COUNT(*) FROM chat_users")
                chat_users = int((await cur.fetchone())[0])

            enabled_campaigns = 0
            if await table_exists("scheduled_campaigns"):
                await cur.execute(
                    "SELECT COUNT(*) FROM scheduled_campaigns WHERE is_enabled = 1"
                )
                enabled_campaigns = int((await cur.fetchone())[0])

        dashboard_stats = {
            "active_donations": active_donations,
            "chat_users": chat_users,
            "enabled_campaigns": enabled_campaigns,
            "preexpiry_enabled": bool(expiry_settings["enabled"]),
            "preexpiry_days": int(expiry_settings["notice_days"]),
            "preexpiry_min_days": int(expiry_settings["min_package_days"]),
            "restrict_new_members": restrict_new_members,
        }
        return render_template("index.html", dashboard_stats=dashboard_stats)

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.exception("Internal server error: %s", error)
        return render_template("500.html"), 500

    return app

app = create_app()
