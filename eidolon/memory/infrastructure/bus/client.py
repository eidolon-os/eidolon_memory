"""FastStream NATS broker factory — same API as eidolon.agent.shared.bus.BusClient."""

from __future__ import annotations

from faststream.nats import NatsBroker


class BusClient:
    """Factory for FastStream NatsBroker instances."""

    @staticmethod
    def create(
        servers: str | list[str] = "nats://localhost:4222",
        *,
        connect_timeout: int = 10,
        allow_reconnect: bool = True,
        reconnect_time_wait: int = 5,
        max_reconnect_attempts: int = -1,
        token: str | None = None,
        name: str | None = None,
        dependencies: tuple = (),
        middlewares: tuple = (),
        routers: tuple = (),
        js_options: dict | None = None,
    ) -> NatsBroker:
        return NatsBroker(
            servers=servers,
            connect_timeout=connect_timeout,
            allow_reconnect=allow_reconnect,
            reconnect_time_wait=reconnect_time_wait,
            max_reconnect_attempts=max_reconnect_attempts,
            token=token,
            name=name,
            dependencies=dependencies,
            middlewares=middlewares,
            routers=routers,
            js_options=js_options,
        )

    @staticmethod
    async def start(broker: NatsBroker) -> None:
        await broker.start()

    @staticmethod
    async def stop(broker: NatsBroker) -> None:
        await broker.stop()
