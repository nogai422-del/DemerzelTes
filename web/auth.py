# Аутентификация админ-панели: вход и выход пользователя.

from flask import Blueprint, render_template, request, redirect, session
from env_config import require_env

auth_bp = Blueprint("auth", __name__)

ADMIN_USERNAME = require_env("ADMIN_USERNAME")
ADMIN_PASSWORD = require_env("ADMIN_PASSWORD")


# Обрабатывает вход в админ-панель и создание сессии.
@auth_bp.route("/login", methods=["GET", "POST"])
async def login():

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["username"] = username
            return redirect("/")

        return render_template("login.html", error="Неверный логин или пароль")

    return render_template("login.html", error=None)


# Завершает сессию и выполняет выход из админ-панели.
@auth_bp.route("/logout")
async def logout():
    session.clear()
    return redirect("/login")
