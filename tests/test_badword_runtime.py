import asyncio
import itertools
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.handlers import badword_detector as detector


class DetectorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_dictionary_editor_preserves_compounds(self):
        from web.edit_badwords import _normalize_words
        self.assertEqual(_normalize_words('сунь-хуй-в-чай собака-засранец'),
                         ['собака-засранец', 'сунь-хуй-в-чай'])

    async def test_mixed_script_and_digit_combinations(self):
        cases = 0
        for letters in (
            [('п','p','n'),('и','i','1'),('з','z','3'),('д','d','9'),('а','a','4')],
            [('х','x','h','χ'),('у','y','υ'),('й','u','j')],
        ):
            for chars in itertools.product(*letters):
                for separator in ('','*','_','.'):
                    token = separator.join(chars)
                    self.assertIsNotNone(await detector.detect_badword_details(token),token)
                    cases += 1
        self.assertGreater(cases,1000)

    async def test_transliteration_layout_and_unicode(self):
        for token in ('blyad', 'blyat', 'shlyuha', 'п и з д а', 'х у й',
                      ',kznm', 'gbplf', 'х\u200bу\u200bй', 'ｂｌｙａｄ',
                      'хххууууййй', 'б***л___я...дь'):
            self.assertIsNotNone(await detector.detect_badword_details(token),token)

    async def test_neutral_exceptions_do_not_exempt_other_words(self):
        for word in ('трахея','трахеит','страхуй','подстрахуйте','мандарин',
                     'мандат','командир','хлеб','ребенок','потреблять','учебник',
                     'бляшка','серебристый','хулиганство'):
            self.assertIsNone(await detector.detect_badword_details(word),word)
            self.assertIsNotNone(await detector.detect_badword_details(word+' пиздец'),word)

    async def test_dictionary_words_and_obfuscations_remain_detected(self):
        for word in detector.prefix_words+detector.exact_words:
            normalized = detector._normalize_to_cyr(word)
            if len(normalized) < 3:
                continue  # Existing dictionary policy excludes two-letter entries.
            variants = [word, word.upper()]
            if normalized == word:
                variants += ['*'.join(word), word.replace('а','a').replace('и','i').replace('е','e').replace('о','0')]
            for text in variants:
                with self.subTest(text=text):
                    self.assertIsNotNone(await detector.detect_badword_details(text))

    async def test_normal_messages_are_allowed(self):
        for text in ('Hello everyone', 'Добрый день', '1234567890', 'myusername', 'Анкета для нового участника'):
            self.assertIsNone(await detector.detect_badword_details(text))

    async def test_long_ambiguous_token_does_not_freeze_worker(self):
        # A subprocess deadline can stop a synchronous regression; asyncio.wait_for cannot.
        script = (
            "import asyncio; from bot.handlers.badword_detector import detect_badword_details as check; "
            "assert asyncio.run(check('u'*4096)) is None; "
            "assert asyncio.run(check('а'*4096)) is None"
        )
        result = subprocess.run([sys.executable,'-c',script],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr.decode())
