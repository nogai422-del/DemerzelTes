"""Worker liveness, polling progress and bounded update processing."""
import asyncio
import json
import os
from pathlib import Path
import threading
import time

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import GetUpdates


class RuntimeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_poll = None
        self.last_tick = None
        self.poll_error = None
        self.active_updates = 0
        self.timed_out_updates = 0

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            poll_age = None if self.last_poll is None else now - self.last_poll
            tick_age = None if self.last_tick is None else now - self.last_tick
            ready = poll_age is not None and poll_age < 180 and tick_age is not None and tick_age < 45
            return dict(pid=os.getpid(), status='ok' if ready else 'unhealthy',
                        uptime_seconds=round(now-self.started, 1),
                        poll_age_seconds=None if poll_age is None else round(poll_age, 1),
                        heartbeat_age_seconds=None if tick_age is None else round(tick_age, 1),
                        poll_error=self.poll_error, active_updates=self.active_updates,
                        timed_out_updates=self.timed_out_updates)


runtime = RuntimeState()


class PollingProgress(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            result = await make_request(bot, method)
        except Exception as exc:
            with runtime.lock:
                runtime.poll_error = type(exc).__name__
            raise
        with runtime.lock:
            runtime.last_poll = time.monotonic()
            runtime.poll_error = None
        return result


class UpdateDeadline(BaseMiddleware):
    def __init__(self, timeout=120):
        self.timeout = timeout

    async def __call__(self, handler, event, data):
        with runtime.lock:
            runtime.active_updates += 1
        try:
            return await asyncio.wait_for(handler(event, data), self.timeout)
        except asyncio.TimeoutError:
            with runtime.lock:
                runtime.timed_out_updates += 1
            print(f'[UPDATE_TIMEOUT] update_id={getattr(event, "update_id", None)} timeout={self.timeout}')
            raise
        finally:
            with runtime.lock:
                runtime.active_updates -= 1


async def heartbeat_loop():
    path = Path(os.getenv('DATA_DIR', 'database')) / 'runtime-heartbeat.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    while True:
        with runtime.lock:
            runtime.last_tick = time.monotonic()
        state = runtime.snapshot()
        # A separate process reads this; replacement prevents partial JSON reads.
        temporary.write_text(json.dumps(state), encoding='utf-8')
        os.replace(temporary, path)
        await asyncio.sleep(10)


async def supervise_background(name, factory):
    while True:
        started = time.monotonic()
        try:
            await factory()
            print(f'[BACKGROUND_EXIT] task={name}; restarting')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f'[BACKGROUND_ERROR] task={name} type={type(exc).__name__}; restarting')
        await asyncio.sleep(5 if time.monotonic()-started > 60 else 30)
