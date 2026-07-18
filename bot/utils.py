# Общие утилиты бота: имена, временные сообщения, напоминания и "мудрость".

import os
import random
import html
import re
import time
import aiohttp
from bs4 import BeautifulSoup

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from bot.database import db
from aiogram.types import User, Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from bot.message_queue import bot_send_message, bot_send_photo, bot_send_photo_to_chat


# Собирает отображаемое имя пользователя из Telegram-полей.
async def get_full_name(user: User) -> str:
    first = (user.first_name or "").strip()
    last = (user.last_name or "").strip()
    full = f"{first} {last}".strip()
    if full:
        return full
    if user.username:
        return f"@{user.username}"
    return "Пользователь"


# Безопасно удаляет сообщение, игнорируя несущественные ошибки Telegram API.
async def safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


# Сохраняет данные в базе или кэше.
async def save_timed_message(chat_id: int, message_id: int):
    async with db() as cur:
        await cur.execute(
            """
            INSERT INTO timed_messages (chat_id, message_id, send_time)
            VALUES (?, ?, strftime('%s','now'))
            """,
            (chat_id, message_id),
        )


# Возвращает id старых сообщений бота в чате для последующей очистки.
async def get_old_messages(chat_id: int) -> list[int]:
    async with db() as cur:
        await cur.execute(
            "SELECT message_id FROM old_message WHERE chat_id=?",
            (chat_id,),
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


# Обновляет существующую запись.
async def update_old_message(chat_id: int, message_id: int):
    async with db() as cur:
        await cur.execute(
            "UPDATE old_message SET message_id=? WHERE chat_id=?",
            (message_id, chat_id),
        )

        if cur.rowcount == 0:
            await cur.execute(
                "INSERT INTO old_message (chat_id, message_id) VALUES (?, ?)",
                (chat_id, message_id),
            )


# Отправляет информационное напоминание о правилах с кнопкой и изображением.
async def send_info(message: Message):
    # 1) Читаем текст и кнопку напоминания о правилах из таблицы other.
    async with db() as cur:
        await cur.execute(
            'SELECT rules_text, rules_button, rules_url FROM other LIMIT 1'
        )
        row = await cur.fetchone()
        if not row:
            return

        rules_text_db, rules_button_db, rules_url_db = row

    full_name = await get_full_name(message.from_user)
    text = rules_text_db.format(user=full_name)

    # 2) Берем случайную картинку из vibe_images.
    folder_path = "bot/images/vibe_images"
    image_files = [
        f for f in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, f))
    ]
    if not image_files:
        return

    random_image = random.choice(image_files)

    photo = FSInputFile(os.path.join(folder_path, random_image))

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=rules_button_db, url=rules_url_db)]
        ]
    )

    try:
        # 3) Отправляем фото с подписью и кнопкой в reply к исходному сообщению.
        await bot_send_photo(
            message,
            photo,
            caption=text,
            reply_markup=markup,
            reply_to_message_id=message.message_id
        )
    except Exception as e:
        print("Ошибка отправки send_info:", e)


# Отправляет напоминание о рейтинге с кнопкой и изображением.
async def send_rating_info(message: Message):
    # 1) Читаем текст и кнопку напоминания о рейтинге.
    async with db() as cur:
        await cur.execute(
            'SELECT rating_info_text, rating_button, rating_url FROM other LIMIT 1'
        )
        row = await cur.fetchone()
        if not row:
            return

        rating_text_db, rating_button_db, rating_url_db = row

    full_name = await get_full_name(message.from_user)
    text = rating_text_db.format(user=full_name)

    # 2) Берем случайную картинку из vibe_images.
    folder_path = "bot/images/vibe_images"
    image_files = [
        f for f in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, f))
    ]
    if not image_files:
        return

    random_image = random.choice(image_files)
    photo = FSInputFile(os.path.join(folder_path, random_image))

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=rating_button_db, url=rating_url_db)]
        ]
    )

    try:
        # 3) Отправляем напоминание в reply к исходному сообщению.
        await bot_send_photo(
            message,
            photo,
            caption=text,
            reply_markup=markup,
            reply_to_message_id=message.message_id
        )
    except Exception as e:
        print("Ошибка отправки send_rating_info:", e)


# Отправляет миф/напоминание из настроек с кнопкой и изображением.
async def send_myth(message: Message):
    # 1) Читаем текст и кнопку "мифа" из таблицы other.
    async with db() as cur:
        await cur.execute(
            'SELECT myth_text, myth_button, myth_url FROM other LIMIT 1'
        )
        row = await cur.fetchone()

        if not row:
            return

        myth_text_db, myth_button_db, myth_url_db = row

    full_name = await get_full_name(message.from_user)
    text = myth_text_db.format(user=full_name)

    # 2) Берем случайную картинку из vibe_images.
    folder_path = "bot/images/vibe_images"
    image_files = [
        f for f in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, f))
    ]
    if not image_files:
        return

    random_image = random.choice(image_files)

    photo = FSInputFile(os.path.join(folder_path, random_image))

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=myth_button_db, url=myth_url_db)]
        ]
    )

    try:
        # 3) Отправляем сообщение в reply к исходному сообщению.
        await bot_send_photo(
            message,
            photo,
            caption=text,
            reply_markup=markup,
            reply_to_message_id=message.message_id
        )
    except Exception as e:
        print("Ошибка отправки send_myth:", e)


# Получает и форматирует случайную цитату с randstuff.ru.
async def get_latest_wisdom_randstuff_formatted() -> str | None:
    try:
        url = "https://randstuff.ru/saying/"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                text = await response.text()

        soup = BeautifulSoup(text, "html.parser")
        saying_element = soup.find("div", id="saying")
        if not saying_element:
            return None

        td = saying_element.find("td")
        if not td:
            return None

        quote = td.get_text(" ", strip=True).strip()

        author_span = saying_element.find("span", class_="author")
        author = author_span.get_text(" ", strip=True).strip() if author_span else None

        if author:
            author = author.lstrip(" \t\r\n—–-").strip()

            if quote.endswith(author):
                quote = quote[: -len(author)].rstrip(" \t\r\n—–-").strip()

            quote = quote.rstrip(" \t\r\n—–-").strip()

        if author:
            return f"<i>„{quote}“</i>\n\n— {author}"
        return f"<i>„{quote}“</i>"

    except Exception as e:
        print(f"Ошибка при получении цитаты (randstuff.ru): {e}")
        return None


# Получает и форматирует случайную цитату с citaty.net.
async def get_latest_wisdom_citaty_formatted() -> str | None:
    try:
        url = "https://ru.citaty.net/tsitaty/sluchainaia-tsitata/"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                text = await response.text()

        soup = BeautifulSoup(text, "html.parser")

        quote_tag = soup.find("h1", class_="blockquote-display")
        if not quote_tag:
            return None

        quote = quote_tag.get_text("\n", strip=True).strip()

        if quote.startswith("„") and quote.endswith("“"):
            quote = quote[1:-1].strip()

        author_tag = soup.select_one(".blockquote-origin a")
        author = author_tag.get_text(" ", strip=True).strip() if author_tag else None
        if author:
            author = author.lstrip(" \t\r\n—–-").strip()
            return f"<i>„{quote}“</i>\n\n— {author}"

        return f"<i>„{quote}“</i>"

    except Exception as e:
        print(f"Ошибка при получении цитаты (citaty.net): {e}")
        return None


# Отправляет цитату/мудрость в чат по текущим настройкам.
async def send_wisdom(bot: Bot, chat_id: int) -> bool:
    if chat_id >= 0:
        return False

    # Случайно выбираем источник и пытаемся получить уже форматированную цитату.
    source_name, func = random.choice([
        ("randstuff.ru", get_latest_wisdom_randstuff_formatted),
        ("citaty.net", get_latest_wisdom_citaty_formatted),
    ])

    formatted = await func()
    if not formatted:
        return False

    sent = await bot_send_message(bot, chat_id, formatted, wait=True, parse_mode="HTML")
    if sent is None:
        return False

    print(f"Отправлена мудрость с ресурса: {source_name}")
    return True


# Сохраняет данные в базе или кэше.
async def save_hello_message(chat_id, message_id, send_time):
    async with db() as cur:
        await cur.execute("""
            INSERT INTO hello_messages (chat_id, message_id, send_time)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                message_id = excluded.message_id,
                send_time  = excluded.send_time
        """, (chat_id, message_id, send_time))


# Возвращает id предыдущего приветственного сообщения в чате.
async def get_old_hello_message(chat_id):
    async with db() as cur:
        await cur.execute(
            "SELECT message_id FROM hello_messages WHERE chat_id = ?",
            (chat_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


# Отправляет приветствие пользователю при входе/возврате в чат (по chat_member-событию).
async def send_welcome_for_member(bot: Bot, chat_id: int, user: User) -> bool:
    folder_path = "bot/images/vibe_images"
    image_files = [
        f for f in os.listdir(folder_path)
        if os.path.isfile(os.path.join(folder_path, f))
    ]
    if not image_files:
        return False

    random_image = random.choice(image_files)

    old_message_id = await get_old_hello_message(chat_id)
    if old_message_id:
        try:
            await bot.delete_message(chat_id, old_message_id)
        except Exception as e:
            print(f"Не удалось удалить старое приветствие: {e}")

    async with db() as cur:
        await cur.execute(
            """
            SELECT welcome_text, welcome_button, welcome_url, welcome_button2, welcome_url2
            FROM other
            LIMIT 1
            """
        )
        row = await cur.fetchone()
        if not row:
            return False
        welcome_text_db, welcome_button_db, welcome_url_db, welcome_button2_db, welcome_url2_db = row

    full_name = await get_full_name(user)
    safe_full_name = html.escape(full_name)
    user_html_link = f'<a href="tg://user?id={user.id}">{safe_full_name}</a>'

    try:
        welcome_message = (welcome_text_db or "").format(user=user_html_link)
    except Exception:
        welcome_message = welcome_text_db or ""

    photo = FSInputFile(os.path.join(folder_path, random_image))

    keyboard_rows = []
    if welcome_button_db and welcome_url_db:
        keyboard_rows.append([InlineKeyboardButton(text=welcome_button_db, url=welcome_url_db)])
    if welcome_button2_db and welcome_url2_db:
        keyboard_rows.append([InlineKeyboardButton(text=welcome_button2_db, url=welcome_url2_db)])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None

    try:
        sent = await bot_send_photo_to_chat(
            bot,
            chat_id,
            photo,
            wait=True,
            caption=welcome_message,
            parse_mode="HTML",
            reply_markup=markup,
        )
        await save_hello_message(chat_id, sent.message_id, time.time())
        return True
    except Exception as e:
        print(f"Ошибка отправки приветствия в chat_member: {e}")
        return False


# Проверяет, считается ли участник "фактически в чате" по статусу ChatMember.
def is_in_chat_member(cm) -> bool:
    """
    cm = ChatMember, который вернул bot.get_chat_member(chat_id, user_id)
    """
    if cm.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return False
    if cm.status == ChatMemberStatus.RESTRICTED:
        return getattr(cm, "is_member", True) is True
    return True

# Права, которые отличают настоящего модератора от декоративного
# Telegram-администратора с зелёным тегом и без полномочий.
_COMMAND_ADMIN_RIGHTS = (
    # Намеренно не учитываем can_manage_chat/can_invite_users/can_change_info:
    # их иногда оставляют у декоративного администратора ради зелёного тега.
    "can_delete_messages",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_pin_messages",
    "can_manage_topics",
    "can_post_messages",
    "can_edit_messages",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
)


def has_command_admin_rights(member, *, user_id: int | None = None, owner_id: int | None = None) -> bool:
    """Проверяет право использовать административные команды бота.

    Владелец из ``COSMOS_ID`` и создатель чата допускаются всегда. Обычный
    Telegram-администратор допускается только при наличии хотя бы одного
    реального права управления. Один декоративный зелёный тег команды не
    включает.
    """
    if owner_id is not None and user_id is not None:
        try:
            if int(user_id) == int(owner_id):
                return True
        except (TypeError, ValueError):
            pass

    status = getattr(member, "status", None)
    if status == ChatMemberStatus.CREATOR:
        return True
    if status != ChatMemberStatus.ADMINISTRATOR:
        return False

    return any(bool(getattr(member, right, False)) for right in _COMMAND_ADMIN_RIGHTS)


async def is_command_admin(bot: Bot, chat_id: int, user_id: int, *, owner_id: int | None = None) -> bool:
    """Загружает участника и применяет проверку реальных админ-прав."""
    try:
        member = await bot.get_chat_member(int(chat_id), int(user_id))
    except Exception:
        return owner_id is not None and int(user_id) == int(owner_id)
    return has_command_admin_rights(member, user_id=user_id, owner_id=owner_id)


def resolve_bot_image_path(path: str) -> str:
    """Возвращает существующий файл; поддерживает JPG-копию, созданную панелью."""
    candidate = os.path.normpath(path)
    if os.path.isfile(candidate):
        return candidate
    root, ext = os.path.splitext(candidate)
    if ext.lower() in {".png", ".gif", ".jpeg"}:
        jpg = root + ".jpg"
        if os.path.isfile(jpg):
            return jpg
    return candidate


def normalize_telegram_button_url(value: str | None) -> str:
    """Нормализует URL для InlineKeyboardButton и отбрасывает опасные значения."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    if raw.startswith("@"):
        username = raw[1:].strip()
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return f"https://t.me/{username}"
        return ""

    lowered = raw.lower()
    if lowered.startswith("t.me/") or lowered.startswith("www.t.me/"):
        return "https://" + raw
    if lowered.startswith(("https://", "http://", "tg://")):
        return raw
    return ""


def telegram_html_to_plain_text(value: str | None) -> str:
    """Делает читаемый plain-text fallback, если Telegram отклонил HTML."""
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)
    text = re.sub(r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote)(?:\s+[^>]*)?>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()
