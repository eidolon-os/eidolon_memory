"""End-to-end tests for the memory service.

These tests exercise the **MCP read + NATS write** contracts against a live
``agent_runner`` subprocess + ``nats-server`` JetStream. They take 30s-5min
each — gated by ``@pytest.mark.e2e``.

Run manually:

    .venv/bin/python -m pytest tests/memory/e2e/ -m e2e -v
"""
