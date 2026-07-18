"""Надёжная доставка карточек уведомлений с безопасной обработкой картинок.

Картинка является необязательным украшением. Любая ошибка чтения, конвертации
или отправки медиа никогда не должна отменять текст уведомления.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from aiogram.types import FSInputFile
from PIL import Image, ImageOps, UnidentifiedImageError

from bot.message_queue import (
    bot_send_animation_to_chat,
    bot_send_message,
    bot_send_photo_to_chat,
)

BASE_DIR = Path(__file__).resolve().parents[1]
BOT_IMAGES_DIR = (BASE_DIR / "bot" / "images").resolve()
DATA_DIR = Path(os.getenv("DATA_DIR", "database")).resolve()
PERSISTENT_UPLOAD_ROOT = (DATA_DIR / "uploads").resolve()
CACHE_DIR = (DATA_DIR / ".notification_cache").resolve()
PERSISTENT_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Ограничения взяты с запасом относительно Telegram Bot API.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ANIMATION_BYTES = 18 * 1024 * 1024
MAX_PHOTO_BYTES = 9 * 1024 * 1024
MAX_SIDE = 1920
MAX_PIXELS = 40_000_000
MAX_ASPECT_RATIO = 19.0
JPEG_QUALITY_STEPS = (90, 84, 76, 68)
CAPTION_SAFE_LIMIT = 950  # Telegram допускает 1024, оставляем запас.

ALLOWED_UPLOAD_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}


@dataclass(frozen=True)
class PreparedNotificationMedia:
    primary_kind: str | None = None  # photo | animation | None
    primary_path: str | None = None
    fallback_photo_path: str | None = None
    source_path: str | None = None
    error: str | None = None


def notification_upload_dir(category: str) -> Path:
    """Возвращает постоянную папку загрузок внутри DATA_DIR."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(category or "images"))
    directory = (PERSISTENT_UPLOAD_ROOT / safe).resolve()
    directory.relative_to(PERSISTENT_UPLOAD_ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolve_notification_source_path(image_path: str | None) -> Path | None:
    """Ищет картинку сначала в постоянном volume, затем среди bundled-файлов."""
    raw = str(image_path or "").strip().replace("\\", "/")
    if not raw:
        return None
    raw = raw.lstrip("/")

    for root in (PERSISTENT_UPLOAD_ROOT, BOT_IMAGES_DIR):
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
        if candidate.suffix.lower() in {".png", ".gif", ".jpeg", ".webp"}:
            jpg_fallback = candidate.with_suffix(".jpg")
            if jpg_fallback.is_file():
                return jpg_fallback

    # Возвращаем безопасный ожидаемый путь для понятной диагностики.
    candidate = (PERSISTENT_UPLOAD_ROOT / raw).resolve()
    try:
        candidate.relative_to(PERSISTENT_UPLOAD_ROOT)
    except ValueError:
        return None
    return candidate


def _safe_source_path(image_path: str | None) -> Path | None:
    return resolve_notification_source_path(image_path)


def _cache_path(source: Path, suffix: str = ".jpg") -> Path:
    try:
        stat = source.stat()
        marker = f"{source}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8", "ignore")
    except OSError:
        marker = str(source).encode("utf-8", "ignore")
    digest = hashlib.sha256(marker).hexdigest()[:24]
    return CACHE_DIR / f"{digest}{suffix}"


def _open_verified_image(path: Path) -> Image.Image:
    # verify() обнаруживает усечённые/битые файлы, после него файл открывается заново.
    with Image.open(path) as probe:
        probe.verify()
    image = Image.open(path)
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        image.close()
        raise ValueError("недопустимые размеры изображения")
    # ImageOps.exif_transpose превращает анимированный GIF в один кадр, поэтому
    # ориентацию применяем только к статическим изображениям.
    if bool(getattr(image, "is_animated", False)) and int(getattr(image, "n_frames", 1)) > 1:
        return image
    return ImageOps.exif_transpose(image)


def _first_frame_rgb(image: Image.Image) -> Image.Image:
    try:
        image.seek(0)
    except EOFError:
        pass

    frame = image.convert("RGBA")
    background = Image.new("RGB", frame.size, "white")
    if frame.mode == "RGBA":
        background.paste(frame, mask=frame.getchannel("A"))
    else:
        background.paste(frame)
    return background


def _fit_telegram_photo(image: Image.Image) -> Image.Image:
    image = _first_frame_rgb(image)
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("пустое изображение")

    # Сначала уменьшаем общий размер.
    if max(width, height) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        width, height = image.size

    # Telegram отклоняет экстремальные соотношения сторон. Не обрезаем картинку,
    # а добавляем белые поля.
    ratio = max(width / height, height / width)
    if ratio > MAX_ASPECT_RATIO:
        if width > height:
            target_height = max(1, int(round(width / MAX_ASPECT_RATIO)))
            canvas = Image.new("RGB", (width, target_height), "white")
            canvas.paste(image, (0, (target_height - height) // 2))
        else:
            target_width = max(1, int(round(height / MAX_ASPECT_RATIO)))
            canvas = Image.new("RGB", (target_width, height), "white")
            canvas.paste(image, ((target_width - width) // 2, 0))
        image = canvas

    return image


def _save_safe_jpeg(image: Image.Image, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    image = _fit_telegram_photo(image)

    last_size = 0
    for quality in JPEG_QUALITY_STEPS:
        tmp = target.with_suffix(f".q{quality}.tmp")
        image.save(tmp, format="JPEG", quality=quality, optimize=True, progressive=True)
        last_size = tmp.stat().st_size
        if last_size <= MAX_PHOTO_BYTES:
            os.replace(tmp, target)
            return target
        tmp.unlink(missing_ok=True)

    # 1920px JPEG практически никогда не дойдёт сюда, но оставляем последний
    # безопасный вариант с дополнительным уменьшением.
    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    image.save(target, format="JPEG", quality=62, optimize=True, progressive=True)
    if target.stat().st_size > MAX_PHOTO_BYTES:
        target.unlink(missing_ok=True)
        raise ValueError(f"изображение слишком большое после оптимизации: {last_size} байт")
    return target


def prepare_notification_media(image_path: str | None) -> PreparedNotificationMedia:
    """Проверяет медиа и создаёт Telegram-safe JPG fallback.

    Функция никогда не выбрасывает ошибку наружу: проблема возвращается в поле
    error, а вызывающий код продолжает отправку текста.
    """
    source = _safe_source_path(image_path)
    if source is None:
        return PreparedNotificationMedia()
    if not source.is_file():
        return PreparedNotificationMedia(source_path=str(source), error="файл не найден")

    try:
        if source.stat().st_size <= 0:
            raise ValueError("пустой файл")
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("файл превышает 20 МБ")

        image = _open_verified_image(source)
        image_format = str(image.format or source.suffix.lstrip(".") or "").upper()
        is_animated = bool(getattr(image, "is_animated", False)) and int(
            getattr(image, "n_frames", 1)
        ) > 1

        fallback = _cache_path(source, ".jpg")
        if not fallback.is_file():
            _save_safe_jpeg(image, fallback)

        if image_format == "GIF" and is_animated and source.stat().st_size <= MAX_ANIMATION_BYTES:
            return PreparedNotificationMedia(
                primary_kind="animation",
                primary_path=str(source),
                fallback_photo_path=str(fallback),
                source_path=str(source),
            )

        # Любое статическое изображение отправляем уже нормализованным JPG.
        return PreparedNotificationMedia(
            primary_kind="photo",
            primary_path=str(fallback),
            fallback_photo_path=str(fallback),
            source_path=str(source),
        )
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        return PreparedNotificationMedia(source_path=str(source), error=str(exc))
    except Exception as exc:  # защита от редких ошибок декодера Pillow
        return PreparedNotificationMedia(source_path=str(source), error=f"{type(exc).__name__}: {exc}")


def _plain_text(value: str | None) -> str:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", text, flags=re.I | re.S)
    text = re.sub(
        r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote)(?:\s+[^>]*)?>",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _log(context: str, message: str) -> None:
    print(f"[NOTIFICATION] {context}: {message}")


async def send_notification_card(
    bot: Any,
    *,
    chat_id: int,
    text: str,
    image_path: str | None = None,
    reply_markup: Any = None,
    message_thread_id: int | None = None,
    reply_parameters: Any = None,
    disable_notification: bool = False,
    protect_content: bool = False,
    context: str = "notification",
) -> Any | None:
    """Отправляет уведомление с каскадом fallback.

    Порядок:
    1) безопасное медиа + HTML + кнопка;
    2) fallback-фото + HTML без кнопки;
    3) HTML-текст с кнопкой;
    4) HTML-текст без кнопки;
    5) plain text.

    Ошибка картинки не выходит наружу и не влияет на текст.
    """
    chat_id = int(chat_id)
    html_text = str(text or "").strip()
    plain_text = _plain_text(html_text) or "Это действие сейчас недоступно."

    common_kwargs: dict[str, Any] = {
        "disable_notification": bool(disable_notification),
        "protect_content": bool(protect_content),
    }
    if message_thread_id is not None:
        common_kwargs["message_thread_id"] = int(message_thread_id)
    if reply_parameters is not None:
        common_kwargs["reply_parameters"] = reply_parameters

    prepared = prepare_notification_media(image_path)
    if prepared.error:
        _log(context, f"картинка пропущена ({prepared.error}); отправляем текст")

    # Caption короче обычного сообщения. Если администратор ввёл длинный текст,
    # сразу используем текстовый вариант, не провоцируя Bad Request.
    can_use_caption = len(html_text) <= CAPTION_SAFE_LIMIT

    async def _try_media(kind: str, path: str, *, keyboard: Any) -> Any | None:
        kwargs = dict(common_kwargs)
        kwargs.update({"caption": html_text, "parse_mode": "HTML", "reply_markup": keyboard})
        try:
            if kind == "animation":
                return await bot_send_animation_to_chat(
                    bot, chat_id, FSInputFile(path), wait=True, **kwargs
                )
            return await bot_send_photo_to_chat(
                bot, chat_id, FSInputFile(path), wait=True, **kwargs
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(context, f"не удалось отправить {kind} {os.path.basename(path)}: {exc}")
            return None

    if can_use_caption and prepared.primary_kind and prepared.primary_path:
        sent = await _try_media(prepared.primary_kind, prepared.primary_path, keyboard=reply_markup)
        if sent is not None:
            return sent

        # Если GIF/кнопка/HTML сломали первый вариант — пробуем статичный JPG
        # без кнопки. Для статичного фото повтор не нужен, если путь тот же.
        fallback = prepared.fallback_photo_path
        if fallback and (
            prepared.primary_kind != "photo"
            or os.path.normcase(fallback) != os.path.normcase(prepared.primary_path)
            or reply_markup is not None
        ):
            sent = await _try_media("photo", fallback, keyboard=None)
            if sent is not None:
                return sent

    async def _try_text(*, keyboard: Any, parse_mode: str | None, value: str) -> Any | None:
        kwargs = dict(common_kwargs)
        kwargs.update(
            {
                "reply_markup": keyboard,
                "disable_web_page_preview": True,
            }
        )
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        try:
            return await bot_send_message(bot, chat_id, value, wait=True, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            mode = parse_mode or "plain"
            _log(context, f"не удалось отправить {mode}-текст: {exc}")
            return None

    sent = await _try_text(keyboard=reply_markup, parse_mode="HTML", value=html_text)
    if sent is not None:
        return sent
    if reply_markup is not None:
        sent = await _try_text(keyboard=None, parse_mode="HTML", value=html_text)
        if sent is not None:
            return sent
    return await _try_text(keyboard=None, parse_mode=None, value=plain_text)


def store_notification_upload(
    stream: BinaryIO,
    original_filename: str,
    upload_dir: str | os.PathLike[str],
    *,
    prefix: str = "img",
    preserve_animation: bool = True,
) -> str:
    """Проверяет загруженный файл и сохраняет безопасную версию.

    Возвращает только имя файла. Статические JPG/PNG/WEBP нормализуются в JPG.
    Анимированный GIF сохраняется как GIF, но предварительно полностью
    проверяется Pillow. Его JPEG fallback будет создан при первой отправке.
    """
    filename = str(original_filename or "")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("разрешены JPG, PNG, WEBP и GIF")

    target_dir = Path(upload_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Читаем ограниченный объём, чтобы загрузка не съела память процесса.
    data = stream.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ValueError("файл пустой")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("файл больше 20 МБ")

    digest = hashlib.sha256(data).hexdigest()[:20]
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix)[:32] or "img"

    with tempfile.NamedTemporaryFile(dir=target_dir, suffix=f".{extension}", delete=False) as tmp:
        tmp.write(data)
        temp_path = Path(tmp.name)

    try:
        image = _open_verified_image(temp_path)
        image_format = str(image.format or temp_path.suffix.lstrip(".") or "").upper()
        is_animated = bool(getattr(image, "is_animated", False)) and int(
            getattr(image, "n_frames", 1)
        ) > 1

        if image_format == "GIF" and is_animated and preserve_animation:
            final_name = f"{safe_prefix}_{digest}.gif"
            final_path = target_dir / final_name
            os.replace(temp_path, final_path)
            # Создаём fallback заранее и одновременно проверяем первый кадр.
            _save_safe_jpeg(image, _cache_path(final_path, ".jpg"))
            return final_name

        final_name = f"{safe_prefix}_{digest}.jpg"
        final_path = target_dir / final_name
        _save_safe_jpeg(image, final_path)
        temp_path.unlink(missing_ok=True)
        return final_name
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        temp_path.unlink(missing_ok=True)
        raise ValueError(f"повреждённое или неподдерживаемое изображение: {exc}") from exc
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise ValueError(f"не удалось обработать изображение: {exc}") from exc
