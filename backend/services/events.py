"""In-process pub/sub used by the SSE and WebSocket live feeds."""

from __future__ import annotations

import itertools
import queue
import threading
from typing import Any

from utils.helpers import to_iso, utcnow


class EventBroker:
    """Fan-out events to a small number of connected dashboards."""

    def __init__(self, queue_size: int = 64) -> None:
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._subscribers: dict[int, queue.Queue] = {}
        self._ids = itertools.count(1)
        self._published = 0

    def subscribe(self) -> tuple[int, queue.Queue]:
        token = next(self._ids)
        channel: queue.Queue = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers[token] = channel
        return token, channel

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subscribers.pop(token, None)

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "type": event_type,
            "at": to_iso(utcnow()),
            "data": data or {},
        }
        with self._lock:
            subscribers = list(self._subscribers.values())
            self._published += 1
        for channel in subscribers:
            try:
                channel.put_nowait(event)
            except queue.Full:
                try:
                    channel.get_nowait()
                except queue.Empty:
                    pass
                try:
                    channel.put_nowait(event)
                except queue.Full:
                    pass
        return event

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def published_count(self) -> int:
        return self._published
