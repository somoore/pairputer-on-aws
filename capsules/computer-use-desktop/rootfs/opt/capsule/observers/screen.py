from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from services.common import sha256_file


class ScreenObserver:
    MAX_FILES = 100
    MAX_TOTAL_BYTES = 64 * 1024 * 1024
    MAX_FILE_BYTES = 8 * 1024 * 1024

    def __init__(self, evidence_dir, display=":1.0", logical_width=1440, logical_height=900):
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.display = display
        self.width, self.height = int(logical_width), int(logical_height)
        self._lock = threading.RLock()

    def _prune(self) -> None:
        files = []
        for path in self.evidence_dir.glob("screen-*.png"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            files.append((stat.st_mtime_ns, stat.st_size, path))
        files.sort()
        total = sum(item[1] for item in files)
        while files and (len(files) >= self.MAX_FILES or
                         total > self.MAX_TOTAL_BYTES - self.MAX_FILE_BYTES):
            _, size, path = files.pop(0)
            path.unlink(missing_ok=True)
            total -= size

    def discard(self, result: dict) -> None:
        try:
            path = Path(str(result.get("path", "")))
            if path.parent.resolve(strict=True) != self.evidence_dir.resolve(strict=True):
                return
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            return

    @staticmethod
    def _focused_window(connection):
        """Return the EWMH active-window identity and root-relative geometry.

        A missing or transiently destroyed active window is represented as
        ``None``.  That still permits pointer-only screenshots, while the input
        arbiter fails keyboard input closed unless this proof is non-null and
        remains exact at every key event.
        """
        from Xlib import X

        root = connection.screen().root
        candidates = []
        try:
            atom = connection.intern_atom("_NET_ACTIVE_WINDOW", only_if_exists=True)
            prop = root.get_full_property(atom, X.AnyPropertyType) if atom else None
            if prop is not None and getattr(prop, "value", None):
                candidates.append(int(prop.value[0]))
        except Exception:
            pass
        try:
            # Minimal WMs may omit EWMH or leave a stale active-window id. X input focus is
            # server-authoritative and is revalidated before every injected key.
            focused = connection.get_input_focus().focus
            focused_id = int(getattr(focused, "id", 0) or 0)
            if focused_id > 0 and focused_id not in candidates:
                candidates.append(focused_id)
            if focused_id == 0:
                try:
                    focused_id = int(focused)
                except (TypeError, ValueError):
                    focused_id = 0
            # With RevertToPointerRoot, the X server's real keyboard recipient is the
            # deepest viewable window under the pointer. Resolve that exact recipient
            # instead of weakening keyboard consent to an unprovable null target.
            if focused_id in (0, X.PointerRoot):
                window = root
                seen = set()
                for _ in range(32):
                    child = window.query_pointer().child
                    child_id = int(getattr(child, "id", child if isinstance(child, int) else 0) or 0)
                    if child_id <= 0 or child_id in seen:
                        break
                    seen.add(child_id)
                    window = connection.create_resource_object("window", child_id)
                if window.id != root.id and window.id not in candidates:
                    candidates.append(int(window.id))
        except Exception:
            pass
        for window_id in candidates:
            try:
                window = connection.create_resource_object("window", window_id)
                geometry = window.get_geometry()
                translated = window.translate_coords(root, 0, 0)
                width, height = int(geometry.width), int(geometry.height)
                if width < 1 or height < 1:
                    continue
                translated_x = getattr(translated, "x", getattr(translated, "dst_x", None))
                translated_y = getattr(translated, "y", getattr(translated, "dst_y", None))
                if translated_x is None or translated_y is None:
                    continue
                return {
                    "window_id": window_id,
                    "x": int(translated_x), "y": int(translated_y),
                    "width": width, "height": height,
                }
            except Exception:
                continue
        return None

    def capture(self, x=0, y=0, width=None, height=None):
        width, height = int(width or self.width), int(height or self.height)
        x, y = int(x), int(y)
        if x < 0 or y < 0 or width < 1 or height < 1 or x + width > self.width or y + height > self.height:
            raise ValueError("screenshot crop is outside display bounds")
        with self._lock:
            self._prune()
            from Xlib import X, display
            connection = display.Display(self.display.split(".", 1)[0])
            try:
                focused_window = self._focused_window(connection)
                image = connection.screen().root.get_image(
                    x, y, width, height, X.ZPixmap, 0xffffffff,
                )
                if image is None or not image.data:
                    raise RuntimeError("bounded screenshot pixels unavailable")
                pixel_sha256 = hashlib.sha256(image.data).hexdigest()
                if focused_window != self._focused_window(connection):
                    raise RuntimeError("focused window changed during screenshot capture")
            finally:
                connection.close()
            target = self.evidence_dir / f"screen-{time.time_ns()}-{uuid.uuid4().hex[:8]}.png"
            crop = f"crop={width}:{height}:{x}:{y}"
            # x11grab does a full X11 handshake + buffer alloc per invocation, which under boot-time
            # CPU contention (chromium + services + video encoder all competing) intermittently blew
            # the old 5s cap, so observe/screenshot failed for the first ~1-2 min after launch.
            # 15s gives real headroom (a single-frame grab is never legitimately that slow); -probesize
            # 32 + -thread_queue_size skip x11grab's stream-analysis so setup is faster to begin with.
            proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-probesize", "32", "-thread_queue_size", "8", "-f", "x11grab",
                 "-video_size", f"{self.width}x{self.height}", "-i", self.display, "-frames:v", "1",
                 "-vf", crop, "-y", str(target)], capture_output=True, timeout=15, check=False)
            if proc.returncode or not target.exists() or target.stat().st_size > self.MAX_FILE_BYTES:
                target.unlink(missing_ok=True)
                raise RuntimeError("bounded screenshot capture failed")
            os.chmod(target, 0o600)
            # Include the PNG bytes as base64 so a host's computer-use loop can actually SEE the
            # screenshot — the file path alone is useless to a remote host (the file lives in the
            # VM). The MCP layer turns imageBase64 into an inline image content block.
            import base64
            image_b64 = base64.b64encode(target.read_bytes()).decode("ascii")
            return {"path": str(target), "sha256": sha256_file(target, self.MAX_FILE_BYTES),
                    "pixelSha256": pixel_sha256,
                    "focusedWindow": focused_window,
                    "mimeType": "image/png", "size": target.stat().st_size,
                    "imageBase64": image_b64,
                    "x": x, "y": y, "width": width, "height": height}
