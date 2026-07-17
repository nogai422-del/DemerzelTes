# Детектор эмодзи для модерации: обычные и кастомные Telegram-эмодзи.

from typing import Any, Iterable, List

import emoji


# Проверяет пару символов regional indicator как флаг.
async def _is_regional_indicator_flag(e: str) -> bool:
    return (
        len(e) == 2
        and all("\U0001F1E6" <= c <= "\U0001F1FF" for c in e)
    )


# Определяет, является ли символ emoji-флагом (включая regional indicator пары).
async def is_flag_emoji(emoji_char: str) -> bool:
    emoji_data = emoji.EMOJI_DATA.get(emoji_char)
    if not emoji_data:
        return await _is_regional_indicator_flag(emoji_char)

    en = (emoji_data.get("en") or "").lower()
    aliases_list = (
        emoji_data.get("alias")
        or emoji_data.get("aliases")
        or []
    )
    aliases = " ".join(aliases_list).lower()

    if "flag" in en or "flag" in aliases:
        return True

    return await _is_regional_indicator_flag(emoji_char)


# Извлекает обычные Unicode-эмодзи из текста с учетом флагов-суррогатов.
async def extract_emojis(text: str | None) -> List[str]:
    if not text:
        return []

    return [
        match["emoji"]
        for match in emoji.emoji_list(text)
        if not (await is_flag_emoji(match["emoji"]))
    ]


# Считает обычные Unicode-эмодзи в строке.
async def emoji_count(text: str | None) -> int:
    return len(await extract_emojis(text))


def _entity_type(entity: Any) -> str:
    value = getattr(entity, "type", "")
    return str(getattr(value, "value", value))


def _custom_emoji_ranges(entities: Iterable[Any] | None) -> list[tuple[int, int]]:
    """Возвращает диапазоны custom_emoji в UTF-16 code units Telegram."""
    ranges: list[tuple[int, int]] = []
    for entity in entities or []:
        if _entity_type(entity) != "custom_emoji":
            continue
        try:
            start = max(0, int(getattr(entity, "offset")))
            length = max(0, int(getattr(entity, "length")))
        except (TypeError, ValueError, AttributeError):
            continue
        if length:
            ranges.append((start, start + length))
    return ranges


async def emoji_count_with_entities(
    text: str | None,
    entities: Iterable[Any] | None = None,
) -> int:
    """
    Считает обычные и премиальные кастомные Telegram-эмодзи без дублей.

    Telegram хранит смещения entities в UTF-16. Текст внутри custom_emoji
    исключается из обычного Unicode-подсчёта, а каждая entity считается ровно
    одной единицей. Поэтому кастомный смайл нельзя обойти библиотеку ``emoji``.
    """
    if not text:
        return 0

    custom_ranges = _custom_emoji_ranges(entities)
    if not custom_ranges:
        return await emoji_count(text)

    encoded = text.encode("utf-16-le")
    units_count = len(encoded) // 2

    # Для исключения текста объединяем пересекающиеся диапазоны, но количество
    # списываем по числу Telegram entities: одна entity = один кастомный смайл.
    normalized: list[tuple[int, int]] = []
    for start, end in sorted(custom_ranges):
        start = min(start, units_count)
        end = min(max(start, end), units_count)
        if not normalized or start > normalized[-1][1]:
            normalized.append((start, end))
        else:
            prev_start, prev_end = normalized[-1]
            normalized[-1] = (prev_start, max(prev_end, end))

    normal_count = 0
    cursor = 0
    for start, end in normalized:
        if start > cursor:
            segment = encoded[cursor * 2:start * 2].decode(
                "utf-16-le", errors="ignore"
            )
            normal_count += await emoji_count(segment)
        cursor = max(cursor, end)

    if cursor < units_count:
        segment = encoded[cursor * 2:].decode("utf-16-le", errors="ignore")
        normal_count += await emoji_count(segment)

    return len(custom_ranges) + normal_count


async def message_emoji_count(message: Any) -> int:
    """Считает эмодзи в тексте сообщения или подписи к медиа."""
    if getattr(message, "text", None) is not None:
        return await emoji_count_with_entities(
            message.text,
            getattr(message, "entities", None),
        )

    return await emoji_count_with_entities(
        getattr(message, "caption", None),
        getattr(message, "caption_entities", None),
    )


# Совместимость со старым вызовом проверки строки.
async def emoji_handler(text: str | None) -> bool:
    return (await emoji_count(text)) > 0
