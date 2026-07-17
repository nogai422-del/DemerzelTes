# Плановые сообщения: хранение расписаний и фоновая отправка в чат.

import asyncio
import os
import random
import time
from typing import Any

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from bot.database import db
from bot.message_queue import bot_send_message, bot_send_photo_to_chat

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
BOT_IMAGES_DIR = os.path.join(BASE_DIR, "bot", "images")
SCHEDULED_IMAGES_DIR = os.path.join(BOT_IMAGES_DIR, "scheduled_images")


# Создаёт таблицы планировщика, если их ещё нет.
def _to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    if not value or ":" not in value:
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    if not parts[0].isdigit() or not parts[1].isdigit():
        return None
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour, minute


def _today_timestamp_at(hhmm: str, now_ts: int) -> int | None:
    hm = _parse_hhmm(hhmm)
    if hm is None:
        return None
    lt = time.localtime(now_ts)
    return int(
        time.mktime(
            (
                lt.tm_year,
                lt.tm_mon,
                lt.tm_mday,
                hm[0],
                hm[1],
                0,
                lt.tm_wday,
                lt.tm_yday,
                lt.tm_isdst,
            )
        )
    )


def _timestamp_at_date(hhmm: str, year: int, month: int, day: int) -> int | None:
    hm = _parse_hhmm(hhmm)
    if hm is None:
        return None
    return int(
        time.mktime(
            (
                year,
                month,
                day,
                hm[0],
                hm[1],
                0,
                -1,
                -1,
                -1,
            )
        )
    )


def _tomorrow_ymd(now_ts: int) -> tuple[int, int, int]:
    t = time.localtime(now_ts + 86400)
    return t.tm_year, t.tm_mon, t.tm_mday


def _date_str_from_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _campaign_updated_after(window_ts: int, campaign: dict[str, Any], day_str: str) -> bool:
    updated_raw = campaign.get("updated_at")
    if updated_raw is None:
        return False
    try:
        updated_ts = int(updated_raw)
    except (TypeError, ValueError):
        return False
    return _date_str_from_ts(updated_ts) == day_str and updated_ts > window_ts


def _random_timestamp_between(start_ts: int, end_ts: int) -> int | None:
    if end_ts <= start_ts:
        return None
    return random.randint(start_ts, end_ts)


def _resolve_image_full_path(image_path: str) -> str:
    normalized = (image_path or "").strip().replace("/", os.sep)
    full = os.path.join(BOT_IMAGES_DIR, normalized)
    if os.path.isfile(full):
        return full

    base, _ext = os.path.splitext(full)
    jpg_fallback = base + ".jpg"
    if os.path.isfile(jpg_fallback):
        return jpg_fallback

    return full


def _build_keyboard(button_text: str, button_url: str) -> InlineKeyboardMarkup | None:
    if not button_text or not button_url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
    )


# Удаляет чат из last_message, если бот больше не может писать в этот чат.
async def _drop_chat_from_last_message(chat_id: int) -> None:
    async with db() as cur:
        await cur.execute("DELETE FROM last_message WHERE chat_id = ?", (chat_id,))


# Определяет, нужно ли удалять чат из last_message по тексту ошибки.
def _must_drop_chat_by_error(error_message: str) -> bool:
    msg = (error_message or "").lower()
    return (
        "group chat was upgraded to a supergroup chat" in msg
        or "chat not found" in msg
        or "group chat was deleted" in msg
        or "forbidden" in msg
        or "not enough rights to send text messages to the chat" in msg
    )


def _choose_content(
    campaign: dict[str, Any], variants: list[dict[str, Any]]
) -> tuple[str, str, str, str]:
    if not variants:
        return "", "", "", ""

    base_variant = variants[0]
    variant_candidates = [
        v
        for v in variants
        if (v.get("text") or "").strip() or (v.get("image_path") or "").strip()
    ]
    if int(campaign.get("random_text_mode") or 0) == 1 and variant_candidates:
        base_variant = random.choice(variant_candidates)

    text = (base_variant.get("text") or "").strip()
    image_path = (base_variant.get("image_path") or "").strip()
    button_text = (base_variant.get("button_text") or "").strip()
    button_url = (base_variant.get("button_url") or "").strip()

    return text, image_path, button_text, button_url


# Отправляет одно сообщение кампании в Telegram.
async def _send_campaign_to_chat(
    bot: Bot, chat_id: int, campaign: dict[str, Any], variants: list[dict[str, Any]]
) -> bool:
    if chat_id >= 0:
        return False

    text, image_path, button_text, button_url = _choose_content(campaign, variants)
    keyboard = _build_keyboard(button_text, button_url)

    if not text and not image_path:
        return False

    try:
        if image_path:
            full_path = _resolve_image_full_path(image_path)
            if not os.path.isfile(full_path):
                print(f"Файл кампании не найден: {full_path}")
                return False

            photo = FSInputFile(full_path)
            sent_photo = await bot_send_photo_to_chat(
                bot,
                chat_id,
                photo,
                wait=True,
                caption=text or None,
                parse_mode="HTML" if text else None,
                reply_markup=keyboard,
            )
            return sent_photo is not None

        sent_msg = await bot_send_message(
            bot,
            chat_id,
            text,
            wait=True,
            parse_mode="HTML",
            disable_web_page_preview=False,
            reply_markup=keyboard,
        )
        return sent_msg is not None
    except Exception as e:
        error_message = str(e)
        if _must_drop_chat_by_error(error_message):
            try:
                await _drop_chat_from_last_message(chat_id)
                print(f"Чат {chat_id} недоступен для scheduled — удалён из last_message.")
            except Exception as drop_err:
                print(f"Не удалось удалить чат {chat_id} из last_message: {drop_err}")
        print(
            f"Ошибка отправки scheduled campaign id={campaign.get('id')} chat_id={chat_id}: {e}"
        )
        return False


# Собирает список чатов из таблицы last_message.
async def _collect_known_chat_ids() -> list[int]:
    chat_ids: list[int] = []

    try:
        async with db() as cur:
            # Берем чаты тем же источником, что и wisdom_loop.
            await cur.execute("SELECT chat_id FROM last_message")
            rows = await cur.fetchall()
    except Exception as e:
        print(f"Не удалось прочитать last_message для scheduled: {e}")
        return []

    for row in rows:
        try:
            chat_id = int(row[0])
        except (TypeError, ValueError):
            continue
        if chat_id >= 0:
            continue
        chat_ids.append(chat_id)

    return chat_ids


# Находит кампании, которые нужно отправить сегодня.
async def _collect_due_campaigns(now_ts: int, today: str) -> list[dict[str, Any]]:
    due: list[dict[str, Any]] = []

    async with db() as cur:
        await cur.execute(
            """
            SELECT *
            FROM scheduled_campaigns
            WHERE is_enabled = 1
            ORDER BY id ASC
            """
        )
        rows = await cur.fetchall()

        for row in rows:
            campaign = _to_dict(row)
            mode = (campaign.get("time_mode") or "fixed").strip()

            if (campaign.get("last_sent_date") or "") == today:
                continue

            if mode == "fixed":
                target_ts = _today_timestamp_at(campaign.get("fixed_time") or "", now_ts)
                if target_ts is None:
                    continue
                # Без "догоняния": если кампанию обновили после сегодняшнего времени отправки,
                # первую отправку переносим на завтра.
                if _campaign_updated_after(target_ts, campaign, today):
                    continue
                if now_ts >= target_ts:
                    due.append(campaign)
                continue

            planned_for_date = (campaign.get("planned_for_date") or "").strip()
            planned_send_ts = campaign.get("planned_send_ts")
            planned_send_ts = int(planned_send_ts) if planned_send_ts is not None else None

            if planned_for_date != today or planned_send_ts is None:
                range_start = campaign.get("range_start") or ""
                range_end = campaign.get("range_end") or ""
                start_today_ts = _today_timestamp_at(range_start, now_ts)
                end_today_ts = _today_timestamp_at(range_end, now_ts)
                if start_today_ts is None or end_today_ts is None or end_today_ts <= start_today_ts:
                    continue

                # Без "догоняния": если уже поздно для сегодняшнего окна, планируем на завтра.
                if now_ts >= end_today_ts or _campaign_updated_after(end_today_ts, campaign, today):
                    y, m, d = _tomorrow_ymd(now_ts)
                    tomorrow = f"{y:04d}-{m:02d}-{d:02d}"
                    start_tomorrow_ts = _timestamp_at_date(range_start, y, m, d)
                    end_tomorrow_ts = _timestamp_at_date(range_end, y, m, d)
                    if (
                        start_tomorrow_ts is None
                        or end_tomorrow_ts is None
                        or end_tomorrow_ts <= start_tomorrow_ts
                    ):
                        continue
                    planned_for_date = tomorrow
                    planned_send_ts = _random_timestamp_between(start_tomorrow_ts, end_tomorrow_ts)
                else:
                    # Если попали в текущее окно, выбираем случайный момент от "сейчас" до конца окна.
                    planned_for_date = today
                    planned_send_ts = _random_timestamp_between(max(now_ts, start_today_ts), end_today_ts)

                if planned_send_ts is None:
                    continue
                await cur.execute(
                    """
                    UPDATE scheduled_campaigns
                    SET planned_for_date = ?, planned_send_ts = ?, updated_at = CAST(strftime('%s','now') AS INTEGER)
                    WHERE id = ?
                    """,
                    (planned_for_date, planned_send_ts, int(campaign["id"])),
                )

            if now_ts >= planned_send_ts:
                due.append(campaign)

    return due


# Выполняет один тик планировщика: проверка и отправка всех due-кампаний.
async def process_scheduled_messages_tick(bot: Bot) -> None:
    now_ts = int(time.time())
    today = time.strftime("%Y-%m-%d", time.localtime(now_ts))
    due_campaigns = await _collect_due_campaigns(now_ts, today)
    target_chat_ids = await _collect_known_chat_ids()

    if not target_chat_ids:
        return

    for campaign in due_campaigns:
        campaign_id = int(campaign["id"])

        async with db() as cur:
            await cur.execute(
                """
                SELECT id, campaign_id, sort_order, text, image_path, button_text, button_url
                FROM scheduled_variants
                WHERE campaign_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (campaign_id,),
            )
            variants = [_to_dict(v) for v in await cur.fetchall()]

        sent_any = False
        for chat_id in target_chat_ids:
            sent = await _send_campaign_to_chat(bot, chat_id, campaign, variants)
            if sent:
                sent_any = True

        if not sent_any:
            continue

        async with db() as cur:
            await cur.execute(
                """
                UPDATE scheduled_campaigns
                SET last_sent_date = ?, updated_at = CAST(strftime('%s','now') AS INTEGER)
                WHERE id = ?
                """,
                (today, campaign_id),
            )


# Фоновый цикл плановых сообщений.
async def scheduled_messages_loop(bot: Bot) -> None:
    while True:
        try:
            await process_scheduled_messages_tick(bot)
        except Exception as e:
            print(f"Ошибка в scheduled_messages_loop: {e}")
        await asyncio.sleep(20)
