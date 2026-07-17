#!/usr/bin/env python3
"""Human-first control epochs, revocable leases, and acknowledged input batches."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, TypeVar

from task_contract import ControlOwner

T = TypeVar("T")


@dataclass(frozen=True)
class ControlLease:
    lease_id: str
    task_id: str
    action_id: str
    human_epoch: int
    world_revision: int
    acquired_at: float


@dataclass(frozen=True)
class InputReceipt:
    sequence: int
    accepted: bool
    accepted_events: int
    dropped_events: int
    owner: ControlOwner
    human_epoch: int
    reason: str
    actual_cursor_x: float | None = None
    actual_cursor_y: float | None = None


@dataclass(frozen=True)
class PreemptionEvent:
    human_epoch: int
    event_type: str
    received_at: float
    released_keys: tuple[str, ...]
    released_buttons: tuple[int, ...]


class ControlClient:
    """In-process reference control service used by the brain and deterministic tests.

    The eventual input service adapter can implement the same interface over a Unix
    socket.  The invariant lives here: human epoch increments before human input is
    forwarded, invalidating every previously acquired lease.
    """

    def __init__(self, world_revision_provider: Callable[[], int] | None = None,
                 authoritative_epoch_provider: Callable[[], int] | None = None):
        self._lock = threading.RLock()
        self._human_epoch = 0
        self._owner = ControlOwner.HUMAN
        self._lease: ControlLease | None = None
        self._sequence = 0
        self._held_keys: set[str] = set()
        self._held_buttons: set[int] = set()
        self._cursor: tuple[float | None, float | None] = (None, None)
        self._frozen = False
        self._listeners: list[Callable[[PreemptionEvent], None]] = []
        self._world_revision_provider = world_revision_provider or (lambda: 0)
        self._authoritative_epoch_provider = authoritative_epoch_provider

    def set_authoritative_epoch_provider(self, provider: Callable[[], int]) -> None:
        """Attach the shared input arbiter's fail-closed epoch source."""

        with self._lock:
            self._authoritative_epoch_provider = provider
            self._refresh_authoritative_epoch_locked()

    @property
    def human_epoch(self) -> int:
        with self._lock:
            return self._human_epoch

    @property
    def owner(self) -> ControlOwner:
        with self._lock:
            return self._owner

    @property
    def held_state(self) -> tuple[tuple[str, ...], tuple[int, ...]]:
        with self._lock:
            return tuple(sorted(self._held_keys)), tuple(sorted(self._held_buttons))

    def subscribe_preemption(self, listener: Callable[[PreemptionEvent], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def acquire(self, *, task_id: str, action_id: str, expected_human_epoch: int, expected_world_revision: int) -> ControlLease:
        with self._lock:
            self._assert_available(expected_human_epoch, expected_world_revision)
            lease = ControlLease(
                lease_id=f"lease_{uuid.uuid4().hex}", task_id=task_id, action_id=action_id,
                human_epoch=self._human_epoch, world_revision=expected_world_revision,
                acquired_at=time.time(),
            )
            self._lease = lease
            self._owner = ControlOwner.AGENT
            return lease

    def checkpoint(self, lease: ControlLease, *, expected_world_revision: int | None = None) -> None:
        with self._lock:
            self._refresh_authoritative_epoch_locked()
            if self._frozen:
                raise FreezeBarrier("control is behind a freeze barrier")
            if lease.human_epoch != self._human_epoch:
                raise HumanPreempted("human epoch changed")
            if self._lease is None or self._lease.lease_id != lease.lease_id:
                raise LeaseRevoked("agent lease was revoked")
            expected = lease.world_revision if expected_world_revision is None else expected_world_revision
            if int(self._world_revision_provider()) != int(expected):
                raise WorldChanged("world revision changed after action preparation")

    def atomic_commit(self, lease: ControlLease, operation: Callable[[], T]) -> T:
        """Run one short non-awaiting commit while epoch advancement is excluded.

        The human is never blocked by planning, preparation, animation, I/O waits,
        or verification.  Only the final bounded syscall/call boundary belongs here.
        """

        with self._lock:
            self.checkpoint(lease)
            return operation()

    def release(self, lease: ControlLease | None = None) -> None:
        with self._lock:
            if lease is None or (self._lease and self._lease.lease_id == lease.lease_id):
                self._lease = None
                self._owner = ControlOwner.IDLE
                self._release_held_locked()

    def reset_to_human(self) -> None:
        """Crash/thaw recovery state: no lease and human owns the machine."""

        with self._lock:
            self._lease = None
            self._owner = ControlOwner.HUMAN
            self._release_held_locked()

    def human_input(self, event_type: str = "input") -> PreemptionEvent:
        """Synchronously revoke the agent before the caller forwards human input."""

        with self._lock:
            self._human_epoch += 1
            self._lease = None
            self._owner = ControlOwner.HUMAN
            released_keys, released_buttons = self._release_held_locked()
            event = PreemptionEvent(
                human_epoch=self._human_epoch,
                event_type=str(event_type),
                received_at=time.time(),
                released_keys=released_keys,
                released_buttons=released_buttons,
            )
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(event)
        return event

    def synchronize_human_epoch(
        self, target_epoch: int, event_type: str = "external_human_input",
    ) -> PreemptionEvent | None:
        """Monotonically align to the shared input arbiter's authoritative epoch.

        AF_UNIX preemption consumers and startup reconciliation call this method.
        Advancing by one or many epochs emits exactly one local preemption event;
        equal/stale values are no-ops and can never restore a revoked lease.
        """

        target = int(target_epoch)
        if target < 0:
            raise ValueError("human epoch cannot be negative")
        with self._lock:
            if target <= self._human_epoch:
                return None
            self._human_epoch = target
            self._lease = None
            self._owner = ControlOwner.HUMAN
            released_keys, released_buttons = self._release_held_locked()
            event = PreemptionEvent(
                human_epoch=self._human_epoch,
                event_type=str(event_type),
                received_at=time.time(),
                released_keys=released_keys,
                released_buttons=released_buttons,
            )
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(event)
        return event

    async def submit_batch(self, lease: ControlLease, events: Iterable[Mapping[str, Any]]) -> InputReceipt:
        batch = tuple(dict(event) for event in events)
        await asyncio.sleep(0)  # explicit race point for human takeover tests
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            try:
                self.checkpoint(lease)
            except ControlError as exc:
                return self._receipt(sequence, False, 0, len(batch), type(exc).__name__)
            for event in batch:
                self._track_event_locked(event)
            return self._receipt(sequence, True, len(batch), 0, "accepted")

    def begin_freeze(self) -> None:
        with self._lock:
            self._frozen = True
            self._lease = None
            self._owner = ControlOwner.HUMAN
            self._release_held_locked()

    def end_thaw(self) -> None:
        with self._lock:
            # The session/version boundary is also a control boundary.
            self._human_epoch += 1
            self._frozen = False
            self._lease = None
            self._owner = ControlOwner.HUMAN

    def _assert_available(self, expected_epoch: int, expected_world_revision: int) -> None:
        self._refresh_authoritative_epoch_locked()
        if self._frozen:
            raise FreezeBarrier("cannot acquire control during freeze")
        if int(expected_epoch) != self._human_epoch:
            raise HumanPreempted("human epoch does not match")
        if int(expected_world_revision) != int(self._world_revision_provider()):
            raise WorldChanged("world revision does not match")
        if self._lease is not None:
            raise LeaseUnavailable("another agent action owns the lease")

    def _refresh_authoritative_epoch_locked(self) -> None:
        if self._authoritative_epoch_provider is None:
            return
        try:
            target = int(self._authoritative_epoch_provider())
        except Exception as exc:
            self._lease = None
            self._owner = ControlOwner.HUMAN
            self._release_held_locked()
            raise HumanPreempted("authoritative human epoch is unavailable") from exc
        if target < self._human_epoch:
            raise HumanPreempted("authoritative human epoch regressed")
        if target > self._human_epoch:
            self._human_epoch = target
            self._lease = None
            self._owner = ControlOwner.HUMAN
            self._release_held_locked()

    def _release_held_locked(self) -> tuple[tuple[str, ...], tuple[int, ...]]:
        keys = tuple(sorted(self._held_keys))
        buttons = tuple(sorted(self._held_buttons))
        self._held_keys.clear()
        self._held_buttons.clear()
        return keys, buttons

    def _track_event_locked(self, event: Mapping[str, Any]) -> None:
        kind = str(event.get("kind") or event.get("type") or "")
        if kind in {"key_down", "keydown"}:
            self._held_keys.add(str(event.get("key")))
        elif kind in {"key_up", "keyup"}:
            self._held_keys.discard(str(event.get("key")))
        elif kind in {"button_down", "mousedown"}:
            self._held_buttons.add(int(event.get("button", 1)))
        elif kind in {"button_up", "mouseup"}:
            self._held_buttons.discard(int(event.get("button", 1)))
        if kind in {"pointer_move", "mousemove"}:
            self._cursor = (float(event.get("x", 0)), float(event.get("y", 0)))

    def _receipt(self, sequence: int, accepted: bool, accepted_events: int, dropped_events: int, reason: str) -> InputReceipt:
        return InputReceipt(
            sequence=sequence, accepted=accepted, accepted_events=accepted_events,
            dropped_events=dropped_events, owner=self._owner, human_epoch=self._human_epoch,
            reason=reason, actual_cursor_x=self._cursor[0], actual_cursor_y=self._cursor[1],
        )


class ControlError(RuntimeError):
    pass


class HumanPreempted(ControlError):
    pass


class LeaseRevoked(ControlError):
    pass


class LeaseUnavailable(ControlError):
    pass


class WorldChanged(ControlError):
    pass


class FreezeBarrier(ControlError):
    pass
