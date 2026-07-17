# Редактирование плановых сообщений: расписание, варианты текста/картинок/кнопок.

import os
import re
import uuid
import hmac
import secrets

from flask import Blueprint, redirect, render_template, request, session

from bot.database import db
from bot.scheduled_messages import (
    SCHEDULED_IMAGES_DIR,
)

scheduled_messages_bp = Blueprint("scheduled_messages", __name__)

ALLOWED_EXT = {"jpg", "jpeg", "png", "gif"}


# Проверяет формат времени HH:MM.
def _is_hhmm(value: str) -> bool:
    if not value or ":" not in value:
        return False
    parts = value.split(":")
    if len(parts) != 2:
        return False
    if not parts[0].isdigit() or not parts[1].isdigit():
        return False
    h = int(parts[0])
    m = int(parts[1])
    return 0 <= h <= 23 and 0 <= m <= 59


def _hhmm_to_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


# Преобразует чекбокс формы в 0/1.
def _as_flag(value: str | None) -> int:
    return 1 if value in ("1", "on", "true", "True") else 0


# Удаляет файлы scheduled_images, которые больше не используются в БД.
async def _cleanup_unused_scheduled_images(candidates: set[str]) -> None:
    if not candidates:
        return

    normalized_candidates = {
        c.strip().replace("\\", "/")
        for c in candidates
        if c and c.strip().startswith("scheduled_images/")
    }
    if not normalized_candidates:
        return

    async with db() as cur:
        for image_path in normalized_candidates:
            await cur.execute(
                """
                SELECT 1
                FROM scheduled_variants
                WHERE image_path = ?
                LIMIT 1
                """,
                (image_path,),
            )
            still_used = await cur.fetchone()
            if still_used:
                continue

            filename = os.path.basename(image_path)
            if not filename:
                continue

            full_path = os.path.join(SCHEDULED_IMAGES_DIR, filename)
            if os.path.isfile(full_path):
                try:
                    os.remove(full_path)
                except OSError as e:
                    print(f"Не удалось удалить неиспользуемое изображение {full_path}: {e}")


# Разбирает поля variant[*][field] в список словарей.
def _parse_variants(form_data) -> list[dict]:
    parsed: dict[str, dict] = {}

    for key, value in form_data.items():
        if not key.startswith("variant["):
            continue
        parts = re.findall(r"\[(.*?)\]", key)
        if len(parts) != 2:
            continue
        idx, field = parts
        parsed.setdefault(idx, {})
        parsed[idx][field] = value

    result = []
    sortable_items = []
    for idx, data in parsed.items():
        if not str(idx).isdigit():
            continue
        sortable_items.append((int(idx), data))

    for idx, data in sorted(sortable_items, key=lambda x: x[0]):
        result.append(
            {
                "form_idx": idx,
                "text": (data.get("text") or "").strip(),
                "image_path": (data.get("image_path") or "").strip(),
                "button_text": (data.get("button_text") or "").strip(),
                "button_url": (data.get("button_url") or "").strip(),
                "remove_image": _as_flag(data.get("remove_image")),
            }
        )
    return result


# Страница со списком кампаний и быстрым созданием.
@scheduled_messages_bp.route("/scheduled_messages", methods=["GET", "POST"])
async def scheduled_messages_list():
    if "username" not in session:
        return redirect("/login")

    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(sent_token, session_token):
            return redirect("/scheduled_messages?csrf=0")

        action = request.form.get("action", "").strip()
        campaign_id_raw = request.form.get("campaign_id", "").strip()

        if action == "create":
            name = (request.form.get("name") or "").strip()

            if not name:
                return redirect("/scheduled_messages?error=Название обязательно")
            async with db() as cur:
                await cur.execute(
                    """
                    INSERT INTO scheduled_campaigns (
                        name, is_enabled, time_mode, fixed_time, range_start, range_end,
                        random_text_mode, updated_at
                    ) VALUES (?, 1, 'fixed', '12:00', '12:00', '13:00', 0, CAST(strftime('%s','now') AS INTEGER))
                    """,
                    (name,),
                )
                campaign_id = cur.lastrowid

                await cur.execute(
                    """
                    INSERT INTO scheduled_variants (
                        campaign_id, sort_order, text, image_path, button_text, button_url
                    ) VALUES (?, 0, '', '', '', '')
                    """,
                    (campaign_id,),
                )

            return redirect(f"/scheduled_messages/{campaign_id}?saved=1")

        if not campaign_id_raw.isdigit():
            return redirect("/scheduled_messages?error=Некорректный campaign_id")

        campaign_id = int(campaign_id_raw)

        if action == "delete":
            removed_images: set[str] = set()
            async with db() as cur:
                await cur.execute(
                    "SELECT image_path FROM scheduled_variants WHERE campaign_id = ?",
                    (campaign_id,),
                )
                removed_images = {
                    (r[0] or "").strip()
                    for r in await cur.fetchall()
                    if r[0]
                }
                await cur.execute("DELETE FROM scheduled_variants WHERE campaign_id = ?", (campaign_id,))
                await cur.execute("DELETE FROM scheduled_campaigns WHERE id = ?", (campaign_id,))

            await _cleanup_unused_scheduled_images(removed_images)
            return redirect("/scheduled_messages?saved=1")

        if action == "toggle":
            async with db() as cur:
                await cur.execute(
                    """
                    UPDATE scheduled_campaigns
                    SET is_enabled = CASE WHEN is_enabled = 1 THEN 0 ELSE 1 END,
                        updated_at = CAST(strftime('%s','now') AS INTEGER)
                    WHERE id = ?
                    """,
                    (campaign_id,),
                )
            return redirect("/scheduled_messages?saved=1")

    async with db() as cur:
        await cur.execute(
            """
            SELECT id, name, is_enabled, time_mode, fixed_time, range_start, range_end, last_sent_date
            FROM scheduled_campaigns
            ORDER BY id DESC
            """
        )
        rows = await cur.fetchall()

    campaigns = [dict(r) for r in rows]
    return render_template(
        "scheduled_messages.html",
        campaigns=campaigns,
        saved=request.args.get("saved"),
        error=request.args.get("error"),
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )


# Детальная настройка одной кампании и её вариантов.
@scheduled_messages_bp.route("/scheduled_messages/<int:campaign_id>", methods=["GET", "POST"])
async def scheduled_messages_edit(campaign_id: int):
    if "username" not in session:
        return redirect("/login")

    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        sent_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not sent_token or not session_token or not hmac.compare_digest(sent_token, session_token):
            return redirect(f"/scheduled_messages/{campaign_id}?csrf=0")

        name = (request.form.get("name") or "").strip()
        is_enabled = _as_flag(request.form.get("is_enabled"))
        time_mode = (request.form.get("time_mode") or "fixed").strip()
        fixed_time = (request.form.get("fixed_time") or "").strip()
        range_start = (request.form.get("range_start") or "").strip()
        range_end = (request.form.get("range_end") or "").strip()

        # random_text_mode в БД используем как флаг "случайный вариант".
        random_variant_mode = _as_flag(request.form.get("random_variant_mode"))
        enable_images = _as_flag(request.form.get("enable_images"))
        enable_buttons = _as_flag(request.form.get("enable_buttons"))

        if not name:
            return redirect(f"/scheduled_messages/{campaign_id}?error=Название обязательно")
        if time_mode not in ("fixed", "range"):
            return redirect(f"/scheduled_messages/{campaign_id}?error=Некорректный режим времени")
        if time_mode == "fixed" and not _is_hhmm(fixed_time):
            return redirect(f"/scheduled_messages/{campaign_id}?error=Время fixed_time должно быть HH:MM")
        if time_mode == "range":
            if not _is_hhmm(range_start) or not _is_hhmm(range_end):
                return redirect(f"/scheduled_messages/{campaign_id}?error=Диапазон должен быть в формате HH:MM")
            if _hhmm_to_minutes(range_end) <= _hhmm_to_minutes(range_start):
                return redirect(f"/scheduled_messages/{campaign_id}?error=Конец диапазона должен быть позже начала")

        variants = _parse_variants(request.form)
        if not variants:
            return redirect(f"/scheduled_messages/{campaign_id}?error=Добавьте хотя бы один вариант")

        # Если случайный вариант выключен, сохраняем только первый вариант.
        if random_variant_mode == 0:
            variants = [variants[0]]

        if enable_images == 1:
            for variant in variants:
                upload = request.files.get(f"variant[{variant['form_idx']}][upload]")
                if upload and upload.filename:
                    ext = upload.filename.rsplit(".", 1)[-1].lower()
                    if ext not in ALLOWED_EXT:
                        return redirect(f"/scheduled_messages/{campaign_id}?error=Разрешены только jpg/jpeg/png/gif")

        if enable_images == 1:
            os.makedirs(SCHEDULED_IMAGES_DIR, exist_ok=True)

        for variant in variants:
            if enable_images == 0:
                variant["image_path"] = ""
            elif variant["remove_image"] == 1:
                variant["image_path"] = ""
            if enable_images == 1:
                upload = request.files.get(f"variant[{variant['form_idx']}][upload]")
                if upload and upload.filename:
                    ext = upload.filename.rsplit(".", 1)[-1].lower()
                    new_name = f"img_{uuid.uuid4().hex}.{ext}"
                    upload.save(os.path.join(SCHEDULED_IMAGES_DIR, new_name))
                    variant["image_path"] = f"scheduled_images/{new_name}"

            if enable_buttons == 0:
                variant["button_text"] = ""
                variant["button_url"] = ""

        has_any_content = any((v["text"] or "").strip() or (v["image_path"] or "").strip() for v in variants)
        if not has_any_content:
            return redirect(f"/scheduled_messages/{campaign_id}?error=Нужен текст и/или изображение хотя бы в одном варианте")

        previous_images: set[str] = set()
        async with db() as cur:
            await cur.execute(
                "SELECT image_path FROM scheduled_variants WHERE campaign_id = ?",
                (campaign_id,),
            )
            previous_images = {
                (r[0] or "").strip()
                for r in await cur.fetchall()
                if r[0]
            }

            await cur.execute(
                """
                UPDATE scheduled_campaigns
                SET name = ?, is_enabled = ?, time_mode = ?,
                    fixed_time = ?, range_start = ?, range_end = ?,
                    random_text_mode = ?,
                    planned_for_date = NULL, planned_send_ts = NULL,
                    updated_at = CAST(strftime('%s','now') AS INTEGER)
                WHERE id = ?
                """,
                (
                    name,
                    is_enabled,
                    time_mode,
                    fixed_time or "12:00",
                    range_start or "12:00",
                    range_end or "13:00",
                    random_variant_mode,
                    campaign_id,
                ),
            )

            await cur.execute("DELETE FROM scheduled_variants WHERE campaign_id = ?", (campaign_id,))
            for sort_order, variant in enumerate(variants):
                await cur.execute(
                    """
                    INSERT INTO scheduled_variants (
                        campaign_id, sort_order, text, image_path, button_text, button_url
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        campaign_id,
                        sort_order,
                        variant["text"],
                        variant["image_path"],
                        variant["button_text"],
                        variant["button_url"],
                    ),
                )

        current_images = {
            (v.get("image_path") or "").strip()
            for v in variants
            if (v.get("image_path") or "").strip()
        }
        await _cleanup_unused_scheduled_images(previous_images - current_images)

        return redirect(f"/scheduled_messages/{campaign_id}?saved=1")

    async with db() as cur:
        await cur.execute("SELECT * FROM scheduled_campaigns WHERE id = ?", (campaign_id,))
        campaign_row = await cur.fetchone()
        if campaign_row is None:
            return redirect("/scheduled_messages?error=Рассылка не найдена")

        await cur.execute(
            """
            SELECT id, campaign_id, sort_order, text, image_path, button_text, button_url
            FROM scheduled_variants
            WHERE campaign_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (campaign_id,),
        )
        variant_rows = await cur.fetchall()

    campaign = dict(campaign_row)
    variants = [dict(v) for v in variant_rows]
    if not variants:
        variants = [
            {
                "id": 0,
                "campaign_id": campaign_id,
                "sort_order": 0,
                "text": "",
                "image_path": "",
                "button_text": "",
                "button_url": "",
            }
        ]

    enable_images = any((v.get("image_path") or "").strip() for v in variants)
    enable_buttons = any(
        (v.get("button_text") or "").strip() and (v.get("button_url") or "").strip()
        for v in variants
    )

    return render_template(
        "scheduled_message_edit.html",
        campaign=campaign,
        variants=variants,
        random_variant_mode=int(campaign.get("random_text_mode") or 0),
        enable_images=1 if enable_images else 0,
        enable_buttons=1 if enable_buttons else 0,
        saved=request.args.get("saved"),
        error=request.args.get("error"),
        csrf=request.args.get("csrf"),
        csrf_token=session["csrf_token"],
    )
