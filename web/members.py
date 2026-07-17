# Участники: просмотр, CSV-импорт и выдача донатов.
import csv
import io
import time
import hmac
import secrets

from flask import Blueprint, redirect, render_template, request, session

from bot.database import db
from bot.donations import CATEGORY_TITLES, extend_donation_grant
from bot.schema import ensure_database_ready
from bot.database import DB_PATH

members_bp = Blueprint("members", __name__)


def _auth() -> bool:
    return "username" in session


def _to_int(value, default=0):
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return default


@members_bp.route("/members", methods=["GET", "POST"])
async def members():
    if not _auth():
        return redirect("/login")
    ensure_database_ready(DB_PATH)
    session.setdefault("csrf_token", secrets.token_urlsafe(32))

    notice = request.args.get("notice", "")
    error = request.args.get("error", "")

    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("csrf_token", ""), session.get("csrf_token", "")):
            return redirect("/members?error=csrf")
        action = request.form.get("action", "")
        if action == "import_csv":
            uploaded = request.files.get("csv_file")
            if not uploaded or not uploaded.filename:
                return redirect("/members?error=no_file")
            raw = uploaded.read()
            text = None
            for encoding in ("utf-8-sig", "utf-8", "cp1251"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                return redirect("/members?error=encoding")
            try:
                reader = csv.DictReader(io.StringIO(text))
                if not reader.fieldnames or "user_id" not in reader.fieldnames:
                    return redirect("/members?error=columns")
                rows = []
                for row in reader:
                    user_id = _to_int(row.get("user_id"))
                    if user_id <= 0:
                        continue
                    rows.append((
                        user_id,
                        str(row.get("username") or "").strip().lstrip("@"),
                        str(row.get("name") or "").strip(),
                        max(0, _to_int(row.get("message_count"))),
                        str(row.get("telegram_status") or "").strip(),
                        str(row.get("telegram_last_seen_at") or "").strip(),
                        str(row.get("telegram_status_checked_at") or "").strip(),
                        int(time.time()),
                    ))
            except (csv.Error, TypeError):
                return redirect("/members?error=bad_csv")

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
                        """INSERT INTO chat_users(chat_id,user_id,messages)
                           SELECT ?,?,? WHERE NOT EXISTS(
                             SELECT 1 FROM chat_users WHERE chat_id=? AND user_id=?
                           )""",
                        [(chat_id, r[0], r[3], chat_id, r[0]) for r in rows],
                    )
            return redirect(f"/members?notice=imported_{len(rows)}")

        if action == "grant":
            chat_id = _to_int(request.form.get("chat_id"))
            user_id = _to_int(request.form.get("user_id"))
            days = _to_int(request.form.get("days"))
            daily_limit = _to_int(request.form.get("daily_limit"))
            category = request.form.get("category", "")
            if chat_id >= 0 or user_id <= 0 or days <= 0 or category not in CATEGORY_TITLES:
                return redirect("/members?error=grant")
            await extend_donation_grant(
                chat_id, user_id, category, days,
                daily_limit=daily_limit if daily_limit > 0 else None,
            )
            return redirect("/members?notice=granted")

    query = str(request.args.get("q") or "").strip()
    params = []
    where = ""
    if query:
        where = "WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR name LIKE ?"
        like = f"%{query}%"
        params = [like, like, like]
    async with db() as cur:
        await cur.execute(
            f"""SELECT user_id,username,name,message_count,telegram_status,
                       telegram_last_seen_at,telegram_status_checked_at
                FROM admin_members {where}
                ORDER BY message_count DESC,user_id DESC LIMIT 1000""",
            params,
        )
        members_rows = [dict(r) for r in await cur.fetchall()]
        await cur.execute("SELECT DISTINCT chat_id FROM chat_users ORDER BY chat_id")
        chat_ids = [int(r[0]) for r in await cur.fetchall()]

    return render_template(
        "members.html", members=members_rows, chat_ids=chat_ids,
        categories=CATEGORY_TITLES, notice=notice, error=error, query=query,
        csrf_token=session["csrf_token"],
    )
