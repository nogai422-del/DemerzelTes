# Участники: удобный каталог, CSV-импорт и выдача донатов с уведомлением в чат.
from __future__ import annotations

import csv
import hmac
import html
import io
import math
import os
import secrets
import time
from datetime import datetime
from urllib.parse import urlencode

from aiogram import Bot
from flask import Blueprint, redirect, render_template, request, session

from bot.database import DB_PATH, db
from bot.donations import CATEGORY_TITLES, ensure_donation_schema, extend_donation_grant
from bot.schema import ensure_database_ready
from env_config import require_env

members_bp = Blueprint("members", __name__)

PAGE_SIZES = (25, 50, 100)
SORT_OPTIONS = {
    "activity": "COALESCE(cu.chat_messages, am.message_count, 0) DESC, am.user_id DESC",
    "name": "LOWER(COALESCE(NULLIF(am.name,''), NULLIF(am.username,''), CAST(am.user_id AS TEXT))) ASC",
    "id": "am.user_id DESC",
    "status": "LOWER(am.telegram_status) ASC, am.user_id DESC",
    "stage": "stage_code DESC, COALESCE(cu.chat_messages, am.message_count, 0) DESC",
}


def _auth() -> bool:
    return "username" in session


def _to_int(value, default=0):
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def _safe_page(value: str | None) -> int:
    return max(1, _to_int(value, 1))


def _members_redirect(**updates):
    params = {
        "chat_id": request.form.get("return_chat_id") or request.args.get("chat_id") or "",
        "q": request.form.get("return_q") or request.args.get("q") or "",
        "stage": request.form.get("return_stage") or request.args.get("stage") or "",
        "status": request.form.get("return_status") or request.args.get("status") or "",
        "donation": request.form.get("return_donation") or request.args.get("donation") or "",
        "sort": request.form.get("return_sort") or request.args.get("sort") or "activity",
    }
    params.update({key: value for key, value in updates.items() if value is not None})
    params = {key: value for key, value in params.items() if str(value) != ""}
    return redirect("/members?" + urlencode(params))


def _parse_grants(raw: str | None) -> list[dict]:
    grants = []
    for part in str(raw or "").split(","):
        if not part:
            continue
        bits = part.split("|")
        if len(bits) != 3:
            continue
        category, valid_until_raw, limit_raw = bits
        valid_until = _to_int(valid_until_raw)
        grants.append(
            {
                "category": category,
                "title": CATEGORY_TITLES.get(category, category),
                "valid_until": valid_until,
                "valid_until_text": datetime.fromtimestamp(valid_until).strftime("%d.%m.%Y") if valid_until else "—",
                "daily_limit": _to_int(limit_raw),
            }
        )
    return grants


async def _send_grant_notification(
    *,
    chat_id: int,
    user_id: int,
    display_name: str,
    category: str,
    days: int,
    daily_limit: int,
    valid_until: int,
) -> bool:
    """Отправляет в целевой чат сообщение о донате, выданном из веб-панели."""
    title = CATEGORY_TITLES.get(category, category)
    safe_name = html.escape(display_name or f"Пользователь {user_id}")
    until_text = datetime.fromtimestamp(valid_until).strftime("%d.%m.%Y")
    text = (
        "🎁 <b>Выдан донат</b>\n\n"
        f'<a href="tg://user?id={user_id}">{safe_name}</a> получает '
        f"<b>{html.escape(title)}</b> на <b>{days}</b> дн.\n"
        f"Действует до: <b>{until_text}</b>."
    )
    if daily_limit > 0 and category in {"voice", "emoji", "video_note"}:
        text += f"\nСуточный лимит: <b>{daily_limit}</b>."
    text += "\n\n<i>Выдано через админ-панель.</i>"

    notify_bot = Bot(token=require_env("BOT_TOKEN"))
    try:
        await notify_bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        print(
            "Не удалось отправить уведомление о выдаче доната "
            f"chat_id={chat_id} user_id={user_id} category={category}: {exc}"
        )
        return False
    finally:
        await notify_bot.session.close()


@members_bp.route("/members", methods=["GET", "POST"])
async def members():
    if not _auth():
        return redirect("/login")

    ensure_database_ready(DB_PATH)
    await ensure_donation_schema()
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    notice = request.args.get("notice", "")
    error = request.args.get("error", "")

    if request.method == "POST":
        if not hmac.compare_digest(
            request.form.get("csrf_token", ""), session.get("csrf_token", "")
        ):
            return _members_redirect(error="csrf")

        action = request.form.get("action", "")
        if action == "import_csv":
            uploaded = request.files.get("csv_file")
            if not uploaded or not uploaded.filename:
                return _members_redirect(error="no_file")

            raw = uploaded.read()
            text = None
            for encoding in ("utf-8-sig", "utf-8", "cp1251"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                return _members_redirect(error="encoding")

            try:
                reader = csv.DictReader(io.StringIO(text))
                if not reader.fieldnames or "user_id" not in reader.fieldnames:
                    return _members_redirect(error="columns")
                rows = []
                for row in reader:
                    user_id = _to_int(row.get("user_id"))
                    if user_id <= 0:
                        continue
                    rows.append(
                        (
                            user_id,
                            str(row.get("username") or "").strip().lstrip("@"),
                            str(row.get("name") or "").strip(),
                            max(0, _to_int(row.get("message_count"))),
                            str(row.get("telegram_status") or "").strip(),
                            str(row.get("telegram_last_seen_at") or "").strip(),
                            str(row.get("telegram_status_checked_at") or "").strip(),
                            int(time.time()),
                        )
                    )
            except (csv.Error, TypeError):
                return _members_redirect(error="bad_csv")

            chat_id = _to_int(request.form.get("chat_id"))
            async with db() as cur:
                await cur.executemany(
                    """INSERT INTO admin_members(
                        user_id,username,name,message_count,telegram_status,
                        telegram_last_seen_at,telegram_status_checked_at,imported_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username=excluded.username,name=excluded.name,
                        message_count=excluded.message_count,
                        telegram_status=excluded.telegram_status,
                        telegram_last_seen_at=excluded.telegram_last_seen_at,
                        telegram_status_checked_at=excluded.telegram_status_checked_at,
                        imported_at=excluded.imported_at""",
                    rows,
                )
                if chat_id:
                    await cur.executemany(
                        """INSERT INTO chat_users(chat_id,user_id,messages,form_stage)
                           SELECT ?,?,?,'new' WHERE NOT EXISTS(
                             SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=?
                           )""",
                        [(chat_id, r[0], r[3], chat_id, r[0]) for r in rows],
                    )
            return _members_redirect(chat_id=chat_id or None, notice=f"imported_{len(rows)}")

        if action == "grant":
            chat_id = _to_int(request.form.get("chat_id"))
            user_id = _to_int(request.form.get("user_id"))
            days = _to_int(request.form.get("days"))
            daily_limit = _to_int(request.form.get("daily_limit"))
            category = request.form.get("category", "")
            if chat_id >= 0 or user_id <= 0 or days <= 0 or category not in CATEGORY_TITLES:
                return _members_redirect(error="grant")

            valid_until, _ = await extend_donation_grant(
                chat_id,
                user_id,
                category,
                days,
                daily_limit=daily_limit if daily_limit > 0 else None,
            )
            async with db() as cur:
                await cur.execute(
                    "SELECT name, username FROM admin_members WHERE user_id=?",
                    (user_id,),
                )
                row = await cur.fetchone()
            display_name = ""
            if row:
                display_name = str(row[0] or "").strip() or (
                    f"@{str(row[1]).strip()}" if str(row[1] or "").strip() else ""
                )

            notified = await _send_grant_notification(
                chat_id=chat_id,
                user_id=user_id,
                display_name=display_name,
                category=category,
                days=days,
                daily_limit=daily_limit,
                valid_until=valid_until,
            )
            return _members_redirect(
                chat_id=chat_id,
                notice="granted" if notified else "granted_notify_failed",
            )

    # Доступные чаты и выбранный контекст каталога.
    async with db() as cur:
        await cur.execute("SELECT DISTINCT chat_id FROM chat_users ORDER BY chat_id")
        chat_ids = [int(row[0]) for row in await cur.fetchall()]

    requested_chat = _to_int(request.args.get("chat_id"))
    env_chat = _to_int(os.getenv("SOURCE_CHAT_ID"))
    selected_chat_id = requested_chat if requested_chat in chat_ids else (
        env_chat if env_chat in chat_ids else (chat_ids[0] if chat_ids else env_chat)
    )

    query = str(request.args.get("q") or "").strip()
    status_filter = str(request.args.get("status") or "").strip()
    stage_filter = str(request.args.get("stage") or "").strip()
    donation_filter = str(request.args.get("donation") or "").strip()
    sort = str(request.args.get("sort") or "activity")
    if sort not in SORT_OPTIONS:
        sort = "activity"
    page = _safe_page(request.args.get("page"))
    per_page = _to_int(request.args.get("per_page"), 50)
    if per_page not in PAGE_SIZES:
        per_page = 50

    # Агрегаты привязаны к выбранному чату, а CSV-профиль остаётся общим.
    stage_sql = """CASE
        WHEN COALESCE(cu.stage_code, 0) >= 2 THEN 'saved'
        WHEN COALESCE(cu.stage_code, 0) = 1 OR bv.target_user_id IS NOT NULL THEN 'filling'
        ELSE 'new' END"""
    from_sql = f"""
        FROM admin_members am
        LEFT JOIN (
            SELECT user_id,
                   MAX(COALESCE(messages,0)) AS chat_messages,
                   MAX(CASE
                       WHEN TRIM(COALESCE(filled_form_text,'')) <> '' OR form_stage='saved' THEN 2
                       WHEN form_stage='filling' THEN 1
                       ELSE 0 END) AS stage_code
            FROM chat_users
            WHERE chat_id = ?
            GROUP BY user_id
        ) cu ON cu.user_id = am.user_id
        LEFT JOIN bv_messages bv
          ON bv.chat_id = ? AND bv.target_user_id = am.user_id
        LEFT JOIN (
            SELECT user_id,
                   GROUP_CONCAT(category || '|' || valid_until || '|' || daily_limit, ',') AS grants
            FROM donation_grants
            WHERE chat_id = ? AND valid_until > CAST(strftime('%s','now') AS INTEGER)
            GROUP BY user_id
        ) dg ON dg.user_id = am.user_id
    """
    base_params: list = [selected_chat_id, selected_chat_id, selected_chat_id]
    where_parts = []
    where_params: list = []
    if query:
        like = f"%{query}%"
        where_parts.append("(CAST(am.user_id AS TEXT) LIKE ? OR am.username LIKE ? OR am.name LIKE ?)")
        where_params.extend([like, like, like])
    if status_filter:
        where_parts.append("am.telegram_status = ?")
        where_params.append(status_filter)
    if stage_filter in {"new", "filling", "saved"}:
        where_parts.append(f"{stage_sql} = ?")
        where_params.append(stage_filter)
    if donation_filter in CATEGORY_TITLES:
        where_parts.append(
            "EXISTS (SELECT 1 FROM donation_grants f "
            "WHERE f.chat_id=? AND f.user_id=am.user_id AND f.category=? "
            "AND f.valid_until > CAST(strftime('%s','now') AS INTEGER))"
        )
        where_params.extend([selected_chat_id, donation_filter])
    where_sql = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    async with db() as cur:
        await cur.execute(
            f"SELECT COUNT(*) {from_sql} {where_sql}",
            base_params + where_params,
        )
        total_filtered = int((await cur.fetchone())[0])
        total_pages = max(1, math.ceil(total_filtered / per_page))
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        await cur.execute(
            f"""SELECT am.user_id, am.username, am.name,
                       COALESCE(cu.chat_messages, am.message_count, 0) AS message_count,
                       am.telegram_status, am.telegram_last_seen_at,
                       am.telegram_status_checked_at, am.imported_at,
                       COALESCE(cu.stage_code, 0) AS stage_code,
                       {stage_sql} AS form_stage,
                       dg.grants
                {from_sql}
                {where_sql}
                ORDER BY {SORT_OPTIONS[sort]}
                LIMIT ? OFFSET ?""",
            base_params + where_params + [per_page, offset],
        )
        members_rows = []
        for row in await cur.fetchall():
            item = dict(row)
            item["grants"] = _parse_grants(item.get("grants"))
            members_rows.append(item)

        await cur.execute("SELECT COUNT(*) FROM admin_members")
        total_members = int((await cur.fetchone())[0])
        await cur.execute(
            """SELECT COUNT(*) FROM donation_grants
               WHERE chat_id=? AND valid_until > CAST(strftime('%s','now') AS INTEGER)""",
            (selected_chat_id,),
        )
        active_donations = int((await cur.fetchone())[0])
        await cur.execute(
            """SELECT
                   SUM(CASE WHEN stage_code=0 THEN 1 ELSE 0 END) AS new_count,
                   SUM(CASE WHEN stage_code=1 THEN 1 ELSE 0 END) AS filling_count,
                   SUM(CASE WHEN stage_code=2 THEN 1 ELSE 0 END) AS saved_count
               FROM (
                   SELECT am.user_id,
                          CASE
                            WHEN MAX(CASE WHEN TRIM(COALESCE(cu.filled_form_text,'')) <> '' OR cu.form_stage='saved' THEN 2 ELSE 0 END)=2 THEN 2
                            WHEN MAX(CASE WHEN cu.form_stage='filling' OR bv.target_user_id IS NOT NULL THEN 1 ELSE 0 END)=1 THEN 1
                            ELSE 0 END AS stage_code
                   FROM admin_members am
                   LEFT JOIN chat_users cu ON cu.user_id=am.user_id AND cu.chat_id=?
                   LEFT JOIN bv_messages bv ON bv.target_user_id=am.user_id AND bv.chat_id=?
                   GROUP BY am.user_id
               )""",
            (selected_chat_id, selected_chat_id),
        )
        stage_counts_row = await cur.fetchone()
        await cur.execute(
            "SELECT DISTINCT telegram_status FROM admin_members WHERE telegram_status <> '' ORDER BY telegram_status"
        )
        status_options = [str(row[0]) for row in await cur.fetchall()]

    stage_counts = {
        "new": int(stage_counts_row[0] or 0),
        "filling": int(stage_counts_row[1] or 0),
        "saved": int(stage_counts_row[2] or 0),
    }

    return render_template(
        "members.html",
        members=members_rows,
        chat_ids=chat_ids,
        selected_chat_id=selected_chat_id,
        categories=CATEGORY_TITLES,
        notice=notice,
        error=error,
        query=query,
        status_filter=status_filter,
        stage_filter=stage_filter,
        donation_filter=donation_filter,
        status_options=status_options,
        sort=sort,
        page=page,
        per_page=per_page,
        page_sizes=PAGE_SIZES,
        total_pages=total_pages,
        total_filtered=total_filtered,
        stats={
            "total_members": total_members,
            "active_donations": active_donations,
            "stages": stage_counts,
        },
        csrf_token=session["csrf_token"],
    )
