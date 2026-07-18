"""Инициализация и безопасная миграция SQLite для bot + web."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

_LOCK = threading.RLock()
_READY_PATHS: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_users (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    filled_form_text TEXT,
    messages INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    permission_level INTEGER NOT NULL DEFAULT 0,
    can_view_forms INTEGER NOT NULL DEFAULT 0,
    rank_name_cache TEXT NOT NULL DEFAULT '',
    form_stage TEXT NOT NULL DEFAULT 'new',
    form_started_at INTEGER NOT NULL DEFAULT 0,
    form_saved_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS form_text (content TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS levels (
    level INTEGER NOT NULL,
    points INTEGER NOT NULL,
    rank_name TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    button_text TEXT NOT NULL DEFAULT '',
    button_url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS other (
    rules_text TEXT DEFAULT '', rules_button TEXT DEFAULT '', rules_url TEXT DEFAULT '',
    rating_info_text TEXT DEFAULT '', rating_button TEXT DEFAULT '', rating_url TEXT DEFAULT '',
    myth_text TEXT DEFAULT '', myth_button TEXT DEFAULT '', myth_url TEXT DEFAULT '',
    welcome_text TEXT DEFAULT '', welcome_button TEXT DEFAULT '', welcome_url TEXT DEFAULT '',
    welcome_button2 TEXT DEFAULT '', welcome_url2 TEXT DEFAULT '',
    rules_reminder_message_number INTEGER DEFAULT 0,
    rating_reminder_message_number INTEGER DEFAULT 0,
    myth_reminder_message_number INTEGER DEFAULT 0,
    wisdom_timer_minutes INTEGER DEFAULT 60
);
CREATE TABLE IF NOT EXISTS chat_lock (chat_id INTEGER PRIMARY KEY, locked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS last_message (chat_id INTEGER, time REAL, wisdom_time REAL);
CREATE TABLE IF NOT EXISTS old_message (chat_id INTEGER, message_id INTEGER);
CREATE TABLE IF NOT EXISTS timed_messages (chat_id INTEGER, message_id INTEGER, send_time REAL);
CREATE TABLE IF NOT EXISTS hello_messages (chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, send_time REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mutes (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, until INTEGER NOT NULL, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS emoji_permissions (chat_id INTEGER, user_id INTEGER, valid_until INTEGER, used_today INTEGER DEFAULT 0, used_date TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS voice_permissions (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, valid_until INTEGER NOT NULL DEFAULT 0, used_today INTEGER NOT NULL DEFAULT 0, used_date TEXT NOT NULL DEFAULT '', PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS permission_types (
    media_type TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '', button_text TEXT NOT NULL DEFAULT '', button_url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS permission_messages (
    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER NOT NULL, send_time REAL NOT NULL,
    PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS scheduled_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1, time_mode TEXT NOT NULL DEFAULT 'fixed',
    fixed_time TEXT NOT NULL DEFAULT '12:00', range_start TEXT NOT NULL DEFAULT '12:00',
    range_end TEXT NOT NULL DEFAULT '13:00', random_text_mode INTEGER NOT NULL DEFAULT 0,
    planned_for_date TEXT, planned_send_ts INTEGER, last_sent_date TEXT,
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE IF NOT EXISTS scheduled_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '', image_path TEXT NOT NULL DEFAULT '',
    button_text TEXT NOT NULL DEFAULT '', button_url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bv_messages (
    chat_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, PRIMARY KEY(chat_id,target_user_id)
);
CREATE TABLE IF NOT EXISTS admin_members (
    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0, telegram_status TEXT NOT NULL DEFAULT '',
    telegram_last_seen_at TEXT NOT NULL DEFAULT '', telegram_status_checked_at TEXT NOT NULL DEFAULT '',
    imported_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
"""

# Колонки, которые могли отсутствовать в старых базах.
MIGRATION_COLUMNS = {
    "chat_users": {
        "filled_form_text": "TEXT",
        "messages": "INTEGER NOT NULL DEFAULT 0",
        "score": "INTEGER NOT NULL DEFAULT 0",
        "level": "INTEGER NOT NULL DEFAULT 0",
        "permission_level": "INTEGER NOT NULL DEFAULT 0",
        "can_view_forms": "INTEGER NOT NULL DEFAULT 0",
        "rank_name_cache": "TEXT NOT NULL DEFAULT ''",
        "form_stage": "TEXT NOT NULL DEFAULT 'new'",
        "form_started_at": "INTEGER NOT NULL DEFAULT 0",
        "form_saved_at": "INTEGER NOT NULL DEFAULT 0",
    },
    "other": {
        "rating_button": "TEXT DEFAULT ''", "rating_url": "TEXT DEFAULT ''",
        "welcome_button": "TEXT DEFAULT ''", "welcome_url": "TEXT DEFAULT ''",
        "welcome_button2": "TEXT DEFAULT ''", "welcome_url2": "TEXT DEFAULT ''",
        "rules_reminder_message_number": "INTEGER DEFAULT 0",
        "rating_reminder_message_number": "INTEGER DEFAULT 0",
        "myth_reminder_message_number": "INTEGER DEFAULT 0",
        "wisdom_timer_minutes": "INTEGER DEFAULT 60",
    },
}

SEED_TABLES = ("form_text", "levels", "other", "permission_types", "scheduled_campaigns", "scheduled_variants")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _migrate_permission_messages(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "permission_messages")
    if cols and "user_id" not in cols:
        conn.execute("ALTER TABLE permission_messages RENAME TO permission_messages_legacy")
        conn.execute("""CREATE TABLE permission_messages (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER NOT NULL, send_time REAL NOT NULL,
            PRIMARY KEY(chat_id,user_id))""")
        conn.execute("""INSERT OR REPLACE INTO permission_messages(chat_id,user_id,message_id,send_time)
                        SELECT chat_id,0,message_id,send_time FROM permission_messages_legacy""")
        conn.execute("DROP TABLE permission_messages_legacy")


def _migrate_chat_users(conn: sqlite3.Connection) -> None:
    """Объединяет дубли пользователей и запрещает их повторное появление."""
    duplicates = conn.execute(
        """SELECT chat_id, user_id FROM chat_users
           GROUP BY chat_id, user_id HAVING COUNT(*) > 1"""
    ).fetchall()

    for chat_id, user_id in duplicates:
        rows = conn.execute(
            """SELECT rowid, filled_form_text, messages, score, level,
                      permission_level, can_view_forms, rank_name_cache,
                      form_stage, form_started_at, form_saved_at
               FROM chat_users
               WHERE chat_id=? AND user_id=?
               ORDER BY rowid DESC""",
            (chat_id, user_id),
        ).fetchall()
        if not rows:
            continue
        keep_rowid = int(rows[0][0])
        filled = next((str(r[1]) for r in rows if str(r[1] or "").strip()), "")
        rank = next((str(r[7]) for r in rows if str(r[7] or "").strip()), "")
        stages = {str(r[8] or "new") for r in rows}
        if filled or "saved" in stages:
            stage = "saved"
        elif "filling" in stages:
            stage = "filling"
        else:
            stage = "new"
        values = (
            filled,
            max(int(r[2] or 0) for r in rows),
            max(int(r[3] or 0) for r in rows),
            max(int(r[4] or 0) for r in rows),
            max(int(r[5] or 0) for r in rows),
            max(int(r[6] or 0) for r in rows),
            rank,
            stage,
            max(int(r[9] or 0) for r in rows),
            max(int(r[10] or 0) for r in rows),
            keep_rowid,
        )
        conn.execute(
            """UPDATE chat_users SET
                   filled_form_text=?, messages=?, score=?, level=?,
                   permission_level=?, can_view_forms=?, rank_name_cache=?,
                   form_stage=?, form_started_at=?, form_saved_at=?
               WHERE rowid=?""",
            values,
        )
        conn.execute(
            "DELETE FROM chat_users WHERE chat_id=? AND user_id=? AND rowid<>?",
            (chat_id, user_id, keep_rowid),
        )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_users_chat_user "
        "ON chat_users(chat_id,user_id)"
    )


def _seed_defaults(conn: sqlite3.Connection, target: Path) -> None:
    seed = Path(__file__).resolve().parents[1] / "database" / "database.db"
    if not seed.exists() or seed.resolve() == target.resolve():
        return
    conn.execute("ATTACH DATABASE ? AS seed", (str(seed),))
    try:
        seed_tables = {r[0] for r in conn.execute("SELECT name FROM seed.sqlite_master WHERE type='table'")}
        for table in SEED_TABLES:
            if table not in seed_tables:
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count:
                continue
            target_cols = [r[1] for r in conn.execute(f'PRAGMA main.table_info("{table}")')]
            seed_cols = {r[1] for r in conn.execute(f'PRAGMA seed.table_info("{table}")')}
            common = [c for c in target_cols if c in seed_cols]
            if not common:
                continue
            qcols = ",".join(f'"{c}"' for c in common)
            conn.execute(f'INSERT INTO main."{table}" ({qcols}) SELECT {qcols} FROM seed."{table}"')
    finally:
        conn.commit()
        conn.execute("DETACH DATABASE seed")


def ensure_database_ready(path: str, *, force: bool = False) -> None:
    absolute = str(Path(path).resolve())
    with _LOCK:
        if absolute in _READY_PATHS and not force:
            return
        target = Path(absolute)
        target.parent.mkdir(parents=True, exist_ok=True)

        # При первом запуске на новом volume переносим не только настройки,
        # а полную встроенную рабочую базу: участников, донаты и статистику.
        # Существующий непустой файл никогда автоматически не перезаписываем.
        seed = Path(__file__).resolve().parents[1] / "database" / "database.db"
        target_is_empty = not target.exists() or target.stat().st_size == 0
        if (
            target_is_empty
            and seed.exists()
            and seed.stat().st_size > 0
            and seed.resolve() != target.resolve()
        ):
            shutil.copy2(seed, target)
        elif not target.exists():
            target.touch()

        conn = sqlite3.connect(absolute, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executescript(SCHEMA)
            _migrate_permission_messages(conn)
            for table, columns in MIGRATION_COLUMNS.items():
                existing = _columns(conn, table)
                for name, definition in columns.items():
                    if name not in existing:
                        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')
            _migrate_chat_users(conn)
            _seed_defaults(conn, target)
            # Старые анкеты автоматически считаются сохранёнными. Записи с /bv
            # без сохранённого текста переводятся в промежуточный этап заполнения.
            conn.execute(
                """UPDATE chat_users
                   SET form_stage='saved',
                       form_saved_at=CASE WHEN form_saved_at > 0 THEN form_saved_at ELSE CAST(strftime('%s','now') AS INTEGER) END
                   WHERE TRIM(COALESCE(filled_form_text,'')) <> ''
                     AND form_stage <> 'saved'"""
            )
            conn.execute(
                """UPDATE chat_users
                   SET form_stage='filling',
                       form_started_at=CASE WHEN form_started_at > 0 THEN form_started_at ELSE CAST(strftime('%s','now') AS INTEGER) END
                   WHERE TRIM(COALESCE(filled_form_text,'')) = ''
                     AND form_stage = 'new'
                     AND EXISTS (
                         SELECT 1 FROM bv_messages b
                         WHERE b.chat_id=chat_users.chat_id
                           AND b.target_user_id=chat_users.user_id
                     )"""
            )
            # Свежие рабочие базы старых версий содержат участников только
            # в chat_users. Заполняем новый каталог админ-панели Telegram ID
            # и количеством сообщений, не перезаписывая данные из CSV.
            conn.execute(
                """INSERT OR IGNORE INTO admin_members(user_id, message_count, imported_at)
                   SELECT user_id, MAX(COALESCE(messages, 0)),
                          CAST(strftime('%s','now') AS INTEGER)
                   FROM chat_users
                   WHERE user_id > 0
                   GROUP BY user_id"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_users_chat_user ON chat_users(chat_id,user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_users_score ON chat_users(chat_id,score DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_variants_campaign ON scheduled_variants(campaign_id,sort_order,id)")
            conn.commit()
            missing = [t for t in ("chat_users", "other", "scheduled_campaigns", "form_text", "levels")
                       if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is None]
            if missing:
                raise RuntimeError("Не созданы обязательные таблицы: " + ", ".join(missing))
        finally:
            conn.close()
        _READY_PATHS.add(absolute)
        print(f"Database ready: path={absolute}")
