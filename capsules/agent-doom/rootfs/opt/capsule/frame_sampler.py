#!/usr/bin/env python3.11
"""Triggered X11 frame capture for Agent DOOM vision events."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from vision_state import VISION_EVENT_DIR, normalize_trigger

DEFAULT_DISPLAY = os.environ.get("DISPLAY", ":1")
DEFAULT_SIZE = os.environ.get("PAIRPUTER_VISION_CAPTURE_SIZE", "224x224")
DEFAULT_SOURCE_SIZE = os.environ.get("PAIRPUTER_VISION_SOURCE_SIZE", "320x200")
DEFAULT_MAX_EVENTS = int(os.environ.get("PAIRPUTER_VISION_MAX_EVENTS", "20"))


class FrameSampler:
    """Captures and rotates JPEG frames only when the brain asks for vision."""

    def __init__(
        self,
        *,
        event_dir: Path = VISION_EVENT_DIR,
        display: str = DEFAULT_DISPLAY,
        source_size: str = DEFAULT_SOURCE_SIZE,
        output_size: str = DEFAULT_SIZE,
        max_events: int = DEFAULT_MAX_EVENTS,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    ) -> None:
        self.event_dir = Path(event_dir)
        self.display = str(display)
        self.source_size = str(source_size)
        self.output_size = str(output_size)
        self.max_events = max(1, int(max_events))
        self._runner = runner or subprocess.run

    def capture_jpeg(self, trigger: str, *, event_id: str | None = None) -> Path | None:
        self.event_dir.mkdir(parents=True, exist_ok=True)
        stamp = event_id or f"{int(time.time() * 1000)}-{normalize_trigger(trigger)}"
        out = self.event_dir / f"{stamp}.jpg"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "x11grab",
            "-video_size",
            self.source_size,
            "-i",
            self.display,
            "-frames:v",
            "1",
            "-vf",
            f"scale={self.output_size.replace('x', ':')}:flags=neighbor",
            "-q:v",
            "6",
            str(out),
        ]
        try:
            result = self._runner(cmd, capture_output=True, timeout=4)
        except Exception:
            return None
        if int(getattr(result, "returncode", 1) or 0) != 0 or not out.exists():
            return None
        self.rotate()
        return out

    def rotate(self) -> None:
        self.event_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(
            [p for p in self.event_dir.iterdir() if p.suffix.lower() in {".jpg", ".json"}],
            key=lambda path: path.stat().st_mtime,
        )
        max_files = self.max_events * 2
        for path in files[:-max_files]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
