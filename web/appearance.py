# Настройки внешнего вида админ-панели.

import hmac
import os
import re
import secrets
import sqlite3
from typing import Any

from flask import Blueprint, redirect, render_template, request, session

from bot.database import DB_PATH

appearance_bp = Blueprint("appearance", __name__)

THEMES = {
    "dark": "Тёмная",
    "light": "Светлая",
    "system": "Как в системе",
}

UI_STYLES = {
    "classic": "Classic",
    "liquid": "Liquid Glass",
    "soft": "Soft UI",
    "minimal": "Minimal",
}

DEFAULT_UI_SETTINGS = {
    "theme": "dark",
    "accent_color": "#7c6cff",
    "button_opacity": 0.90,
    "ui_style": "classic",
}

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def ensure_ui_settings_schema() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_ui_settings (
                id                 INTEGER PRIMARY KEY CHECK (id = 1),
                theme              TEXT NOT NULL DEFAULT 'dark',
                accent_color       TEXT NOT NULL DEFAULT '#7c6cff',
                button_opacity     REAL NOT NULL DEFAULT 0.90,
                ui_style           TEXT NOT NULL DEFAULT 'classic'
            )
            """
        )
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(admin_ui_settings)")
        }
        migrations = {
            "theme": "TEXT NOT NULL DEFAULT 'dark'",
            "accent_color": "TEXT NOT NULL DEFAULT '#7c6cff'",
            "button_opacity": "REAL NOT NULL DEFAULT 0.90",
            "ui_style": "TEXT NOT NULL DEFAULT 'classic'",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                conn.execute(
                    f'ALTER TABLE admin_ui_settings ADD COLUMN "{column}" {definition}'
                )

        conn.execute(
            """
            INSERT OR IGNORE INTO admin_ui_settings (
                id, theme, accent_color, button_opacity, ui_style
            ) VALUES (1, 'dark', '#7c6cff', 0.90, 'classic')
            """
        )


def _normalize_settings(values: dict[str, Any]) -> dict[str, Any]:
    theme = str(values.get("theme", DEFAULT_UI_SETTINGS["theme"]))
    if theme not in THEMES:
        theme = DEFAULT_UI_SETTINGS["theme"]

    ui_style = str(values.get("ui_style", DEFAULT_UI_SETTINGS["ui_style"]))
    if ui_style not in UI_STYLES:
        ui_style = DEFAULT_UI_SETTINGS["ui_style"]

    accent_color = str(
        values.get("accent_color", DEFAULT_UI_SETTINGS["accent_color"])
    ).strip()
    if not _HEX_COLOR.fullmatch(accent_color):
        accent_color = DEFAULT_UI_SETTINGS["accent_color"]
    accent_color = accent_color.lower()

    try:
        button_opacity = float(
            values.get("button_opacity", DEFAULT_UI_SETTINGS["button_opacity"])
        )
    except (TypeError, ValueError):
        button_opacity = float(DEFAULT_UI_SETTINGS["button_opacity"])
    button_opacity = max(0.15, min(button_opacity, 1.0))

    rgb = tuple(int(accent_color[index:index + 2], 16) for index in (1, 3, 5))
    return {
        "theme": theme,
        "accent_color": accent_color,
        "accent_rgb": ", ".join(str(value) for value in rgb),
        "button_opacity": round(button_opacity, 2),
        "button_opacity_percent": int(round(button_opacity * 100)),
        "ui_style": ui_style,
    }


def get_ui_settings() -> dict[str, Any]:
    ensure_ui_settings_schema()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT theme, accent_color, button_opacity, ui_style
            FROM admin_ui_settings
            WHERE id = 1
            """
        ).fetchone()

    if not row:
        return _normalize_settings(DEFAULT_UI_SETTINGS)
    return _normalize_settings(
        {
            "theme": row[0],
            "accent_color": row[1],
            "button_opacity": row[2],
            "ui_style": row[3],
        }
    )


def save_ui_settings(values: dict[str, Any]) -> dict[str, Any]:
    settings = _normalize_settings(values)
    ensure_ui_settings_schema()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO admin_ui_settings (
                id, theme, accent_color, button_opacity, ui_style
            ) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                theme = excluded.theme,
                accent_color = excluded.accent_color,
                button_opacity = excluded.button_opacity,
                ui_style = excluded.ui_style
            """,
            (
                settings["theme"],
                settings["accent_color"],
                settings["button_opacity"],
                settings["ui_style"],
            ),
        )
    return settings


@appearance_bp.route("/appearance", methods=["GET", "POST"])
def appearance():
    if "username" not in session:
        return redirect("/login")

    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(
            sent_token, session_token
        ):
            return redirect("/appearance?csrf=0")

        save_ui_settings(
            {
                "theme": request.form.get("theme", "dark"),
                "accent_color": request.form.get("accent_color", "#7c6cff"),
                "button_opacity": request.form.get("button_opacity", "0.90"),
                "ui_style": request.form.get("ui_style", "classic"),
            }
        )
        return redirect("/appearance?saved=1")

    return render_template(
        "appearance.html",
        settings=get_ui_settings(),
        themes=THEMES,
        ui_styles=UI_STYLES,
        saved=request.args.get("saved"),
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
