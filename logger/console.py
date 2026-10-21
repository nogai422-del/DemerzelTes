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
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime


# Подключает перехват stdout/stderr и пишет логи в файлы.
def setup_console_capture(name: str = "run"):
    log_dir = os.path.join(os.getenv('DATA_DIR', '.'), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    file = RotatingFileHandler(os.path.join(log_dir, f'{name}.log'),
                               maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    file.setFormatter(logging.Formatter('%(message)s'))
    # A full disk must not recursively log write failures through sys.stderr.
    file.handleError = lambda record: None
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
            file.emit(logging.LogRecord(name, logging.INFO, '', 0, line, (), None))
            self.orig.write(line + "\n")
            return len(s)

        # Принудительно сбрасывает буфер вывода в целевой поток.
        def flush(self):
            file.flush()
            self.orig.flush()

    sys.stdout = Console("INFO", sys.stdout)
    sys.stderr = Console("ERROR", sys.stderr)
