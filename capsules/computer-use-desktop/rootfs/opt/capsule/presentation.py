#!/usr/bin/env python3
"""Truthful, redacted theatre-of-work events (never hidden reasoning)."""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from evidence import redact, redact_text


@dataclass(frozen=True)
class FeedEvent:
    sequence: int
    actor: str
    event_type: str
    task_id: str | None
    action_id: str | None
    intent: str
    method: str
    status: str
    detail: Mapping[str, Any]
    created_at: float


class PresentationSink:
    ALLOWED_METHODS = frozenset({
        "workspace", "process", "browser_semantic", "accessibility", "window",
        "xtest_physical", "visual_fallback", "policy", "control", "verification",
    })

    def __init__(self, max_events: int = 500):
        self._lock = threading.RLock()
        self._sequence = 0
        self._events: collections.deque[FeedEvent] = collections.deque(maxlen=max(10, min(max_events, 5000)))

    def emit(
        self,
        *,
        actor: str,
        event_type: str,
        intent: str,
        method: str,
        status: str,
        task_id: str | None = None,
        action_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> FeedEvent:
        if method not in self.ALLOWED_METHODS:
            raise ValueError("presentation method must state the real actuator")
        with self._lock:
            self._sequence += 1
            item = FeedEvent(
                sequence=self._sequence,
                actor=redact_text(actor, limit=32),
                event_type=redact_text(event_type, limit=64),
                task_id=redact_text(task_id, limit=128) if task_id else None,
                action_id=redact_text(action_id, limit=128) if action_id else None,
                intent=redact_text(intent, limit=256),
                method=method,
                status=redact_text(status, limit=64),
                detail=redact(detail or {}),
                created_at=time.time(),
            )
            self._events.append(item)
            return item

    def recent(self, *, after: int = 0, limit: int = 100) -> tuple[FeedEvent, ...]:
        with self._lock:
            return tuple(item for item in self._events if item.sequence > after)[:max(1, min(limit, 500))]
