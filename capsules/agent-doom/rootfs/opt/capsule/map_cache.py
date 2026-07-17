#!/usr/bin/env python3.11
"""Cached access to restful-doom map snapshots for Agent DOOM."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from wad_map import WadMapLoader, find_wad_path


@dataclass
class CachedMap:
    key: tuple[int, int, int]
    snapshot: Any
    fetched_at: float


class MapCache:
    """Keeps raw map geometry out of MCP output while giving the capsule planner state."""

    def __init__(self, *, timeout_s: float = 2.0) -> None:
        self.timeout_s = float(timeout_s)
        self._cached: CachedMap | None = None
        self._last_error: str | None = None
        self._wad_loader: WadMapLoader | None = None
        self._wad_path: str | None = None
        self._fetches = 0

    @property
    def snapshot(self) -> Any | None:
        return None if self._cached is None else self._cached.snapshot

    def refresh(self, _stub: Any, _agent_pb2: Any, state: Any | None = None, *, force: bool = False) -> Any | None:
        """Builds the latest map snapshot from capsule-local WAD data."""
        if not force and self._cached is not None and not self._state_changed(state):
            return self._cached.snapshot
        return self._refresh_from_wad(state)

    def _refresh_from_wad(self, state: Any | None) -> Any | None:
        level = getattr(state, "level", None)
        episode = int(getattr(level, "episode", 1) or 1)
        game_map = int(getattr(level, "map", 1) or 1)
        try:
            loader = self._loader()
            snapshot = loader.load(episode, game_map)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            return self.snapshot
        key = (episode, game_map, int(getattr(snapshot, "digest", 0)))
        self._cached = CachedMap(key=key, snapshot=snapshot, fetched_at=time.time())
        self._last_error = None
        self._fetches += 1
        return snapshot

    def _loader(self) -> WadMapLoader:
        wad_path = find_wad_path()
        if wad_path is None:
            raise FileNotFoundError("no WAD found under /home/app/app")
        wad_path_str = str(wad_path)
        if self._wad_loader is None or self._wad_path != wad_path_str:
            self._wad_loader = WadMapLoader(wad_path)
            self._wad_path = wad_path_str
        return self._wad_loader

    def _state_changed(self, state: Any | None) -> bool:
        if state is None or self._cached is None:
            return False
        level = getattr(state, "level", None)
        episode = int(getattr(level, "episode", self._cached.key[0]) or 0)
        game_map = int(getattr(level, "map", self._cached.key[1]) or 0)
        return (episode, game_map) != self._cached.key[:2]

    def summary(self) -> dict[str, Any]:
        if self._cached is None:
            out: dict[str, Any] = {"status": "empty", "fetches": self._fetches}
        else:
            snapshot = self._cached.snapshot
            out = {
                "status": "ready",
                "source": str(getattr(snapshot, "source", "grpc")),
                "map": [int(getattr(snapshot, "episode", 0)), int(getattr(snapshot, "map", 0))],
                "digest": int(getattr(snapshot, "digest", 0)),
                "v": len(getattr(snapshot, "vertices", []) or []),
                "l": len(getattr(snapshot, "lines", []) or []),
                "s": len(getattr(snapshot, "sectors", []) or []),
                "t": len(getattr(snapshot, "things", []) or []),
                "truncated": bool(getattr(snapshot, "truncated", False)),
                "age_ms": int((time.time() - self._cached.fetched_at) * 1000),
                "fetches": self._fetches,
            }
        if self._last_error:
            out["error"] = self._last_error[:160]
        return out
