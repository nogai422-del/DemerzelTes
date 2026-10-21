"""Failure injection without Telegram traffic or production data."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetUpdates, SendMessage
from bot import message_queue as queue
from bot import runtime as rt
from launcher import unhealthy_reason, stop_worker


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        rt.runtime = rt.RuntimeState()

    async def asyncTearDown(self):
        await queue.shutdown_message_queue()

    async def test_stuck_send_expires_and_next_message_succeeds(self):
        async def hang():
            await asyncio.sleep(60)
        with patch.object(queue, 'SEND_TIMEOUT_SECONDS', .03):
            with self.assertRaises(asyncio.TimeoutError):
                await queue._send(-1, hang)
        self.assertFalse(queue._locks)
        self.assertFalse(queue._queued)
        self.assertEqual(await queue._send(-1, AsyncMock(return_value='delivered')), 'delivered')

    async def test_flood_wait_is_bounded_and_releases_chat(self):
        error = TelegramRetryAfter(method=SendMessage(chat_id=-1,text='x'), message='flood', retry_after=0)
        sender = AsyncMock(side_effect=error)
        with self.assertRaises(TelegramRetryAfter):
            await queue._send(-1, sender)
        self.assertEqual(sender.await_count, 3)
        self.assertEqual(await queue._send(-1, AsyncMock(return_value=True)), True)
        error.retry_after = 3600
        with self.assertRaises(TelegramRetryAfter):
            await asyncio.wait_for(queue._send(-1, AsyncMock(side_effect=error)), .2)

    async def test_chat_backlog_is_bounded(self):
        queue._queued[-1] = queue.MAX_QUEUED_PER_CHAT
        try:
            sender = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, 'queue full'):
                await queue._send(-1, sender)
            sender.assert_not_awaited()
        finally:
            queue._queued.clear()

    async def test_cancelled_send_releases_lock(self):
        entered = asyncio.Event()
        async def hang():
            entered.set()
            await asyncio.sleep(60)
        task = asyncio.create_task(queue._send(-2, hang))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(queue._locks)
        self.assertFalse(queue._queued)

    async def test_poll_progress_counts_empty_response_and_errors(self):
        middleware = rt.PollingProgress()
        method = GetUpdates(timeout=20)
        await middleware(AsyncMock(return_value=[]), NS(), method)
        self.assertIsNotNone(rt.runtime.last_poll)
        saved = rt.runtime.last_poll
        with self.assertRaises(OSError):
            await middleware(AsyncMock(side_effect=OSError()), NS(), method)
        self.assertEqual(rt.runtime.last_poll, saved)
        self.assertEqual(rt.runtime.poll_error, 'OSError')
        await middleware(AsyncMock(return_value=[]), NS(), method)
        self.assertIsNone(rt.runtime.poll_error)

    async def test_stalled_update_is_cancelled_and_count_cleared(self):
        finalized = asyncio.Event()
        async def stuck(event, data):
            try:
                await asyncio.sleep(60)
            finally:
                finalized.set()
        with self.assertRaises(asyncio.TimeoutError):
            await rt.UpdateDeadline(.02)(stuck, NS(update_id=12), {})
        self.assertTrue(finalized.is_set())
        self.assertEqual(rt.runtime.active_updates, 0)
        self.assertEqual(rt.runtime.timed_out_updates, 1)

    async def test_heartbeat_and_health_require_polling(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, DATA_DIR=folder):
            task = asyncio.create_task(rt.heartbeat_loop())
            await asyncio.sleep(.02)
            path = Path(folder)/'runtime-heartbeat.json'
            self.assertTrue(path.exists())
            self.assertEqual(rt.runtime.snapshot()['status'], 'unhealthy')
            rt.runtime.last_poll = time.monotonic()
            self.assertEqual(rt.runtime.snapshot()['status'], 'ok')
            rt.runtime.last_poll -= 181
            self.assertEqual(rt.runtime.snapshot()['status'], 'unhealthy')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_background_task_restarts_after_error(self):
        called = asyncio.Event()
        count = 0
        async def worker():
            nonlocal count
            count += 1
            if count == 1:
                raise OSError('temporary failure')
            called.set()
            await asyncio.Event().wait()
        original_sleep = asyncio.sleep
        async def fast_sleep(seconds):
            await original_sleep(0)
        with patch.object(rt.asyncio, 'sleep', fast_sleep):
            task = asyncio.create_task(rt.supervise_background('test', worker))
            await asyncio.wait_for(called.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(count, 2)

    async def test_database_open_failure_closes_connection(self):
        # The other test module creates an isolated database before importing it.
        from bot import database
        conn = NS(execute=AsyncMock(side_effect=OSError()),close=AsyncMock())
        with patch.object(database.aiosqlite,'connect',AsyncMock(return_value=conn)):
            with self.assertRaises(OSError):
                await database._open_conn()
        conn.close.assert_awaited_once()

    async def test_database_cancel_rolls_back(self):
        from bot import database
        cur = NS(close=AsyncMock())
        conn = NS(cursor=AsyncMock(return_value=cur),commit=AsyncMock(),rollback=AsyncMock(),close=AsyncMock())
        with patch.object(database,'_open_conn',AsyncMock(return_value=conn)):
            with self.assertRaises(asyncio.CancelledError):
                async with database.db():
                    raise asyncio.CancelledError()
        conn.rollback.assert_awaited_once()
        conn.commit.assert_not_awaited()
        conn.close.assert_awaited_once()

    async def test_http_health_exposes_stalled_polling(self):
        import importlib
        with patch.dict(os.environ, FLASK_SECRET_KEY='test-only', ADMIN_USERNAME='test', ADMIN_PASSWORD='test-only', DEMERZEL_WORKER='1'):
            web = importlib.import_module('web.app')
            with patch.object(web, 'runtime', rt.runtime):
                def check(expected):
                    with web.app.test_client() as client:
                        response = client.get('/health')
                        self.assertEqual(response.status_code,expected,response.data)
                        self.assertIn('telegram',response.get_json())
                await asyncio.to_thread(check,503)
                rt.runtime.last_poll = rt.runtime.last_tick = time.monotonic()
                await asyncio.to_thread(check,200)
                rt.runtime.last_poll -= 181
                await asyncio.to_thread(check,503)


class SupervisorTests(unittest.TestCase):
    def test_watchdog_does_not_restart_healthy_or_starting_worker(self):
        self.assertIsNone(unhealthy_reason({}, 10, 30, 1000))
        self.assertIsNone(unhealthy_reason(dict(pid=10,poll_age_seconds=25), 10, 600, 3))
        self.assertIsNone(unhealthy_reason(dict(pid=10,poll_age_seconds=None), 10, 180, 3))

    def test_watchdog_detects_stuck_loop_and_polling(self):
        self.assertIn('heartbeat', unhealthy_reason(dict(pid=10),10,200,100))
        self.assertIn('heartbeat', unhealthy_reason(dict(pid=9),10,200,1))
        self.assertIn('polling', unhealthy_reason(dict(pid=10,poll_age_seconds=301),10,600,1))
        self.assertIn('polling', unhealthy_reason(dict(pid=10,poll_age_seconds=None),10,600,1))

    def test_unresponsive_worker_is_killed_after_grace(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('test',20), 0]
        stop_worker(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    def test_real_child_is_terminated(self):
        child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
        try:
            stop_worker(child)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_real_crashed_worker_is_restarted(self):
        source = Path(__file__).resolve().parents[1]/'launcher.py'
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            (target/'launcher.py').write_text(source.read_text(encoding='utf-8'),encoding='utf-8')
            (target/'demerzel.py').write_text(
                "from pathlib import Path\nimport sys\np=Path('starts')\n"
                "n=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\n"
                "sys.exit(7 if n == 1 else 0)\n", encoding='utf-8')
            parent = subprocess.Popen([sys.executable,'-u','launcher.py'],cwd=folder,
                                      env=dict(os.environ,DATA_DIR=folder),
                                      stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            try:
                end = time.monotonic()+10
                count = 0
                while time.monotonic() < end:
                    if (target/'starts').exists():
                        count = int((target/'starts').read_text() or '0')
                    if count >= 2:
                        break
                    time.sleep(.05)
                self.assertGreaterEqual(count,2)
                # Both fake workers exit immediately; parent is in restart backoff.
                time.sleep(.1)
            finally:
                parent.terminate()
                output = parent.communicate(timeout=5)[0].decode()
            self.assertIn('worker exit=7',output)
