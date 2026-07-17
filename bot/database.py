# Доступ к базе данных и транзакционные контексты.

"""
Помощники для работы с базой данных.

Использование:
    async with db() as cur:
        await cur.execute(...)

Каждый вызов db() открывает отдельное подключение SQLite.
Это позволяет избежать проблем между event loop и потоками,
когда bot и web работают одновременно.
"""

import os
from contextlib import asynccontextmanager

import aiosqlite

DATA_DIR = os.getenv("DATA_DIR", "database")
DB_PATH = os.path.join(DATA_DIR, "database.db")


# Открывает новое подключение SQLite и применяет PRAGMA-настройки.
async def _open_conn() -> aiosqlite.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    return conn


# Контекст БД: выдает курсор и завершает транзакцию через commit/rollback.
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


# Принудительно сбрасывает WAL в основной файл базы.
async def flush_db():
    conn = await _open_conn()
    try:
        await conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        await conn.commit()
    finally:
        await conn.close()


# Завершает работу с БД при остановке приложения.
async def close_db():
    """
    Совместимый хук остановки приложения.
    При короткоживущих подключениях достаточно выполнить flush WAL.
    """
    await flush_db()
