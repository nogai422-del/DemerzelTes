# Редактирование префиксов и точных запрещенных слов через админ-панель.

import re
from pathlib import Path

from flask import Blueprint, redirect, render_template, request, session

edit_badwords_bp = Blueprint("edit_badwords", __name__)

HANDLERS_DIR = Path(__file__).resolve().parent.parent / "bot" / "handlers"
BADWORDS_PREFIXES_PATH = HANDLERS_DIR / "badwords_prefixes.txt"
BADWORDS_EXACT_PATH = HANDLERS_DIR / "badwords_exact.txt"

# Разрешаем только буквы (ru/en/ua) и цифры; всё остальное считаем разделителями.
TOKEN_RE = re.compile(r"[0-9A-Za-z\u0400-\u04FF]+", re.UNICODE)


# Приводит сырой ввод к валидному набору слов: без дублей, без спецсимволов, в lowercase.
def _normalize_words(raw: str) -> list[str]:
    words = [w.lower().replace("\u0451", "\u0435") for w in TOKEN_RE.findall(raw or "")]
    # Отсекаем совсем короткий мусор.
    words = [w for w in words if len(w) >= 2]

    # Удаляем дубли и сортируем по алфавиту перед сохранением.
    return sorted(set(words))


# Считывает слова из файла (по одному слову в строке).
def _read_words(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# Роут редактирования словаря матов.
@edit_badwords_bp.route("/edit_badwords", methods=["GET", "POST"])
async def edit_badwords():
    if "username" not in session:
        return redirect("/login")

    if request.method == "POST":
        prefixes_raw = request.form.get("prefixes", "")
        exact_raw = request.form.get("exact_words", "")

        prefixes = _normalize_words(prefixes_raw)
        exact_words = _normalize_words(exact_raw)

        # Если слово есть в точных словах, не храним его в префиксах.
        exact_set = set(exact_words)
        prefixes = [w for w in prefixes if w not in exact_set]

        HANDLERS_DIR.mkdir(parents=True, exist_ok=True)

        prefixes_text = "\n".join(prefixes)
        if prefixes_text:
            prefixes_text += "\n"
        BADWORDS_PREFIXES_PATH.write_text(prefixes_text, encoding="utf-8", newline="\n")

        exact_text = "\n".join(exact_words)
        if exact_text:
            exact_text += "\n"
        BADWORDS_EXACT_PATH.write_text(exact_text, encoding="utf-8", newline="\n")

        return redirect("/edit_badwords?saved=1")

    prefixes = _read_words(BADWORDS_PREFIXES_PATH)
    exact_words = _read_words(BADWORDS_EXACT_PATH)

    min_height = 150
    text_length = len(" ".join(prefixes)) + len(" ".join(exact_words))
    additional_height = (text_length // 100) + 1
    textarea_height = min_height + additional_height * 20

    return render_template(
        "edit_badwords.html",
        prefixes_content=" ".join(prefixes),
        exact_words_content=" ".join(exact_words),
        textarea_height=textarea_height,
        saved=request.args.get("saved"),
    )
