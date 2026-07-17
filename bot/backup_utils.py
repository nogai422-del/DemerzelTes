# Резервное копирование проекта: упаковка, нарезка и отправка в backup-канал.

import os
import zipfile
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import FSInputFile
from dotenv import load_dotenv

from bot.database import DB_PATH, flush_db
from bot.message_queue import bot_send_document
from env_config import require_int_env

load_dotenv()


def get_backup_channel_id() -> int:
    return require_int_env("BACKUP_CHANNEL_ID")

PROJECT_ROOT = "."
BACKUP_DIR = "backup"
DB_FILE = DB_PATH

MAX_FILE_SIZE = 48 * 1024 * 1024  # 48 МБ

EXCLUDE_DIRS = {
    "venv",
    "__pycache__",
    "backup",
}

EXCLUDE_FILES = {".env"}


# Упаковывает базу и медиафайлы в zip-архив.
async def create_zip(root_folder: str, output_file: str):

    # Фоново создает архив и кладет путь в очередь результата.
    def _create():
        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)

        tmp_file = output_file + ".tmp"

        root_abs = os.path.abspath(root_folder)
        out_abs = os.path.abspath(output_file)
        tmp_abs = os.path.abspath(tmp_file)

        with zipfile.ZipFile(tmp_file, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(root_folder, topdown=True):
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

                for file_name in files:
                    if file_name in EXCLUDE_FILES:
                        continue

                    filepath = os.path.join(root, file_name)
                    file_abs = os.path.abspath(filepath)

                    if file_abs == out_abs or file_abs == tmp_abs:
                        continue

                    try:
                        arcname = os.path.relpath(file_abs, start=root_abs)
                        zipf.write(file_abs, arcname)
                    except (FileNotFoundError, PermissionError, OSError) as e:
                        print(f"Пропущен файл (недоступен/исчез): {filepath} ({type(e).__name__}: {e})")

        os.replace(tmp_file, output_file)

    await asyncio.to_thread(_create)
    print("Архив создан:", output_file)


# Разбивает большой файл на части заданного размера.
async def split_file(file_path: str, chunk_size: int = MAX_FILE_SIZE):

    # Фоново выполняет нарезку файла и возвращает список частей.
    def _split():
        parts = []
        with open(file_path, "rb") as f:
            i = 1
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                part_name = f"{file_path}.part{i}"
                with open(part_name, "wb") as pf:
                    pf.write(chunk)
                parts.append(part_name)
                i += 1
        return parts

    return await asyncio.to_thread(_split)


# Отправляет файл (или его части) в backup-канал Telegram.
async def send_file(bot: Bot, file_path: str, caption: str = None):
    channel_id = get_backup_channel_id()
    if not os.path.exists(file_path):
        print("Файл не найден:", file_path)
        return

    file_size = os.path.getsize(file_path)

    if file_size <= MAX_FILE_SIZE:
        try:
            file = FSInputFile(file_path)
            if caption is not None:
                await bot_send_document(bot, channel_id, file, wait=True, caption=caption)
            else:
                await bot_send_document(bot, channel_id, file, wait=True)
            print("Файл успешно отправлен:", file_path)
        except Exception as e:
            print(f"Ошибка при отправке файла {file_path}: {e}")
        return

    parts = await split_file(file_path)
    print(f"Файл большой, делим на {len(parts)} частей")

    for part in parts:
        try:
            file = FSInputFile(part)
            if caption is not None:
                await bot_send_document(bot, channel_id, file, wait=True, caption=caption)
            else:
                await bot_send_document(bot, channel_id, file, wait=True)
            print("Отправлена часть:", part)
        except Exception as e:
            print(f"Ошибка при отправке части {part}: {e}")
        finally:
            try:
                await asyncio.to_thread(os.remove, part)
            except Exception:
                pass


# Запускает основной рабочий цикл.
async def run_daily(bot: Bot, hour: int = 0, minute: int = 0):
    """
    Первый запуск — ждём до указанного времени.
    Дальше — раз в сутки.
    """
    try:
        get_backup_channel_id()
    except RuntimeError as e:
        print(f"Бэкап отключён: {e}")
        return

    now = datetime.now()
    first_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if first_run <= now:
        first_run += timedelta(days=1)

    delay = (first_run - now).total_seconds()
    print(f"Ждем {int(delay)} секунд до следующего резервного копирования")
    await asyncio.sleep(delay)

    while True:
        os.makedirs(BACKUP_DIR, exist_ok=True)

        db_caption = f"Резервная копия базы данных {datetime.now().strftime('%Y-%m-%d')}"
        await flush_db()
        await send_file(bot, DB_FILE, caption=db_caption)

        archive_path = os.path.join(BACKUP_DIR, "backup.zip")
        await create_zip(PROJECT_ROOT, archive_path)

        archive_caption = f"Резервная копия проекта {datetime.now().strftime('%Y-%m-%d')}"
        await send_file(bot, archive_path, caption=archive_caption)

        print("Бэкап завершён, спим сутки")
        await asyncio.sleep(24 * 60 * 60)
