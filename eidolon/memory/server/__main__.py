"""Run legacy NATS memory RPC server (delegates to ``entrypoints.server``)."""

from eidolon.memory.entrypoints.server import main

if __name__ == "__main__":
    main()
