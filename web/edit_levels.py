# Редактирование уровней/рангов, сообщений и изображений уровней.

import os
import re
import uuid
import hmac
import secrets
from flask import Blueprint, render_template, request, redirect, session
from bot.database import db

edit_levels_bp = Blueprint("edit_levels", __name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
RANK_DIR = os.path.join(BASE_DIR, "bot/images/rank_images")

ALLOWED_EXT = {"jpg", "jpeg", "png", "gif"}


# Удаляет неиспользуемые файлы rank_images, которые больше не встречаются в levels.image_path.
def cleanup_unused_rank_images(candidates: set[str]) -> None:
    if not candidates:
        return

    for image_path in candidates:
        normalized = (image_path or "").strip().replace("\\", "/")
        if not normalized.startswith("rank_images/"):
            continue

        filename = os.path.basename(normalized)
        if not filename:
            continue

        full_path = os.path.join(RANK_DIR, filename)
        if os.path.isfile(full_path):
            try:
                os.remove(full_path)
            except OSError as e:
                print(f"Не удалось удалить неиспользуемое изображение уровня {full_path}: {e}")


# Преобразует данные в нужный формат.
def convert_to_link(message: str) -> str:
    return re.sub(
        r"\{user\}",
        '<a href="tg://user?id={user_id}">{name}</a>',
        message or ""
    )


# Преобразует данные в нужный формат.
def convert_to_display(message: str) -> str:
    return re.sub(
        r'<a href="tg://user\?id=\{user_id\}">\{name\}</a>',
        "{user}",
        message or ""
    )


# Обрабатывает редактирование уровней, валидацию и сохранение.
@edit_levels_bp.route("/edit_levels", methods=["GET", "POST"])
async def edit_levels():

    if "username" not in session:
        return redirect("/login")

    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(sent_token, session_token):
            return redirect("/edit_levels?csrf=0")

        parsed = {}

        # Собираем поля формы в словарь по уровням: {level: {field: value}}.
        for key, value in request.form.items():
            if not key.startswith("levels["):
                continue

            lvl = key.split("[")[1].split("]")[0]
            field = key.split("[")[2].split("]")[0]

            parsed.setdefault(lvl, {})[field] = value

        points = []
        error = None

        # Проверяем, что points у каждого уровня — корректное число.
        for lvl_str, data in parsed.items():

            raw_points = data.get("points", "").strip()

            if not raw_points.isdigit():
                error = f"Очки на уровне {lvl_str} должны быть числом!"
                break

            points.append((int(lvl_str), int(raw_points)))

        if not error:
            points.sort(key=lambda x: x[0])

            # Очки должны строго расти от уровня к уровню.
            for i in range(1, len(points)):
                if points[i][1] <= points[i - 1][1]:
                    error = f"Очки на уровне {points[i][0]} должны быть больше, чем на предыдущем!"
                    break

        if error:

            levels = []

            for lvl_str, data in sorted(parsed.items(), key=lambda x: int(x[0])):
                levels.append({
                    "level": int(lvl_str),
                    "points": data.get("points", ""),
                    "rank_name": data.get("rank_name", ""),
                    "message": data.get("message", ""),
                    "image_path": data.get("image_path", ""),
                    "button_text": data.get("button_text", ""),
                    "button_url": data.get("button_url", ""),
                })

            return render_template(
                "edit_levels.html",
                levels=levels,
                saved=None,
                error=error,
                csrf=request.args.get("csrf"),
                csrf_token=session["csrf_token"],
            )

        # Файлы сохраняем только после успешной валидации формы.
        for lvl in parsed:
            file_key = f"levels[{lvl}][upload]"
            f = request.files.get(file_key)

            if f and f.filename:
                ext = f.filename.rsplit(".", 1)[-1].lower()
                if ext in ALLOWED_EXT:
                    os.makedirs(RANK_DIR, exist_ok=True)

                    new_name = f"img_{uuid.uuid4().hex}.{ext}"
                    f.save(os.path.join(RANK_DIR, new_name))

                    parsed[lvl]["image_path"] = f"rank_images/{new_name}"

        old_images: set[str] = set()
        async with db() as cur:
            await cur.execute(
                """
                SELECT image_path
                FROM levels
                WHERE image_path IS NOT NULL AND image_path != ''
                """
            )
            old_images = {
                (r[0] or "").strip()
                for r in await cur.fetchall()
                if r[0]
            }

            # Upsert по каждому уровню: обновляем существующий или добавляем новый.
            for lvl_str, data in parsed.items():
                lvl = int(lvl_str)
                msg = convert_to_link(data.get("message", ""))

                await cur.execute(
                    "SELECT COUNT(*) FROM levels WHERE level=?",
                    (lvl,)
                )
                exists = (await cur.fetchone())[0]

                values = (
                    int(data.get("points", 0)),
                    data.get("rank_name", ""),
                    msg,
                    data.get("image_path", ""),
                    data.get("button_text", ""),
                    data.get("button_url", ""),
                )

                if exists:
                    await cur.execute("""
                        UPDATE levels SET
                            points=?,
                            rank_name=?,
                            message=?,
                            image_path=?,
                            button_text=?,
                            button_url=?
                        WHERE level=?
                    """, values + (lvl,))
                else:
                    await cur.execute("""
                        INSERT INTO levels
                        (level, points, rank_name, message, image_path, button_text, button_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (lvl,) + values)

        async with db() as cur:
            await cur.execute(
                """
                SELECT image_path
                FROM levels
                WHERE image_path IS NOT NULL AND image_path != ''
                """
            )
            new_images = {
                (r[0] or "").strip()
                for r in await cur.fetchall()
                if r[0]
            }

        cleanup_unused_rank_images(old_images - new_images)

        return redirect("/edit_levels?saved=1")

    async with db() as cur:
        await cur.execute("SELECT * FROM levels ORDER BY level ASC")
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]

    levels = []
    for r in rows:
        row_dict = dict(zip(cols, r))
        row_dict["message"] = convert_to_display(
            row_dict.get("message", "")
        )
        levels.append(row_dict)

    return render_template(
        "edit_levels.html",
        levels=levels,
        saved=request.args.get("saved"),
        error=None,
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
