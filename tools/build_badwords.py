"""Build the Russian lexicon; pymorphy3 is a build-only dependency.

Input: safetext/languages/ru/words.txt (see third_party/badwords/SOURCES.txt).
Only reviewed obscene families are imported; medical terms and neutral words
from the upstream aggregate are deliberately excluded. Inflections come from
known OpenCorpora entries, never from spelling permutations or guessed parses.
"""
import argparse
from pathlib import Path
import re
import pymorphy3

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT/'bot'/'handlers'
FAMILIES = re.compile(
    r'бляд|блят|блэт|блэд|пизд|пезд|песд|ху[йеёиуюя]|'
    r'еб[аоуеёитьнлршц]|[ъь]еб|епт[ауы]|'
    r'манд[аоеиуьыяю]|муд[аоиеы]|залуп|говн|гавн|дроч|дрюч|'
    r'суч[аёи]|сука|шлю[хш]|шалав|сра[нтлч]|срать|ссать|ссык|дрист|'
    r'гандон|гондон|пид[аоер]|пед[аи]к|жоп|бзд|перд|долбо[её]|'
    r'дебил|дебило|мраз[ьи]|сволоч|шмар|чм[оы]|ублюд|падл|вафлер')
NEUTRAL = set('мандат мандаты мандарин мандарины мандала команда командир трахея трахеи трахеит страхуй страхуйте подстрахуй подстрахуйте хулиган хулиганы хулиганство хулить похулить похуленный'.split())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    NEUTRAL.update(w.strip() for w in (HANDLERS/'badwords_allow.txt').read_text(encoding='utf-8').splitlines() if w.strip() and not w.startswith('#'))
    source = {line.strip().lower().replace('ё','е') for line in args.source.read_text(encoding='utf-8').splitlines()}
    imported = {w for w in source if re.fullmatch('[а-я-]{3,}',w) and FAMILIES.search(w) and w not in NEUTRAL}
    morph = pymorphy3.MorphAnalyzer()
    forms = set(imported)
    stems = {line.strip() for line in (HANDLERS/'badwords_prefixes.txt').read_text(encoding='utf-8').splitlines() if line.strip()}
    stems.update('бляд пизд пезд хуе хуи хуя хуйн еба еби ебл ебу говн гавн залуп мудо муда пидор пидар шлюх шалав чмош мраз сволоч'.split())
    # Read known inflections directly by prefix; do not scan all Russian words.
    for stem in sorted(stems):
        for word in morph.dictionary.words.iterkeys(stem):
            word = word.replace('ё','е')
            if FAMILIES.search(word) and word not in NEUTRAL:
                forms.add(word)
    for word in sorted(imported):
        for parsed in morph.parse(word):
            if not parsed.is_known or not FAMILIES.search(parsed.normal_form):
                continue
            forms.update(item.word.replace('ё','е') for item in parsed.lexeme)
    old = {w.strip() for w in (HANDLERS/'badwords_exact.txt').read_text(encoding='utf-8').splitlines() if w.strip()}
    words = sorted((forms | old)-NEUTRAL)
    (HANDLERS/'badwords_exact.txt').write_text('\n'.join(words)+'\n',encoding='utf-8')
    print(f'imported={len(imported)} inflected={len(forms)} exact_total={len(words)}')


if __name__ == '__main__':
    main()
