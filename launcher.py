"""Process supervisor: recover from crashes and an unresponsive event loop."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def unhealthy_reason(state, pid, elapsed, heartbeat_age):
    if elapsed < 120:
        return None
    if state.get('pid') != pid or heartbeat_age > 90:
        return 'worker heartbeat stopped'
    age = state.get('poll_age_seconds')
    if (age is None and elapsed > 300) or (age is not None and age > 300):
        return 'Telegram polling stopped progressing'
    return None


def read_heartbeat(path):
    try:
        return json.loads(path.read_text(encoding='utf-8')), max(0, time.time()-path.stat().st_mtime)
    except (OSError, ValueError):
        return {}, float('inf')


def stop_worker(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main():
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    path = Path(os.getenv('DATA_DIR', '/app/data')) / 'runtime-heartbeat.json'
    env = dict(os.environ, DEMERZEL_WORKER='1')
    failures = 0
    while not stopping:
        child = subprocess.Popen([sys.executable, '-u', 'demerzel.py'], env=env)
        started = time.monotonic()
        print(f'[SUPERVISOR] worker started pid={child.pid}', flush=True)
        try:
            while not stopping and child.poll() is None:
                state, age = read_heartbeat(path)
                reason = unhealthy_reason(state, child.pid, time.monotonic()-started, age)
                if reason:
                    print(f'[SUPERVISOR] restarting pid={child.pid}: {reason}', flush=True)
                    break
                time.sleep(1)
        finally:
            stop_worker(child)
        if stopping:
            break
        lived = time.monotonic()-started
        failures = 0 if lived > 300 else failures+1
        delay = min(60, 2**min(failures, 6))
        print(f'[SUPERVISOR] worker exit={child.returncode}; restart in {delay}s', flush=True)
        end = time.monotonic()+delay
        while not stopping and time.monotonic() < end:
            time.sleep(0.2)


if __name__ == '__main__':
    main()
