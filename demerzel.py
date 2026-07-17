from logger.console import setup_console_capture

setup_console_capture("log")

import asyncio
import os
import signal
import sys
import threading
import tracemalloc
from typing import Any

REQUIRED_ENV_VARS = (
    "BOT_TOKEN",
    "FLASK_SECRET_KEY",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "BACKUP_CHANNEL_ID",
    "LOG_CHANNEL_ID",
    "SOURCE_CHAT_ID",
    "COSMOS_ID",
)


def _log_env_status() -> None:
    missing = [name for name in REQUIRED_ENV_VARS if not (os.getenv(name) or "").strip()]
    if missing:
        print("Не заданы переменные окружения:", ", ".join(missing))
    else:
        print("Все обязательные переменные окружения заданы")


_log_env_status()

from bot.database import close_db
from bot.main import main as bot_main
from bot.memory_monitor import memory_monitor_loop
from waitress import create_server
from web.app import app


def run_web(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
    shutdown_event: threading.Event,
    web_state: dict[str, Any],
):
    server = None
    try:
        port = int(os.getenv("PORT", "7196"))
        server = create_server(app, host="0.0.0.0", port=port)
        web_state["server"] = server

        print(f"Сервер запущен на порту {port}")
        server.run()

        if not shutdown_event.is_set():
            web_state["error"] = RuntimeError("Waitress остановился неожиданно")
            loop.call_soon_threadsafe(stop_event.set)
    except Exception as e:
        web_state["error"] = e
        loop.call_soon_threadsafe(stop_event.set)
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass


async def run_bot():
    print("Бот запущен")
    try:
        await bot_main()
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        return
    except Exception as e:
        print(f"Ошибка run_bot: {e}")


def setup_signal_handlers(loop, stop_event):
    def _stop():
        if stop_event.is_set():
            return
        print("Получен сигнал завершения")
        stop_event.set()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, _stop)
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGQUIT, _stop)
    else:
        signal.signal(signal.SIGINT, lambda s, f: _stop())
        signal.signal(signal.SIGTERM, lambda s, f: _stop())


async def main():
    print("Запуск bot + web")

    if not tracemalloc.is_tracing():
        tracemalloc.start(25)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    shutdown_event = threading.Event()
    web_state: dict[str, Any] = {"server": None, "error": None}
    exit_code = 0

    setup_signal_handlers(loop, stop_event)

    memory_task = asyncio.create_task(memory_monitor_loop())
    bot_task = asyncio.create_task(run_bot())
    stop_task = asyncio.create_task(stop_event.wait())

    web_thread = threading.Thread(
        target=run_web,
        args=(loop, stop_event, shutdown_event, web_state),
        daemon=True,
    )
    web_thread.start()

    try:
        done, _ = await asyncio.wait(
            [bot_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        print("Завершение работы...")

        if web_state["error"] is not None:
            print(f"Web-сервер завершился с ошибкой: {web_state['error']}")
            exit_code = 1

        if stop_task in done and not bot_task.done():
            bot_task.cancel()

        try:
            await asyncio.wait_for(bot_task, timeout=10)
        except asyncio.TimeoutError:
            print("Бот не смог корректно завершить работу, завершаем принудительно")
            bot_task.cancel()
        finally:
            await asyncio.gather(bot_task, return_exceptions=True)

        memory_task.cancel()
        await asyncio.gather(memory_task, return_exceptions=True)

        await close_db()
        print("База данных закрыта")

    finally:
        shutdown_event.set()

        server = web_state.get("server")
        if server is not None:
            try:
                server.close()
            except Exception:
                pass

        await asyncio.to_thread(web_thread.join, 5)
        if web_thread.is_alive():
            print("Web-поток не завершился за 5 секунд")

        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)

        if not memory_task.done():
            memory_task.cancel()
        await asyncio.gather(memory_task, return_exceptions=True)

    if exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
