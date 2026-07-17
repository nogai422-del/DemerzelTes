# Доступ к базе данных и транзакционные контексты.

"""Помощники для безопасной работы с SQLite из бота и веб-панели.

При первом запуске persistent-volume может быть пустым. В таком случае база
создаётся из поставляемого с проектом шаблона. Для частично созданной базы
недостающие таблицы, индексы и начальные строки также переносятся из шаблона,
не затирая пользовательские данные.
"""

import os
import shutil
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = os.getenv("DATA_DIR", "database")
DB_PATH = os.path.join(DATA_DIR, "database.db")
SEED_DB_PATH = BASE_DIR / "database" / "database.db"
_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


def _has_user_tables(path: str) -> bool:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    try:
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
            return bool(row and row[0])
    except sqlite3.DatabaseError:
        return False


def _merge_seed_schema(target_path: str, seed_path: str) -> None:
    """Добавляет отсутствующую схему/настройки, сохраняя имеющиеся данные."""
    with sqlite3.connect(target_path, timeout=15) as target, sqlite3.connect(seed_path) as seed:
        target.execute("PRAGMA busy_timeout=15000")
        existing = {
            row[0]
            for row in target.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        seed_objects = seed.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END
            """
        ).fetchall()

        newly_created: list[str] = []
        for obj_type, name, _table_name, sql in seed_objects:
            if obj_type == "table" and name not in existing:
                target.execute(sql)
                existing.add(name)
                newly_created.append(name)
            elif obj_type == "index":
                # Индексы из sqlite_master могут не содержать IF NOT EXISTS.
                try:
                    target.execute(sql)
                except sqlite3.OperationalError as exc:
                    if "already exists" not in str(exc).lower():
                        raise

        # Начальные значения нужны только для таблиц, созданных сейчас.
        for table in newly_created:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in seed.execute(f"PRAGMA table_info({quoted})")]
            if not columns:
                continue
            col_sql = ", ".join('"' + col.replace('"', '""') + '"' for col in columns)
            rows = seed.execute(f"SELECT {col_sql} FROM {quoted}").fetchall()
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                target.executemany(
                    f"INSERT OR IGNORE INTO {quoted} ({col_sql}) VALUES ({placeholders})",
                    rows,
                )
        target.commit()


def _initialize_database_file() -> None:
    absolute_path = os.path.abspath(DB_PATH)
    if absolute_path in _INITIALIZED_PATHS:
        return

    with _INIT_LOCK:
        if absolute_path in _INITIALIZED_PATHS:
            return
        os.makedirs(os.path.dirname(absolute_path) or ".", exist_ok=True)

        seed_path = str(SEED_DB_PATH)
        if os.path.isfile(seed_path):
            if not _has_user_tables(absolute_path):
                # Пустой volume/файл: переносим рабочую базу целиком.
                shutil.copy2(seed_path, absolute_path)
            else:
                # Существующий volume: только дополняем недостающую схему.
                _merge_seed_schema(absolute_path, seed_path)
        elif not os.path.exists(absolute_path):
            # Аварийный режим: SQLite создаст файл; runtime-миграции модулей
            # всё равно смогут создать свои таблицы.
            Path(absolute_path).touch()

        _INITIALIZED_PATHS.add(absolute_path)


async def _open_conn() -> aiosqlite.Connection:
    _initialize_database_file()
    conn = await aiosqlite.connect(DB_PATH, timeout=15)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA busy_timeout=15000;")
    return conn


@asynccontextmanager
async def db():
    conn = await _open_conn()
    cur = await conn.cursor()
    try:
        yield cur
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await cur.close()
        await conn.close()


async def flush_db():
    conn = await _open_conn()
    try:
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        await conn.commit()
    finally:
        await conn.close()


async def close_db():
    await flush_db()
