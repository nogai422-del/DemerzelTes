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


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _add_missing_columns(
    target: sqlite3.Connection, seed: sqlite3.Connection, table: str
) -> None:
    """Добавляет столбцы из эталонной БД в старую БД Bothost.

    SQLite не поддерживает ADD COLUMN с PRIMARY KEY/UNIQUE. Для обычных
    runtime-полей переносим тип, DEFAULT и NOT NULL, когда это допустимо.
    Если старое хранилище уже содержит строки, NOT NULL без DEFAULT
    добавляется как nullable — это безопаснее, чем падение всей панели.
    """
    quoted = _quote_ident(table)
    target_columns = {row[1] for row in target.execute(f"PRAGMA table_info({quoted})")}
    seed_columns = seed.execute(f"PRAGMA table_info({quoted})").fetchall()
    row_count = target.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]

    for _cid, name, col_type, not_null, default_value, primary_key in seed_columns:
        if name in target_columns:
            continue
        parts = [_quote_ident(name)]
        if col_type:
            parts.append(col_type)
        if default_value is not None:
            parts.extend(["DEFAULT", str(default_value)])
        if not_null and (default_value is not None or row_count == 0):
            parts.append("NOT NULL")
        # PRIMARY KEY нельзя добавлять через ALTER TABLE; такие столбцы
        # встречаются только в новых таблицах, которые создаются целиком.
        target.execute(
            f"ALTER TABLE {quoted} ADD COLUMN {' '.join(parts)}"
        )
        target_columns.add(name)
        print(f"DB migration: added {table}.{name}")


def _merge_seed_schema(target_path: str, seed_path: str) -> None:
    """Дополняет старую persistent-БД таблицами, столбцами и индексами."""
    with sqlite3.connect(target_path, timeout=30) as target, sqlite3.connect(seed_path) as seed:
        target.execute("PRAGMA busy_timeout=30000")
        target.execute("PRAGMA foreign_keys=OFF")
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
        # Сначала создаём только таблицы. Индексы могут ссылаться на столбцы,
        # которых ещё нет в старой базе, поэтому создаются после ALTER TABLE.
        for obj_type, name, _table_name, sql in seed_objects:
            if obj_type == "table" and name not in existing:
                target.execute(sql)
                existing.add(name)
                newly_created.append(name)

        # Для таблиц из старого volume добавляем новые столбцы.
        for table in sorted(existing):
            if table.startswith("sqlite_") or table in newly_created:
                continue
            if seed.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                _add_missing_columns(target, seed, table)

        # Теперь безопасно создаём недостающие индексы.
        for obj_type, _name, _table_name, sql in seed_objects:
            if obj_type != "index":
                continue
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


def initialize_database() -> None:
    """Публичная синхронная миграция для запуска до первого HTTP-запроса."""
    _initialize_database_file()



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
