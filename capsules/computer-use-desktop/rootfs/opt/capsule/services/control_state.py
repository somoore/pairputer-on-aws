"""Cross-process control epoch and world revision enforcement.

All semantic commits and human input use the same advisory lock.  A human event
therefore either advances the epoch before a commit begins or waits only for the
small, non-interruptible commit quantum already in progress.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator


class LeaseRejected(RuntimeError):
    def __init__(self, reason: str, state: dict):
        super().__init__(reason)
        self.reason = reason
        self.state = state


class ControlStateCorrupt(RuntimeError):
    pass


class ControlState:
    def __init__(self, state_dir: str | os.PathLike[str] = "/run/pairputer"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "control-state.json"
        self.lock_path = self.state_dir / "control-state.lock"
        self._thread_lock = threading.RLock()
        self.lock_path.touch(mode=0o660, exist_ok=True)
        # In the container the setgid control directory assigns the dedicated
        # control group. Only the creating owner may normalize the mode; later
        # unprivileged participants simply use the shared advisory lock.
        if self.lock_path.stat().st_uid == os.geteuid():
            self.lock_path.chmod(0o660)
        if not self.path.exists():
            with self._locked():
                if not self.path.exists():
                    self._write({"humanEpoch": 0, "worldRevision": 0,
                                 "owner": "idle", "updatedAt": time.time()})

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            with self.lock_path.open("r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise ControlStateCorrupt("authoritative control state is unavailable or corrupt") from exc
        return {
            "humanEpoch": int(value.get("humanEpoch", 0)),
            "worldRevision": int(value.get("worldRevision", 0)),
            "owner": str(value.get("owner", "idle")),
            "updatedAt": float(value.get("updatedAt", time.time())),
        }

    def _write(self, state: dict) -> None:
        state = dict(state)
        state["updatedAt"] = time.time()
        try:
            existing_owner = (self.path.stat().st_uid, self.path.stat().st_gid)
        except FileNotFoundError:
            existing_owner = None
        fd, tmp = tempfile.mkstemp(prefix=".control-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, sort_keys=True, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            # Epoch metadata is readable by the unprivileged brain, while the
            # root-owned directory and lock make it immutable to guest jobs.
            os.chmod(tmp, 0o644)
            if existing_owner is not None and os.geteuid() == 0:
                os.chown(tmp, *existing_owner)
            os.replace(tmp, self.path)
            dir_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def snapshot(self) -> dict:
        with self._locked():
            return self._read()

    def human_takeover(self) -> dict:
        """Advance the epoch before any authenticated human event is injected."""
        with self._locked():
            state = self._read()
            state["humanEpoch"] += 1
            state["worldRevision"] += 1
            state["owner"] = "human"
            self._write(state)
            return state

    def set_owner(self, owner: str) -> dict:
        if owner not in {"idle", "agent", "human"}:
            raise ValueError("invalid control owner")
        with self._locked():
            state = self._read()
            state["owner"] = owner
            self._write(state)
            return state

    @contextlib.contextmanager
    def commit(self, expected_human_epoch: int, expected_world_revision: int) -> Iterator[dict]:
        """Hold the preemption lock across one bounded atomic commit.

        The caller performs its final precondition check and mutation within the
        context.  The revision is advanced only if the caller exits successfully.
        """
        with self._locked():
            state = self._read()
            if int(expected_human_epoch) != state["humanEpoch"]:
                raise LeaseRejected("human_epoch_changed", state)
            if int(expected_world_revision) != state["worldRevision"]:
                raise LeaseRejected("world_revision_changed", state)
            yield state
            state["worldRevision"] += 1
            state["owner"] = "agent"
            self._write(state)

    def note_observed_change(self) -> dict:
        with self._locked():
            state = self._read()
            state["worldRevision"] += 1
            self._write(state)
            return state
