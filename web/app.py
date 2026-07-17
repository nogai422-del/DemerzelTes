# Flask-приложение админ-панели: сборка приложения и подключение blueprint-ов.

from flask import Flask, render_template, redirect, session
from dotenv import load_dotenv
from env_config import require_env

load_dotenv()


# Создает Flask-приложение и регистрирует маршруты.
def create_app():
    app = Flask(__name__)
    app.secret_key = require_env("FLASK_SECRET_KEY")

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

    from .members import members_bp
    app.register_blueprint(members_bp)

    from .appearance import appearance_bp, get_ui_settings
    app.register_blueprint(appearance_bp)

    @app.context_processor
    def inject_ui_settings():
        return {"ui_settings": get_ui_settings()}

    @app.get("/health")
    def health():
        return "ok", 200

    # Редиректит на стартовую страницу панели после авторизации.
    @app.route("/")
    async def index():
        if "username" not in session:
            return redirect("/login")
        return render_template("index.html")

    return app

app = create_app()
