# Настройка логирования и вывода в консоль.

'''
Использование:

from logger.console import setup_console_capture
setup_console_capture("имя лошера"), например ("bot")

Выполняется один раз в самом начале скрипта запуска
Конкретно в run.py, run_bot.py, run_web.py
'''

import sys
import os
import atexit
from datetime import datetime


# Подключает перехват stdout/stderr и пишет логи в файлы.
def setup_console_capture(name: str = "run"):
    os.makedirs("logs", exist_ok=True)

    file = open(f"logs/{name}.log", "a", encoding="utf-8", buffering=1)
    atexit.register(file.close)

    # Преобразует данные в нужный формат.
    def format_line(msg: str, level: str):
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        pid = os.getpid()
        return f"[{ts}] [{pid}] [{level}] {msg}"

    class Console:

        # Инициализирует объект и его внутреннее состояние.
        def __init__(self, level, orig):
            self.level = level
            self.orig = orig

        # Записывает строку в лог и проксирует вывод в оригинальный поток.
        def write(self, s):
            if not s:
                return

            text = s.rstrip()
            if not text:
                return

            line = format_line(text, self.level)
            file.write(line + "\n")
            self.orig.write(line + "\n")

        # Принудительно сбрасывает буфер вывода в целевой поток.
        def flush(self):
            file.flush()
            self.orig.flush()

    sys.stdout = Console("INFO", sys.stdout)
    sys.stderr = Console("ERROR", sys.stderr)
