"""Facts about the machine a measurement was taken on.

Every latency and memory figure in this project was produced on a 12-core Apple
Silicon laptop, and ``docs/ARCHITECTURE.md`` already says the ordering between
models transfers while the absolute milliseconds do not. The deployment target is a
Raspberry Pi 5 — four Cortex-A76 cores — so a number without the host it came from
is not comparable to anything.

**``rss_mb`` exists because the obvious version is wrong on exactly the machine that
matters.** ``resource.getrusage().ru_maxrss`` is documented as platform-dependent:
bytes on macOS and the BSDs, kilobytes on Linux. ``probe_embedders`` divided by
1024*1024 unconditionally, which is right here and reports a 158 MB process as
``0 MB`` on the board — and memory is the single figure the Pi was going to be
measured for.
"""

from __future__ import annotations

import os
import platform
import resource
import sys


def rss_mb() -> float:
    """Peak resident set of this process, in MB, on either unit convention."""

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes; macOS and the BSDs report bytes.
    divisor = 1024 if sys.platform.startswith("linux") else 1024 * 1024
    return round(peak / divisor, 1)


def total_ram_mb() -> int | None:
    """Physical memory, or ``None`` where we cannot ask portably."""

    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform
        return None
    return int(pages * page_size / (1024 * 1024))


def cpu_model() -> str:
    """A human-readable CPU name, best effort.

    ``platform.processor()`` is useless on both machines that matter — ``arm`` on
    Apple Silicon, ``aarch64`` on the Pi — and a baseline profile that cannot say
    which machine produced it is a baseline nobody can trust a ratio against. So
    each platform is asked the way that actually answers: ``/proc/cpuinfo`` on
    Linux, where "Raspberry Pi 5" and "Cortex-A76" appear, and ``sysctl`` on macOS.
    """

    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            fields: dict[str, str] = {}
            for line in handle:
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields.setdefault(key.strip(), value.strip())
        for key in ("Model", "model name", "Hardware", "CPU part"):
            if fields.get(key):
                return fields[key]
    except OSError:
        pass

    if sys.platform == "darwin":
        try:
            import subprocess

            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform
            pass

    return platform.processor() or platform.machine()


def describe() -> dict:
    """Everything needed to say what a number on this host means.

    The two derived bounds are here rather than left to the reader because both are
    ``cpu_count``-shaped and both change by a factor of three between this laptop
    and the board: the ledger semaphore is the core count, and asyncio's default
    executor is ``min(32, cores + 4)``. On a 12-core machine that is 12 and 16; on
    the Pi it is 4 and 8, and six ledgers times a few spaces reaches those.
    """

    cores = os.cpu_count() or 1
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": cpu_model(),
        "cpu_count": cores,
        "total_ram_mb": total_ram_mb(),
        "python": platform.python_version(),
        "ledger_semaphore": max(2, cores),
        "default_executor_threads": min(32, cores + 4),
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS", "unset"),
    }
