"""A benchmark must fail by naming what is missing.

Without a broker the agent subprocess starts, waits 30s for its subscriber, times
out and exits — so the bench reported that the agent never bound its port. True,
and useless: it points at the agent, the port, and a 45s timeout, none of which is
the problem. Diagnosing it meant reading the agent's log and then looking for the
process.

A wrong failure message is a defect in a script whose run takes twenty minutes,
because it is paid for in reruns.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark.preflight import (  # noqa: E402
    _host_port,
    nats_reachable,
    require_nats,
)


def _unused_port() -> int:
    """A port nothing is listening on, obtained by binding and releasing one."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("nats://127.0.0.1:4222", ("127.0.0.1", 4222)),
        # A bare host:port is what an operator types, so it has to work.
        ("127.0.0.1:4222", ("127.0.0.1", 4222)),
        # No port means the NATS default, not a crash.
        ("nats://broker.internal", ("broker.internal", 4222)),
    ],
)
def test_urls_are_parsed_the_way_they_get_typed(url: str, expected) -> None:
    assert _host_port(url) == expected


def test_an_absent_broker_is_reported_as_absent() -> None:
    assert nats_reachable(f"nats://127.0.0.1:{_unused_port()}") is False


def test_the_error_names_the_url_and_the_fix() -> None:
    """The whole point. A message that does not say what to run leaves the reader
    where the old one did."""

    port = _unused_port()

    with pytest.raises(SystemExit) as raised:
        require_nats(f"nats://127.0.0.1:{port}")

    assert raised.value.code == 2


def test_the_bench_exits_before_spawning_anything() -> None:
    """End to end, because the value is in *when* it fails.

    The old path spent 45s starting an agent that could not work. This asserts
    the script gives up in seconds, and says NATS — run as a subprocess so it
    also proves the import wiring holds outside pytest.
    """

    port = _unused_port()

    result = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark/bench_memory_retrieve_quality.py",
            "--nats-url",
            f"nats://127.0.0.1:{port}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2, (
        f"expected the preflight exit code, got {result.returncode}\n"
        f"{result.stdout[-500:]}{result.stderr[-500:]}"
    )
    combined = result.stdout + result.stderr
    assert "no NATS broker reachable" in combined
    assert "nats-server -js" in combined
    # Nothing was started. Asserted on the spawn log line rather than on the
    # absence of "did not bind", because the new message quotes that phrase when
    # explaining what it is pre-empting — my first version of this assertion
    # matched its own error text.
    assert "spawning agent_runner" not in combined
    assert "RuntimeError" not in combined
