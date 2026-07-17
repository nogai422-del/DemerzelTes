# Участники чата: CSV-импорт, просмотр и выдача донат-пакетов.

import csv
import hmac
import io
import os
import secrets
from datetime import datetime

from flask import Blueprint, redirect, render_template, request, session

from bot.database import db
from bot.donations import CATEGORY_TITLES, extend_donation_grant, ensure_donation_schema
from env_config import require_int_env

members_bp = Blueprint("members", __name__)
SOURCE_CHAT_ID = require_int_env("SOURCE_CHAT_ID")


async def ensure_members_schema() -> None:
    async with db() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                telegram_status TEXT NOT NULL DEFAULT '',
                telegram_last_seen_at INTEGER,
                telegram_status_checked_at INTEGER,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            )
            """
        )


def _csrf_ok() -> bool:
    sent = request.form.get("csrf_token", "")
    stored = session.get("csrf_token", "")
    return bool(sent and stored and hmac.compare_digest(sent, stored))


@members_bp.route("/members", methods=["GET", "POST"])
async def members():
    if "username" not in session:
        return redirect("/login")

    await ensure_members_schema()
    await ensure_donation_schema()
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    if request.method == "POST":
        if not _csrf_ok():
            return redirect("/members?csrf=0")

        action = request.form.get("action", "")
        if action == "import_csv":
            upload = request.files.get("csv_file")
            if not upload or not upload.filename:
                return redirect("/members?error=no_file")
            try:
                text = upload.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text))
                required = {"user_id", "username", "name"}
                if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                    return redirect("/members?error=bad_columns")
                count = 0
                now = int(datetime.now().timestamp())
                async with db() as cur:
                    for row in reader:
                        try:
                            user_id = int((row.get("user_id") or "").strip())
                        except ValueError:
                            continue
                        def as_int(value, default=0):
                            try:
                                return int(float(value)) if str(value).strip() else default
                            except (TypeError, ValueError):
                                return default
                        await cur.execute(
                            """
                            INSERT INTO admin_members(
                                chat_id, user_id, username, name, message_count,
                                telegram_status, telegram_last_seen_at,
                                telegram_status_checked_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                                username=excluded.username, name=excluded.name,
                                message_count=excluded.message_count,
                                telegram_status=excluded.telegram_status,
                                telegram_last_seen_at=excluded.telegram_last_seen_at,
                                telegram_status_checked_at=excluded.telegram_status_checked_at,
                                updated_at=excluded.updated_at
                            """,
                            (
                                SOURCE_CHAT_ID, user_id, (row.get("username") or "").strip(),
                                (row.get("name") or "").strip(), as_int(row.get("message_count")),
                                (row.get("telegram_status") or "").strip(),
                                as_int(row.get("telegram_last_seen_at"), None),
                                as_int(row.get("telegram_status_checked_at"), None), now,
                            ),
                        )
                        await cur.execute(
                            """
                            INSERT INTO chat_users(chat_id, user_id, messages)
                            SELECT ?, ?, ?
                            WHERE NOT EXISTS(
                                SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=?
                            )
                            """,
                            (SOURCE_CHAT_ID, user_id, as_int(row.get("message_count")), SOURCE_CHAT_ID, user_id),
                        )
                        count += 1
                return redirect(f"/members?imported={count}")
            except (UnicodeDecodeError, csv.Error, OSError):
                return redirect("/members?error=bad_csv")

        if action == "grant_donation":
            try:
                user_id = int(request.form.get("user_id", ""))
                days = max(1, min(int(request.form.get("days", "30")), 3650))
                daily_limit_raw = (request.form.get("daily_limit") or "").strip()
                daily_limit = max(1, int(daily_limit_raw)) if daily_limit_raw else None
                category = request.form.get("category", "")
                await extend_donation_grant(SOURCE_CHAT_ID, user_id, category, days, daily_limit)
            except (TypeError, ValueError):
                return redirect("/members?error=bad_grant")
            return redirect(f"/members?granted={user_id}")

    query = (request.args.get("q") or "").strip()
    params = [SOURCE_CHAT_ID]
    where = "WHERE m.chat_id = ?"
    if query:
        where += " AND (CAST(m.user_id AS TEXT) LIKE ? OR m.username LIKE ? OR m.name LIKE ?)"
        term = f"%{query}%"
        params.extend([term, term, term])

    async with db() as cur:
        await cur.execute(
            f"""
            SELECT m.user_id, m.username, m.name, m.message_count, m.telegram_status,
                   GROUP_CONCAT(g.category || ':' || g.valid_until, ',') AS grants
            FROM admin_members m
            LEFT JOIN donation_grants g
              ON g.chat_id=m.chat_id AND g.user_id=m.user_id AND g.valid_until > strftime('%s','now')
            {where}
            GROUP BY m.user_id, m.username, m.name, m.message_count, m.telegram_status
            ORDER BY m.name COLLATE NOCASE, m.username COLLATE NOCASE
            LIMIT 1000
            """,
            params,
        )
        rows = await cur.fetchall()

    return render_template(
        "members.html", members=rows, categories=CATEGORY_TITLES,
        csrf_token=session["csrf_token"], query=query,
        imported=request.args.get("imported"), granted=request.args.get("granted"),
        error=request.args.get("error"), csrf=request.args.get("csrf"),
    )
