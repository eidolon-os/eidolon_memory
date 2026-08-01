"""A real PostgreSQL for tests, without asking anyone to install one.

The shared-storage code paths — the graph and the ledgers — were for a long time
verified only structurally: tests asserted that the PostgreSQL statements matched
the SQLite ones in shape, which says nothing about whether a server accepts them.
Structural agreement with a query the server rejects is worth nothing.

``pgserver`` ships PostgreSQL binaries as a wheel, so a real server starts in a
temporary directory with no system package and no container. That turns the cloud
paths from reviewed code into tested code.

Two sources, in order:

* ``EIDOLON_MEMORY_PG_TEST_DSN`` — use this server. For checking against the
  same major version and configuration as a deployment.
* otherwise a ``pgserver`` instance for the session, torn down at the end.

Every test gets its own schema, dropped afterwards, so pointing the environment
variable at a database with other content does not touch it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

_ENV_DSN = "EIDOLON_MEMORY_PG_TEST_DSN"


def postgres_dsn_or_skip(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A DSN for a running PostgreSQL, or skip if neither source is available."""

    configured = os.environ.get(_ENV_DSN, "").strip()
    if configured:
        return configured

    pgserver = pytest.importorskip(
        "pgserver",
        reason=f"install pgserver, or set {_ENV_DSN} to an existing PostgreSQL",
    )
    # Kept for the whole session: initdb plus startup is a few seconds, which is
    # tolerable once and not per test.
    data_dir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(data_dir))
    _SESSION_SERVERS.append(server)
    return server.get_uri()


# Shut down in the session fixture below rather than by a finaliser on the helper,
# so a caller that resolves the DSN outside a fixture still gets cleanup.
_SESSION_SERVERS: list = []


@pytest.fixture(scope="session")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """DSN of a PostgreSQL that is up for the duration of the session."""

    dsn = postgres_dsn_or_skip(tmp_path_factory)
    try:
        yield dsn
    finally:
        while _SESSION_SERVERS:
            server = _SESSION_SERVERS.pop()
            try:
                server.cleanup()
            except Exception:  # noqa: BLE001 - teardown of a temp dir, never fatal
                pass


@pytest.fixture
async def postgres_pool(postgres_dsn: str, request: pytest.FixtureRequest):
    """A connection pool whose connections all default to a fresh schema.

    Per-test isolation via schema rather than database: creating a database needs
    a connection to a different one and cannot run inside a transaction, while a
    schema is cheap and drops with CASCADE. It also means these tests can run
    against a shared server without owning it.
    """

    pytest.importorskip("psycopg_pool")
    from psycopg_pool import AsyncConnectionPool

    # Node names contain characters an identifier cannot hold, and are long enough
    # to hit the 63-byte limit, so the schema is named from a hash instead.
    schema = f"t_{abs(hash(request.node.nodeid)) % (10**12):012d}"

    setup = AsyncConnectionPool(postgres_dsn, min_size=1, max_size=2, open=False)
    await setup.open()
    async with setup.connection() as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.execute(f"CREATE SCHEMA {schema}")

    # Must commit: psycopg opens a transaction for the SET, and a configure
    # callback that returns with one open has its connection discarded as
    # INTRANS. The pool then retries forever and the test times out rather than
    # failing, which is why this is worth a comment.
    async def _use_test_schema(conn) -> None:
        await conn.execute(f"SET search_path TO {schema}")
        await conn.commit()

    scoped = AsyncConnectionPool(
        postgres_dsn,
        min_size=1,
        max_size=4,
        open=False,
        configure=_use_test_schema,
    )
    await scoped.open()
    try:
        yield scoped
    finally:
        await scoped.close()
        async with setup.connection() as conn:
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await setup.close()
