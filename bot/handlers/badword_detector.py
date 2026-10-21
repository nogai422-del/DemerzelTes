# Детектор мата: словарь + устойчивость к типовым обходам для кириллической базы.

import re
import time
import unicodedata
from pathlib import Path
from typing import Literal

BADWORDS_PREFIXES_PATH = Path(__file__).with_name("badwords_prefixes.txt")
BADWORDS_EXACT_PATH = Path(__file__).with_name("badwords_exact.txt")
BADWORDS_ALLOW_PATH = Path(__file__).with_name("badwords_allow.txt")


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

for _canonical, _aliases in {
    'а': ('α',), 'в': ('β',), 'е': ('ε',), 'н': ('η',),
    'и': ('ι',), 'к': ('κ',), 'м': ('μ',), 'о': ('ο',),
    'р': ('ρ',), 'т': ('τ',), 'у': ('υ',), 'х': ('χ',),
    'с': ('ϲ',), 'з': ('ѕ',),
}.items():
    CANON_EQUIV[_canonical] += _aliases



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


def _build_normalized_trie(prefixes, exact):
    nodes = [{}]
    letters = ['']
    terminals = {}
    for kind, words in (('prefix', prefixes), ('exact', exact)):
        for word in sorted(words):
            node = 0
            for char in word:
                if char not in nodes[node]:
                    nodes[node][char] = len(nodes)
                    nodes.append({})
                    letters.append(char)
                node = nodes[node][char]
            terminals.setdefault(node, {})[kind] = word
    return nodes, letters, terminals


MULTI_OPTIONS = {}
for _canonical, _variants in CANON_EQUIV.items():
    for _alias in _variants:
        if len(_alias) > 1:
            MULTI_OPTIONS.setdefault(_alias, set()).add(_canonical)


def _match_normalized(text: str, allowed=('prefix', 'exact'), trie=None):
    # Merge states by input position and dictionary node. Ambiguous one- and
    # multi-character aliases never create a Cartesian product of strings.
    nodes, letters, terminals = _normalized_trie if trie is None else trie
    text = ''.join(c for c in _preprocess_text(text, ())
                   if c in CYR_SINGLE_MAP or c in CYR_AMBIG_SINGLE_MAP)
    # More than two identical letters/digits is an elongation, not a new
    # dictionary spelling. Keep doubles for ordinary Russian morphology.
    text = re.sub(r'(.)\1{2,}', r'\1\1', text)
    pending = {0: ({0}, {0})}
    size = len(text)
    for index in range(size):
        states = pending.pop(index, None)
        if states is None:
            continue
        raw, squeezed = states
        if not raw and not squeezed:
            if not pending:
                return None
            continue
        char = text[index]
        options = CYR_AMBIG_SINGLE_MAP.get(char, ())
        if char in CYR_SINGLE_MAP:
            options = (CYR_SINGLE_MAP[char],)
        steps = [(1, options)]
        for alias, aliases in MULTI_OPTIONS.items():
            if text.startswith(alias, index):
                steps.append((len(alias), aliases))
        if char.isdigit():
            target = pending.setdefault(index+1, (set(), set()))
            target[0].update(raw)
            target[1].update(squeezed)
        for length, choices in steps:
            next_raw, next_squeezed = pending.setdefault(index+length, (set(), set()))
            for option in choices:
                if option == 'й':
                    option = 'и'  # Dictionary normalization folds the breve.
                for node in raw:
                    child = nodes[node].get(option)
                    if child is not None:
                        next_raw.add(child)
                for node in squeezed:
                    if letters[node] == option:
                        next_squeezed.add(node)
                    else:
                        child = nodes[node].get(option)
                        if child is not None:
                            next_squeezed.add(child)
            if 'prefix' in allowed:
                matches = [terminals[node]['prefix'] for node in next_raw | next_squeezed
                           if 'prefix' in terminals.get(node, {})]
                if matches:
                    return max(matches, key=len), 'prefix'
    if 'exact' in allowed:
        raw, squeezed = pending.get(size, (set(), set()))
        for node in sorted(raw | squeezed):
            if 'exact' in terminals.get(node, {}):
                return terminals[node]['exact'], 'exact'
    return None


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


prefix_words, exact_words = _load_badwords_sources()

prefix_fallback_cyr = _build_fallback_set(prefix_words)
exact_fallback_cyr = _build_fallback_set(exact_words)
prefix_display_lookup = _build_display_lookup(prefix_words)
exact_display_lookup = _build_display_lookup(exact_words)
_normalized_trie = _build_normalized_trie(prefix_fallback_cyr, exact_fallback_cyr)

_allow_trie = _build_normalized_trie(set(), _build_fallback_set(_load_words_file(BADWORDS_ALLOW_PATH)))
_last_mtimes = None
_last_mtime_check_ts = 0.0
_MTIME_CHECK_INTERVAL = 2.0


def _reload_badwords_if_needed() -> None:
    global prefix_words, exact_words, prefix_fallback_cyr, exact_fallback_cyr
    global prefix_display_lookup, exact_display_lookup, _normalized_trie, _allow_trie
    global _last_mtimes, _last_mtime_check_ts
    now = time.monotonic()
    if now - _last_mtime_check_ts < _MTIME_CHECK_INTERVAL:
        return
    _last_mtime_check_ts = now
    stamp = tuple(path.stat().st_mtime_ns if path.exists() else None
                  for path in (BADWORDS_PREFIXES_PATH, BADWORDS_EXACT_PATH, BADWORDS_ALLOW_PATH))
    if stamp == _last_mtimes:
        return
    prefixes, exact = _load_badwords_sources()
    prefix_set, exact_set = _build_fallback_set(prefixes), _build_fallback_set(exact)
    trie = _build_normalized_trie(prefix_set, exact_set)
    allowed = _build_normalized_trie(set(), _build_fallback_set(_load_words_file(BADWORDS_ALLOW_PATH)))
    # Publish complete indexes only after all files have been read successfully.
    prefix_words, exact_words = prefixes, exact
    prefix_fallback_cyr, exact_fallback_cyr = prefix_set, exact_set
    prefix_display_lookup, exact_display_lookup = _build_display_lookup(prefixes), _build_display_lookup(exact)
    _normalized_trie, _allow_trie = trie, allowed
    _last_mtimes = stamp


# These patterns find input spans, never enumerate dictionary alternatives.
_LETTERS = r"A-Za-zÀ-ÖØ-öø-ÿĀ-ſ\u0370-\u052f"
_ATOM = '['+_LETTERS+r"0-9@#$+!|']"
TOKENS_RE = re.compile(_ATOM+'{3,}')
NOISY_TOKENS_RE = re.compile(_ATOM+r"+(?:[._*~`:/\\,;?()\[\]{}-]+"+_ATOM+r"+)+")
SPELLED_TOKENS_RE = re.compile(r'(?<!\w)(?:['+_LETTERS+r'0-9][ \t]+){2,}['+_LETTERS+r'0-9](?!\w)')
KEYBOARD_LAYOUT = str.maketrans("qwertyuiop[]asdfghjkl;'zxcvbnm,.", 'йцукенгшщзхъфывапролджэячсмитьбю')


def _candidate_details(token):
    if _match_normalized(token, ('exact',), trie=_allow_trie):
        return None
    result = _match_normalized(token)
    if result:
        canonical, kind = result
        lookup = prefix_display_lookup if kind == 'prefix' else exact_display_lookup
        return token, lookup.get(canonical, canonical), kind
    return None


async def detect_badword_details(
    text: str | None,
) -> tuple[str, str, Literal["prefix", "exact"]] | None:
    if not text:
        return None

    _reload_badwords_if_needed()
    text = unicodedata.normalize('NFKC', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    text = text.replace('()', 'о')
    views = [text]
    if re.search('[A-Za-z]', text):
        views.append(text.lower().translate(KEYBOARD_LAYOUT))
    for view in views:
        for pattern in (TOKENS_RE, NOISY_TOKENS_RE, SPELLED_TOKENS_RE):
            for match in pattern.finditer(view):
                result = _candidate_details(match.group(0))
                if result:
                    return result
    return None


# Возвращает фрагмент текста, по которому сработал детектор, либо None.
async def detect_badword(text: str | None) -> str | None:
    details = await detect_badword_details(text)
    return details[0] if details else None


# Проверяет текст на наличие запрещенных слов.
async def badword_handler(text: str | None) -> bool:
    return (await detect_badword_details(text)) is not None
