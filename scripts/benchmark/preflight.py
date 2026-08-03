"""Check a benchmark's external dependencies before it starts doing work.

These scripts spawn an agent subprocess that connects to NATS. Without a broker
the agent starts, waits 30s for its subscriber to become ready, times out, and
exits — so the bench reports that the agent never bound its port. That message
points at the agent, at the port, and at a 45s timeout, none of which is the
problem. Finding the real cause meant reading the agent's log and then checking
for the process.

A wrong failure message is a defect here, not untidiness: it sends whoever is
debugging to the wrong place, and the cost is measured in reruns of a bench that
takes twenty minutes.

So the dependency is checked up front, named in the error, and paired with the
command that fixes it.
"""

from __future__ import annotations

import socket
import sys
from urllib.parse import urlsplit

NATS_DEFAULT_PORT = 4222


def _host_port(url: str) -> tuple[str, int]:
    """Split a ``nats://host:port`` URL, tolerating a missing scheme or port."""

    candidate = url if "//" in url else f"nats://{url}"
    parts = urlsplit(candidate)
    return parts.hostname or "127.0.0.1", parts.port or NATS_DEFAULT_PORT


def nats_reachable(url: str, *, timeout_s: float = 1.0) -> bool:
    """Whether something is accepting connections where NATS should be.

    A TCP connect only, deliberately. Verifying it is really NATS, with
    JetStream, would need the client and its own timeouts — and this check exists
    to replace a confusing failure with a clear one, not to become a second thing
    that can hang.
    """

    host, port = _host_port(url)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def require_nats(url: str) -> None:
    """Exit non-zero, saying what is missing and how to start it.

    Called before any subprocess is spawned or corpus published, so a missing
    broker costs a second rather than the 45s it takes to misdiagnose.
    """

    if nats_reachable(url):
        return

    host, port = _host_port(url)
    print(
        f"[FAIL] no NATS broker reachable at {url} (tried {host}:{port}).\n"
        f"       This bench publishes turns over JetStream, and the agent it\n"
        f"       spawns will not become ready without one — which surfaces as a\n"
        f"       misleading 'agent did not bind its port' error.\n"
        f"       Start one with:  nats-server -js",
        file=sys.stderr,
    )
    raise SystemExit(2)
