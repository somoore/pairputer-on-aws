#!/usr/bin/env python3
"""Bounded, deterministic semantic state fusion and reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Protocol

from evidence import canonical_json, redact


MAX_OBSERVER_BYTES = 256 * 1024


class Observer(Protocol):
    def __call__(self) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class Observation:
    name: str
    observed: bool
    value: Any
    truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class DesktopSnapshot:
    world_revision: int
    human_epoch: int
    observed_at: float
    observations: Mapping[str, Observation]
    semantic_digest: str
    changed_observers: tuple[str, ...] = ()
    truncation_notices: tuple[str, ...] = ()

    def get(self, name: str, default: Any = None) -> Any:
        item = self.observations.get(name)
        return item.value if item and item.observed else default


@dataclass(frozen=True)
class Reconciliation:
    previous_revision: int
    current_revision: int
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    missing: tuple[str, ...]
    in_scope: bool
    summary: str


class StateFusion:
    def __init__(self, human_epoch_provider: Callable[[], int] | None = None,
                 world_revision_provider: Callable[[], int] | None = None):
        self._observers: dict[str, Observer] = {}
        self._lock = asyncio.Lock()
        self._world_revision = 0
        self._last_values: dict[str, str] = {}
        self._last_snapshot: DesktopSnapshot | None = None
        self._human_epoch_provider = human_epoch_provider or (lambda: 0)
        self._world_revision_provider = world_revision_provider

    @property
    def world_revision(self) -> int:
        return self._world_revision

    @property
    def last_snapshot(self) -> DesktopSnapshot | None:
        return self._last_snapshot

    def register(self, name: str, observer: Observer) -> None:
        if not name or name in self._observers:
            raise ValueError(f"invalid or duplicate observer: {name}")
        self._observers[name] = observer

    def set_human_epoch_provider(self, provider: Callable[[], int]) -> None:
        self._human_epoch_provider = provider

    def advance_revision(self) -> int:
        """Advance for an out-of-band control event before the next observation."""

        self._world_revision += 1
        return self._world_revision

    async def observe(self, names: tuple[str, ...] | None = None) -> DesktopSnapshot:
        """Run observers in stable order and revision only on semantic change."""

        async with self._lock:
            selected = sorted(names or tuple(self._observers))
            for attempt in range(3):
                starting_epoch = int(self._human_epoch_provider())
                values: dict[str, Observation] = {}
                digests: dict[str, str] = {}
                notices: list[str] = []
                for name in selected:
                    observer = self._observers.get(name)
                    if observer is None:
                        values[name] = Observation(name, False, None, error="not_registered")
                        digests[name] = "not_observed"
                        continue
                    try:
                        raw = observer()
                        if inspect.isawaitable(raw):
                            raw = await raw
                        clean = redact(raw)
                        encoded = canonical_json(clean)
                        truncated = len(encoded.encode()) > MAX_OBSERVER_BYTES
                        if truncated:
                            clean = {
                                "digest": hashlib.sha256(encoded.encode()).hexdigest(),
                                "bytes": len(encoded.encode()),
                                "truncated": True,
                            }
                            encoded = canonical_json(clean)
                            notices.append(f"{name}: observer output exceeded {MAX_OBSERVER_BYTES} bytes")
                        values[name] = Observation(name, True, clean, truncated=truncated)
                        digests[name] = hashlib.sha256(encoded.encode()).hexdigest()
                    except Exception as exc:  # Observations fail independently and explicitly.
                        values[name] = Observation(name, False, None, error=type(exc).__name__)
                        digests[name] = f"error:{type(exc).__name__}"
                ending_epoch = int(self._human_epoch_provider())
                if starting_epoch == ending_epoch:
                    break
            else:
                raise ObservationPreempted("human epoch changed during three consecutive observations")
            changed = tuple(name for name in selected if self._last_values.get(name) != digests[name])
            if self._world_revision_provider is not None:
                authoritative_revision = int(self._world_revision_provider())
                if authoritative_revision < self._world_revision:
                    raise ObservationPreempted("authoritative world revision regressed")
                self._world_revision = authoritative_revision
            elif changed:
                self._world_revision += 1
            self._last_values.update(digests)
            semantic_digest = hashlib.sha256(canonical_json(digests).encode()).hexdigest()
            snapshot = DesktopSnapshot(
                world_revision=self._world_revision,
                human_epoch=ending_epoch,
                observed_at=time.time(),
                observations=MappingProxyType(values),
                semantic_digest=semantic_digest,
                changed_observers=changed,
                truncation_notices=tuple(notices),
            )
            self._last_snapshot = snapshot
            return snapshot

    @staticmethod
    def reconcile(previous: DesktopSnapshot | None, current: DesktopSnapshot, *, required_observers: tuple[str, ...] = ()) -> Reconciliation:
        if previous is None:
            changed = tuple(current.observations)
            unchanged: tuple[str, ...] = ()
            previous_revision = 0
        else:
            changed = current.changed_observers
            unchanged = tuple(sorted(set(current.observations) - set(changed)))
            previous_revision = previous.world_revision
        missing = tuple(
            name for name in required_observers
            if name not in current.observations or not current.observations[name].observed
        )
        summary = "no semantic changes" if not changed else "changed: " + ", ".join(changed)
        if missing:
            summary += "; unavailable: " + ", ".join(missing)
        return Reconciliation(
            previous_revision=previous_revision,
            current_revision=current.world_revision,
            changed=changed,
            unchanged=unchanged,
            missing=missing,
            in_scope=not missing,
            summary=summary,
        )


class ObservationPreempted(RuntimeError):
    pass
