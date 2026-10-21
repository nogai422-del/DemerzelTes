import asyncio
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.handlers import badword_detector as detector


class DetectorRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
