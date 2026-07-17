import asyncio
import os
import tracemalloc


def _read_rss_mb() -> float | None:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) / 1024.0
    except Exception:
        return None
    return None


def _read_tracemalloc_mb() -> tuple[float, float]:
    current, peak = tracemalloc.get_traced_memory()
    return current / (1024 * 1024), peak / (1024 * 1024)


def _format_stat_diff(stat) -> str:
    frame = stat.traceback[0]
    size_mb = stat.size_diff / (1024 * 1024)
    count = stat.count_diff
    return f"{frame.filename}:{frame.lineno} size_diff={size_mb:.2f}MB count_diff={count}"


async def memory_monitor_loop(
    interval_seconds: int = 60,
    diff_interval_loops: int = 5,
    top_stats_limit: int = 10,
) -> None:
    from bot.message_queue import _locks

    if not tracemalloc.is_tracing():
        tracemalloc.start(25)

    previous_snapshot = tracemalloc.take_snapshot()
    loop_index = 0
    pid = os.getpid()

    while True:
        try:
            loop_index += 1

            rss_mb = _read_rss_mb()
            traced_current_mb, traced_peak_mb = _read_tracemalloc_mb()
            task_count = len(asyncio.all_tasks())
            lock_count = len(_locks)

            rss_text = f"{rss_mb:.2f}MB" if rss_mb is not None else "n/a"
            print(
                "[MEM] "
                f"pid={pid} rss={rss_text} "
                f"tracemalloc_current={traced_current_mb:.2f}MB "
                f"tracemalloc_peak={traced_peak_mb:.2f}MB "
                f"asyncio_tasks={task_count} "
                f"message_queue_locks={lock_count}"
            )

            if loop_index % diff_interval_loops == 0:
                current_snapshot = tracemalloc.take_snapshot()
                top_stats = current_snapshot.compare_to(previous_snapshot, "lineno")

                printed = 0
                for stat in top_stats:
                    if stat.size_diff <= 0:
                        continue
                    print(f"[MEMDIFF] {_format_stat_diff(stat)}")
                    printed += 1
                    if printed >= top_stats_limit:
                        break

                if printed == 0:
                    print("[MEMDIFF] no positive growth since last snapshot")

                previous_snapshot = current_snapshot

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[MEM] monitor error: {e}")

        await asyncio.sleep(interval_seconds)
