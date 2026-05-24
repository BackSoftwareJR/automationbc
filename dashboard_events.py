"""WebSocket event hub for live dashboard updates."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class EventHub:
    """Broadcast dashboard events to connected WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, websocket: WebSocket) -> None:
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def send_to(self, websocket: WebSocket, event: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(event, default=str))

    async def broadcast(self, event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for client in list(self._clients):
            try:
                await client.send_text(json.dumps(event, default=str))
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    def broadcast_sync(self, event: dict[str, Any]) -> None:
        """Thread-safe broadcast from worker threads or the event loop thread."""
        if self._loop is None or not self._loop.is_running():
            return
        try:
            running = asyncio.get_running_loop()
            if running is self._loop:
                task = asyncio.create_task(self.broadcast(event))
                task.add_done_callback(self._log_broadcast_error)
                return
        except RuntimeError:
            pass
        future = asyncio.run_coroutine_threadsafe(self.broadcast(event), self._loop)
        future.add_done_callback(self._log_future_error)

    @staticmethod
    def _log_broadcast_error(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            from logger_config import bridge_logger

            bridge_logger.warning("WebSocket broadcast failed: %s", exc)

    @staticmethod
    def _log_future_error(future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            from logger_config import bridge_logger

            bridge_logger.warning("WebSocket broadcast failed: %s", exc)


_hub: EventHub | None = None


def get_event_hub() -> EventHub:
    global _hub
    if _hub is None:
        _hub = EventHub()
    return _hub
