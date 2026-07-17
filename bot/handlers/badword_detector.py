# Детектор мата: словарь + устойчивость к типовым обходам для кириллической базы.

import re
import time
import unicodedata
from pathlib import Path
from typing import Literal

BADWORDS_PREFIXES_PATH = Path(__file__).with_name("badwords_prefixes.txt")
BADWORDS_EXACT_PATH = Path(__file__).with_name("badwords_exact.txt")


def _load_words_file(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return [line.strip().lower() for line in f if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def _load_badwords_sources() -> tuple[list[str], list[str]]:
    prefixes = _load_words_file(BADWORDS_PREFIXES_PATH)
    exact_words = _load_words_file(BADWORDS_EXACT_PATH)

    return prefixes, exact_words


# Единый словарь эквивалентов для кириллической основы.
CANON_EQUIV: dict[str, tuple[str, ...]] = {
    "а": ("а", "a", "@", "4"),
    "б": ("б", "b", "6"),
    "в": ("в", "v", "w"),
    "г": ("г", "g", "ґ", "r"),
    "д": ("д", "d", "g", "9"),
    "е": ("е", "ё", "є", "e", "3", "yo", "jo", "ye", "je"),
    "ж": ("ж", "zh"),
    "з": ("з", "z", "3"),
    "и": ("и", "u", "і", "ї", "i", "1", "!"),
    "й": ("й", "u", "y", "j"),
    "к": ("к", "k"),
    "л": ("л", "l"),
    "м": ("м", "m"),
    "н": ("н", "n", "h"),
    "о": ("о", "o", "0", "()"),
    "п": ("п", "p", "n"),
    "р": ("р", "r", "p"),
    "с": ("с", "c", "s", "$"),
    "т": ("т", "t", "7", "+"),
    "у": ("у", "y", "u"),
    "ф": ("ф", "f", "ph"),
    "х": ("х", "x", "h", "kh"),
    "ц": ("ц", "c", "ts", "tc"),
    "ч": ("ч", "ch", "4"),
    "ш": ("ш", "w", "sh"),
    "щ": ("щ", "w", "shch", "sch"),
    "ы": ("ы", "y", "i", "bi", "bl"),
    "э": ("э", "e"),
    "ю": ("ю", "yu", "iu", "ju"),
    "я": ("я", "ya", "ia", "ja", "9", "9i", "9l"),
    "ь": ("ь", "b", "'"),
    "ъ": ("ъ", "b", "'"),
}

WORD_CHARS_RE = r"a-zа-яёіїєґ0-9"
TOKENS_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ſА-Яа-яЁёІіЇїЄєҐґ0-9@#$+!|']{3,}")
# Разрешаем немного "мусора" между буквами для обходов типа "бл*я_дь", но без пробелов.
NOISE_RE = r"(?:[^\w\s]|_|[0-9]){0,2}"


def _build_maps(
    equiv: dict[str, tuple[str, ...]],
    forced_chars: str,
    yo_alias: str | None = None,
) -> tuple[
    dict[str, str],
    dict[str, tuple[str, ...]],
    tuple[tuple[str, str], ...],
    dict[str, tuple[str, ...]],
]:
    single_candidates: dict[str, set[str]] = {}
    multi_to_canon: dict[str, str] = {}
    regex_variants: dict[str, tuple[str, ...]] = {}

    for canon, variants in equiv.items():
        uniq = tuple(dict.fromkeys(v.lower() for v in variants))
        regex_variants[canon] = uniq

        for v in uniq:
            if len(v) == 1:
                single_candidates.setdefault(v, set()).add(canon)
            else:
                multi_to_canon[v] = canon

    single_map: dict[str, str] = {}
    ambig_single_map: dict[str, tuple[str, ...]] = {}
    for alias, canons in single_candidates.items():
        if len(canons) == 1:
            single_map[alias] = next(iter(canons))
        else:
            ambig_single_map[alias] = tuple(sorted(canons))

    for c in forced_chars:
        if yo_alias and c == "ё":
            single_map[c] = yo_alias
        else:
            single_map[c] = c

    multi_seq = tuple(sorted(multi_to_canon.items(), key=lambda item: len(item[0]), reverse=True))
    return single_map, ambig_single_map, multi_seq, regex_variants


CYR_SINGLE_MAP, CYR_AMBIG_SINGLE_MAP, CYR_MULTI_SEQ, CYR_REGEX_VARIANTS = _build_maps(
    CANON_EQUIV,
    forced_chars="абвгдеёжзийклмнопрстуфхцчшщыэюяьъіїєґ",
    yo_alias="е",
)


def _preprocess_text(text: str, multi_seq: tuple[tuple[str, str], ...]) -> str:
    t = text.lower()
    # Приводим экзотические апострофы к одному виду.
    for q in ("’", "ʼ", "ʻ", "ʹ", "`", "´", "՚"):
        t = t.replace(q, "'")

    # Снимаем диакритику (польская/евро-латиница): ą->a, ć->c, ś->s, ź/ż->z и т.д.
    # Отдельно обрабатываем ł, т.к. он не всегда раскладывается в NFKD как хотелось бы.
    t = t.replace("ł", "l")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))

    for src, dst in multi_seq:
        t = t.replace(src, dst)
    return t


def _normalize(text: str, single_map: dict[str, str], multi_seq: tuple[tuple[str, str], ...]) -> str:
    t = _preprocess_text(text, multi_seq)

    out: list[str] = []
    for ch in t:
        canon = single_map.get(ch)
        if canon:
            out.append(canon)

    return "".join(out)


def _normalize_to_cyr(text: str) -> str:
    return _normalize(text, CYR_SINGLE_MAP, CYR_MULTI_SEQ)


def _normalized_candidates(text: str):
    t = _preprocess_text(text, CYR_MULTI_SEQ)
    options_per_char: list[tuple[str, ...]] = []

    for ch in t:
        if ch in CYR_SINGLE_MAP:
            options_per_char.append((CYR_SINGLE_MAP[ch],))
        elif ch in CYR_AMBIG_SINGLE_MAP:
            options_per_char.append(CYR_AMBIG_SINGLE_MAP[ch])

    if not options_per_char:
        return

    # Ленивый перебор всех комбинаций без лимита:
    # вызывающий код завершает цикл сразу после первого совпадения.
    seen: set[str] = set()

    def _gen(idx: int, parts: list[str]):
        if idx >= len(options_per_char):
            candidate = "".join(parts)
            if candidate and candidate not in seen:
                seen.add(candidate)
                yield candidate
            return

        for opt in options_per_char[idx]:
            parts.append(opt)
            yield from _gen(idx + 1, parts)
            parts.pop()

    yield from _gen(0, [])


def _char_pattern(ch: str, regex_variants: dict[str, tuple[str, ...]]) -> str:
    variants = regex_variants.get(ch, (ch,))
    parts = sorted({re.escape(v) for v in variants}, key=len, reverse=True)

    if ch in {"ь", "ъ"}:
        return rf"(?:{'|'.join(parts)})?"

    return rf"(?:{'|'.join(parts)})"


def _build_badword_regex(words: list[str], *, prefix_mode: bool = False) -> re.Pattern[str] | None:
    patterns: list[str] = []

    for w in words:
        normalized = _normalize_to_cyr(w)
        if len(normalized) < 3:
            continue

        body = NOISE_RE.join(_char_pattern(ch, CYR_REGEX_VARIANTS) for ch in normalized)
        if prefix_mode:
            patterns.append(rf"(?<![{WORD_CHARS_RE}]){body}[{WORD_CHARS_RE}]*")
        else:
            patterns.append(rf"(?<![{WORD_CHARS_RE}]){body}(?![{WORD_CHARS_RE}])")

    if not patterns:
        return None

    patterns.sort(key=len, reverse=True)
    return re.compile("|".join(patterns), re.IGNORECASE)


def _build_fallback_set(words: list[str]) -> set[str]:
    cyr_set: set[str] = set()
    for w in words:
        cyr = _normalize_to_cyr(w)
        if len(cyr) >= 3:
            cyr_set.add(cyr)

    return cyr_set


def _build_display_lookup(words: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for w in words:
        original = w.strip().lower()
        if not original:
            continue
        norm = _normalize_to_cyr(original)
        if len(norm) < 3:
            continue
        lookup.setdefault(norm, original)
    return lookup


def _squeeze_repeats(token: str) -> str:
    return re.sub(r"(.)\1{1,}", r"\1", token)


def _match_prefix_canonical(norm_cyr: str) -> str | None:
    matched = [prefix for prefix in prefix_fallback_cyr if norm_cyr.startswith(prefix)]
    if not matched:
        return None
    return max(matched, key=len)


prefix_words, exact_words = _load_badwords_sources()

prefix_badwords_pattern = _build_badword_regex(prefix_words, prefix_mode=True)
exact_badwords_pattern = _build_badword_regex(exact_words, prefix_mode=False)
prefix_fallback_cyr = _build_fallback_set(prefix_words)
exact_fallback_cyr = _build_fallback_set(exact_words)
prefix_display_lookup = _build_display_lookup(prefix_words)
exact_display_lookup = _build_display_lookup(exact_words)

_last_prefixes_mtime = BADWORDS_PREFIXES_PATH.stat().st_mtime if BADWORDS_PREFIXES_PATH.exists() else None
_last_exact_mtime = BADWORDS_EXACT_PATH.stat().st_mtime if BADWORDS_EXACT_PATH.exists() else None
_last_mtime_check_ts = 0.0
_MTIME_CHECK_INTERVAL = 2.0


def _reload_badwords_if_needed() -> None:
    global prefix_words, exact_words
    global prefix_badwords_pattern, exact_badwords_pattern
    global prefix_fallback_cyr, exact_fallback_cyr
    global prefix_display_lookup, exact_display_lookup
    global _last_prefixes_mtime, _last_exact_mtime, _last_mtime_check_ts

    now = time.monotonic()
    if now - _last_mtime_check_ts < _MTIME_CHECK_INTERVAL:
        return
    _last_mtime_check_ts = now

    prefixes_mtime = BADWORDS_PREFIXES_PATH.stat().st_mtime if BADWORDS_PREFIXES_PATH.exists() else None
    exact_mtime = BADWORDS_EXACT_PATH.stat().st_mtime if BADWORDS_EXACT_PATH.exists() else None

    if (
        prefixes_mtime == _last_prefixes_mtime
        and exact_mtime == _last_exact_mtime
    ):
        return

    prefix_words, exact_words = _load_badwords_sources()
    prefix_badwords_pattern = _build_badword_regex(prefix_words, prefix_mode=True)
    exact_badwords_pattern = _build_badword_regex(exact_words, prefix_mode=False)
    prefix_fallback_cyr = _build_fallback_set(prefix_words)
    exact_fallback_cyr = _build_fallback_set(exact_words)
    prefix_display_lookup = _build_display_lookup(prefix_words)
    exact_display_lookup = _build_display_lookup(exact_words)

    _last_prefixes_mtime = prefixes_mtime
    _last_exact_mtime = exact_mtime


async def detect_badword_details(
    text: str | None,
) -> tuple[str, str, Literal["prefix", "exact"]] | None:
    if not text:
        return None

    _reload_badwords_if_needed()

    if prefix_badwords_pattern:
        match = prefix_badwords_pattern.search(text)
        if match:
            trigger_text = match.group(0)
            for norm_raw in _normalized_candidates(trigger_text):
                norm_squeezed = _squeeze_repeats(norm_raw)
                canonical_prefix = (
                    _match_prefix_canonical(norm_raw)
                    or _match_prefix_canonical(norm_squeezed)
                )
                if canonical_prefix:
                    return trigger_text, prefix_display_lookup.get(canonical_prefix, canonical_prefix), "prefix"

    if exact_badwords_pattern:
        match = exact_badwords_pattern.search(text)
        if match:
            trigger_text = match.group(0)
            for norm_raw in _normalized_candidates(trigger_text):
                norm_squeezed = _squeeze_repeats(norm_raw)
                if norm_raw in exact_fallback_cyr:
                    return trigger_text, exact_display_lookup.get(norm_raw, norm_raw), "exact"
                if norm_squeezed in exact_fallback_cyr:
                    return trigger_text, exact_display_lookup.get(norm_squeezed, norm_squeezed), "exact"

    for token in TOKENS_RE.findall(text.lower()):
        for norm_raw in _normalized_candidates(token):
            norm_squeezed = _squeeze_repeats(norm_raw)
            if len(norm_raw) >= 3 or len(norm_squeezed) >= 3:
                if norm_raw in exact_fallback_cyr:
                    return token, exact_display_lookup.get(norm_raw, norm_raw), "exact"
                if norm_squeezed in exact_fallback_cyr:
                    return token, exact_display_lookup.get(norm_squeezed, norm_squeezed), "exact"
                prefix = _match_prefix_canonical(norm_raw) or _match_prefix_canonical(norm_squeezed)
                if prefix:
                    return token, prefix_display_lookup.get(prefix, prefix), "prefix"

    return None


# Возвращает фрагмент текста, по которому сработал детектор, либо None.
async def detect_badword(text: str | None) -> str | None:
    details = await detect_badword_details(text)
    return details[0] if details else None


# Проверяет текст на наличие запрещенных слов.
async def badword_handler(text: str | None) -> bool:
    return (await detect_badword_details(text)) is not None
