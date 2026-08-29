"""Owner UI cannot reassign one ledger assertion's audience.

Normal conversation scope is fixed when evidence enters the ledger. Owner
Shared is a system-policy output, not a per-entry permission toggle, so the old
``PUT /entries/{id}/audience`` surface must stay absent even on a write-capable
Host.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from eidolon.memory.entrypoints.owner_memory_http import owner_memory_routes


class _Runtime:
    backend = object()
    palace_path = "/tmp/palace"


class _Service:
    privacy_signer = None

    async def runtime_for(self, context: Any) -> _Runtime:
        return _Runtime()


class _Settings:
    wings: list[Any] = []


def test_per_entry_audience_mutation_is_not_a_product_route() -> None:
    app = Starlette(
        routes=owner_memory_routes(
            service=_Service(),  # type: ignore[arg-type]
            settings=_Settings(),  # type: ignore[arg-type]
            memory_space_id="realm_owner_one",
            owner_id="owner-1",
            service_token="memory-api-token",
            command_publisher=object(),
            command_status=None,
        )
    )
    with TestClient(app) as http:
        response = http.put(
            "/api/memory/v1/entries/drawer_1/audience",
            headers={"Authorization": "Bearer memory-api-token"},
            json={"companion_id": "c_mochi"},
        )

    assert response.status_code == 404
