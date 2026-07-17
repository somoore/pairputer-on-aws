#!/usr/bin/env python3.11
"""Human-first XTEST input with epochs, held-state cleanup, and receipts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.server
import json
import logging
import os
import socket
import threading
import time

from services.control_state import ControlState
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

PORT, STATE_PORT = 6904, 6906
HOST = os.environ.get("PAIRPUTER_WS_BIND", "0.0.0.0")
AGENT_KEY_FILE = os.environ.get("PAIRPUTER_AGENT_KEY_FILE", "/run/pairputer/agent-input.key")
COOLDOWN_S = float(os.environ.get("PAIRPUTER_AGENT_COOLDOWN_S", "0.6"))
# After this much idle with no human or agent input, decay owner back to "idle" so the overlay stops
# showing a stale actor. Also the poll cadence for the display-human-activity detector.
IDLE_OWNER_RESET_MS = float(os.environ.get("PAIRPUTER_IDLE_OWNER_RESET_MS", "1500"))
HUMAN_DETECT_POLL_S = float(os.environ.get("PAIRPUTER_HUMAN_DETECT_POLL_S", "0.15"))
MAX_BATCH = int(os.environ.get("PAIRPUTER_INPUT_MAX_BATCH", "32"))


class XTestInjector:
    SPECIAL = {
        "Enter": "Return", "Backspace": "BackSpace", "Tab": "Tab", "Escape": "Escape",
        "ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down",
        "Delete": "Delete", "Home": "Home", "End": "End", "PageUp": "Page_Up", "PageDown": "Page_Down",
        "Shift": "Shift_L", "Control": "Control_L", "Alt": "Alt_L", "Meta": "Super_L", " ": "space",
    }
    CHARACTER_KEYSYMS = {
        "!": "exclam", '"': "quotedbl", "#": "numbersign", "$": "dollar", "%": "percent",
        "&": "ampersand", "'": "apostrophe", "(": "parenleft", ")": "parenright",
        "*": "asterisk", "+": "plus", ",": "comma", "-": "minus", ".": "period",
        "/": "slash", ":": "colon", ";": "semicolon", "<": "less", "=": "equal",
        ">": "greater", "?": "question", "@": "at", "[": "bracketleft",
        "\\": "backslash", "]": "bracketright", "^": "asciicircum", "_": "underscore",
        "`": "grave", "{": "braceleft", "|": "bar", "}": "braceright", "~": "asciitilde",
    }

    def __init__(self, display_name=":1"):
        from Xlib import display
        last = None
        for _ in range(150):
            try:
                self.display = display.Display(display_name); break
            except Exception as exc:
                last = exc; time.sleep(0.2)
        else:
            raise last or RuntimeError("X display unavailable")
        if not self.display.query_extension("XTEST"):
            raise RuntimeError("XTEST extension unavailable")
        # X11 exposes Shift as one physical modifier bit.  Keep explicit Shift
        # presses separate from the synthetic Shift needed for characters such
        # as ``A`` and ``!`` so overlapping key sequences cannot release the
        # modifier out from under one another.
        self._explicit_shift_down = False
        self._synthetic_shift_keys = set()
        self._physical_shift_down = False

    def display_size(self):
        screen = self.display.screen()
        return int(screen.width_in_pixels), int(screen.height_in_pixels)

    def _resolve_key(self, key):
        from Xlib import XK
        # Accept three vocabularies for the same physical key: browser-style names (Enter,
        # ArrowLeft) via SPECIAL, punctuation via CHARACTER_KEYSYMS, single characters, AND
        # bare X keysym names (Return, Left, BackSpace) that the CUA adapter emits — the last
        # was previously dropped ("unsupported key") because it's multi-char, breaking Enter.
        symbol_name = self.SPECIAL.get(key) or self.CHARACTER_KEYSYMS.get(key)
        if symbol_name is None:
            symbol_name = key if (len(key) == 1 or XK.string_to_keysym(key)) else ""
        symbol = XK.string_to_keysym(symbol_name) if symbol_name else 0
        keycode = self.display.keysym_to_keycode(symbol) if symbol else 0
        if not keycode:
            raise ValueError("unsupported key")
        mapping = self.display.get_keyboard_mapping(keycode, 1)
        levels = list(mapping[0]) if mapping else []
        if not levels or levels[0] == symbol:
            needs_shift = False
        elif len(levels) > 1 and levels[1] == symbol:
            needs_shift = True
        else:
            # Levels above 1 generally require AltGr or another layout-specific
            # modifier.  Treating them as Shift types the wrong character.
            raise ValueError("unsupported key")
        return keycode, needs_shift

    def _set_physical_shift(self, down, xtest, x_module, shift_code):
        if bool(down) == self._physical_shift_down:
            return
        xtest.fake_input(
            self.display,
            x_module.KeyPress if down else x_module.KeyRelease,
            shift_code,
        )
        self._physical_shift_down = bool(down)

    def validate(self, event):
        if event.get("t") == "k":
            self._resolve_key(str(event.get("key", "")))

    def inject(self, event):
        from Xlib import X, XK
        from Xlib.ext import xtest
        kind = event.get("t")
        if kind == "m":
            width, height = self.display_size()
            x = max(0, min(width - 1, int(event.get("x", 0))))
            y = max(0, min(height - 1, int(event.get("y", 0))))
            xtest.fake_input(self.display, X.MotionNotify, x=x, y=y)
        elif kind == "b":
            button = int(event.get("button", 0)) + 1
            if not 1 <= button <= 8:
                raise ValueError("invalid pointer button")
            press = bool(event.get("down"))
            xtest.fake_input(self.display, X.ButtonPress if press else X.ButtonRelease, button)
            # A synthetic XTEST click does NOT make Mutter transfer keyboard focus the way a
            # real click does, so typed keys land nowhere. On a left-button PRESS, focus + raise
            # the top-level window under the pointer — what a real click would trigger — so the
            # very next `type` goes to the clicked window. Best-effort; never break input.
            if press and button == 1:
                self._focus_window_under_pointer()
        elif kind == "k":
            key = str(event.get("key", ""))
            keycode, needs_shift = self._resolve_key(key)
            down = bool(event.get("down"))
            shift_code = self.display.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
            if key == "Shift":
                self._explicit_shift_down = down
                self._set_physical_shift(
                    self._explicit_shift_down or bool(self._synthetic_shift_keys),
                    xtest,
                    X,
                    shift_code,
                )
            else:
                synthetic_key = (keycode, key)
                if needs_shift and down:
                    self._synthetic_shift_keys.add(synthetic_key)
                    self._set_physical_shift(True, xtest, X, shift_code)
                xtest.fake_input(self.display, X.KeyPress if down else X.KeyRelease, keycode)
                if needs_shift and not down:
                    self._synthetic_shift_keys.discard(synthetic_key)
                    self._set_physical_shift(
                        self._explicit_shift_down or bool(self._synthetic_shift_keys),
                        xtest,
                        X,
                        shift_code,
                    )
        else:
            raise ValueError("unknown input event type")
        self.display.sync()

    def _focus_window_under_pointer(self):
        """Give keyboard focus (and raise) the top-level window under the pointer, so a
        synthetic click behaves like a real one. Best-effort — any failure is swallowed so
        input injection never breaks because of focus bookkeeping."""
        try:
            from Xlib import X
            root = self.display.screen().root
            pointer = root.query_pointer()
            child = pointer.child
            if not child or child == X.NONE:
                return
            # Walk up to the client top-level: the window whose parent is the root (its WM frame),
            # then descend once — set focus on the deepest managed window under the pointer.
            target = child
            for _ in range(8):
                try:
                    parent = target.query_tree().parent
                except Exception:
                    break
                if not parent or parent == root:
                    break
                target = parent
            target.set_input_focus(X.RevertToParent, X.CurrentTime)
            target.configure(stack_mode=X.Above)
            self.display.sync()
        except Exception:
            pass

    def cursor(self):
        pointer = self.display.screen().root.query_pointer()
        return int(pointer.root_x), int(pointer.root_y)

    def display_idle_ms(self):
        """Milliseconds since the last input to the X display, via MIT-SCREEN-SAVER. Detects input
        from ANY source (VNC viewer, physical console, XTEST) — the primitive for human detection.
        Returns None if the extension is unavailable."""
        try:
            info = self.display.screen().root.screensaver_query_info()
            return int(info.idle)
        except Exception:
            return None

    def region_digest(self, x, y, width, height):
        from Xlib import X
        screen = self.display.screen()
        x, y, width, height = int(x), int(y), int(width), int(height)
        if x < 0 or y < 0 or width < 1 or height < 1 \
                or x + width > screen.width_in_pixels or y + height > screen.height_in_pixels:
            raise ValueError("target proof is outside display bounds")
        image = screen.root.get_image(x, y, width, height, X.ZPixmap, 0xffffffff)
        if image is None or not image.data:
            raise RuntimeError("target proof pixels are unavailable")
        return hashlib.sha256(image.data).hexdigest()

    def focused_window_proof(self):
        from Xlib import X

        root = self.display.screen().root
        candidates = []
        try:
            atom = self.display.intern_atom("_NET_ACTIVE_WINDOW", only_if_exists=True)
            prop = root.get_full_property(atom, X.AnyPropertyType) if atom else None
            if prop is not None and getattr(prop, "value", None):
                candidates.append(int(prop.value[0]))
        except Exception:
            pass
        try:
            focused = self.display.get_input_focus().focus
            focused_id = int(getattr(focused, "id", 0) or 0)
            if focused_id > 0 and focused_id not in candidates:
                candidates.append(focused_id)
            if focused_id == 0:
                try:
                    focused_id = int(focused)
                except (TypeError, ValueError):
                    focused_id = 0
            # PointerRoot means keys go to the deepest viewable window under the
            # pointer. Resolve that server-authoritative recipient so the screenshot
            # proof and every injected key revalidation agree exactly.
            if focused_id in (0, X.PointerRoot):
                window = root
                seen = set()
                for _ in range(32):
                    child = window.query_pointer().child
                    child_id = int(getattr(child, "id", child if isinstance(child, int) else 0) or 0)
                    if child_id <= 0 or child_id in seen:
                        break
                    seen.add(child_id)
                    window = self.display.create_resource_object("window", child_id)
                if window.id != root.id and window.id not in candidates:
                    candidates.append(int(window.id))
        except Exception:
            pass
        for window_id in candidates:
            try:
                window = self.display.create_resource_object("window", window_id)
                geometry = window.get_geometry()
                translated = window.translate_coords(root, 0, 0)
                width, height = int(geometry.width), int(geometry.height)
                if width < 1 or height < 1:
                    continue
                translated_x = getattr(translated, "x", getattr(translated, "dst_x", None))
                translated_y = getattr(translated, "y", getattr(translated, "dst_y", None))
                if translated_x is None or translated_y is None:
                    continue
                return {"window_id": window_id,
                        "x": int(translated_x), "y": int(translated_y),
                        "width": width, "height": height}
            except Exception:
                continue
        return None


class InputArbiter:
    def __init__(self, injector, control: ControlState):
        self.injector, self.control = injector, control
        self._lock = threading.RLock()
        self._last_human = 0.0
        self._sequence = 0
        self._agent_keys, self._agent_buttons = set(), set()
        self._dropped = 0
        self._last_receipt = None
        # Human-activity detection: the agent injects through submit() (which bumps this), but a HUMAN
        # can drive the desktop through a path the arbiter never sees — a raw VNC viewer injects
        # straight into the X server, and a physical console likewise. So we ALSO watch the X11 idle
        # counter (MIT-SCREEN-SAVER): if input reaches the display that the agent did NOT cause, it's
        # a human, and we run the same human_takeover the relay path does. This makes owner attribution
        # truthful for EVERY input path, not just the arbiter's own channel.
        self._last_agent_inject = 0.0
        self._last_idle_ms = None
        self._human_edge_ticks = 0

    def note_agent_injected(self):
        self._last_agent_inject = time.monotonic()

    def detect_display_human_activity(self, idle_ms=None):
        """Given the current X11 idle time (ms), if the display got input the agent didn't cause,
        treat it as a human takeover (same as a relay human event). Also decay a stale owner to idle."""
        if idle_ms is None:
            try:
                idle_ms = self.injector.display_idle_ms()
            except Exception:
                return
        if idle_ms is None:
            return
        prev = self._last_idle_ms
        self._last_idle_ms = idle_ms
        now = time.monotonic()
        # A NEW input event is an EDGE: the idle counter dropped meaningfully below the previous poll
        # (input resets it toward 0). Steady-low idle is NOT a new event — that's the mistake that made
        # a 1-second-old agent move look like fresh human input every poll.
        new_input = prev is not None and idle_ms + 40 < prev
        # Attribute the edge: if the agent injected very recently, it was the agent; else a human drove
        # the desktop directly (VNC/console) — input the arbiter's own channel never saw.
        agent_recent = (now - self._last_agent_inject) <= 0.5
        with self._lock:
            if new_input and not agent_recent:
                # Debounce: a one-shot idle reset is NOT a human. X clients (Chromium among them) call
                # XResetScreenSaver, producing a single unattributed edge that used to mislabel a purely
                # agent-driven desktop as "human" and drop the agent's next events. A real human's
                # motion resets the counter on consecutive polls, so demand two edge ticks (~0.3s).
                self._human_edge_ticks += 1
                if self._human_edge_ticks >= 2 and (
                        self.control.snapshot().get("owner") != "human" or now - self._last_human > 0.3):
                    state = self.control.human_takeover()
                    self._release_held()
                    self._last_human = now
                    self._notify_preempt(state)
            else:
                self._human_edge_ticks = 0
                if idle_ms > IDLE_OWNER_RESET_MS and (now - self._last_human) * 1000.0 > IDLE_OWNER_RESET_MS \
                        and (now - self._last_agent_inject) * 1000.0 > IDLE_OWNER_RESET_MS:
                    # Nobody has acted for a while — decay a stale owner back to idle so the overlay
                    # doesn't keep showing "Agent"/"You" long after either stopped.
                    if self.control.snapshot().get("owner") != "idle":
                        self.control.set_owner("idle")

    def _notify_preempt(self, state):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                notifier.sendto(json.dumps({"type": "human_preempt", "humanEpoch": state["humanEpoch"],
                    "worldRevision": state["worldRevision"], "timestamp": time.time()},
                    separators=(",", ":")).encode(),
                    os.environ.get("PAIRPUTER_BRAIN_PREEMPT_SOCKET", "/run/pairputer/brain-preempt.sock"))
        except OSError:
            pass

    def _release_held(self):
        for key in list(self._agent_keys):
            try: self.injector.inject({"t": "k", "key": key, "down": False})
            except Exception: pass
        for button in list(self._agent_buttons):
            try: self.injector.inject({"t": "b", "button": button, "down": False})
            except Exception: pass
        released = len(self._agent_keys) + len(self._agent_buttons)
        self._agent_keys.clear(); self._agent_buttons.clear()
        return released

    def _track(self, event):
        if event.get("t") == "k":
            target, value = self._agent_keys, str(event.get("key", ""))
        elif event.get("t") == "b":
            target, value = self._agent_buttons, int(event.get("button", 0))
        else:
            return
        target.add(value) if event.get("down") else target.discard(value)

    @staticmethod
    def _valid_focused_window(proof):
        if not isinstance(proof, dict) or set(proof) != {
                "window_id", "x", "y", "width", "height"}:
            return False
        if any(isinstance(proof[key], bool) or not isinstance(proof[key], int)
               for key in proof):
            return False
        return proof["window_id"] > 0 and proof["width"] > 0 and proof["height"] > 0

    def _keyboard_focus_matches(self, proof):
        approved = proof.get("focused_window") if isinstance(proof, dict) else None
        if not self._valid_focused_window(approved):
            return False
        actual = self.injector.focused_window_proof()
        if not self._valid_focused_window(actual):
            return False
        approved_json = json.dumps(approved, sort_keys=True, separators=(",", ":"))
        actual_json = json.dumps(actual, sort_keys=True, separators=(",", ":"))
        return hmac.compare_digest(approved_json, actual_json)

    def _validate_target_proof(self, proof, events):
        if not isinstance(proof, dict) or set(proof) != {
                "x", "y", "width", "height", "pixel_sha256", "focused_window"}:
            raise ValueError("exact target proof is required")
        if any(isinstance(proof[key], bool) or not isinstance(proof[key], int)
               for key in ("x", "y", "width", "height")):
            raise ValueError("target proof geometry is invalid")
        x, y, width, height = (proof[key] for key in ("x", "y", "width", "height"))
        digest = str(proof.get("pixel_sha256") or "")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("target proof digest is invalid")
        actual = self.injector.region_digest(x, y, width, height)
        if not hmac.compare_digest(actual, digest):
            return "target_changed"
        cursor_x, cursor_y = self.injector.cursor()
        for event in events:
            if event.get("t") == "m":
                cursor_x, cursor_y = int(event["x"]), int(event["y"])
                if not (x <= cursor_x < x + width and y <= cursor_y < y + height):
                    raise ValueError("pointer event is outside its approved target proof")
            elif event.get("t") == "b" and not (
                    x <= cursor_x < x + width and y <= cursor_y < y + height):
                raise ValueError("pointer button is outside its approved target proof")
        if any(event.get("t") == "k" for event in events) and not self._keyboard_focus_matches(proof):
            return "focused_window_changed"
        return ""

    def submit(self, actor, events, expected_epoch=None, display_revision=None, sequence=None,
               target_proof=None):
        if not isinstance(events, list) or not events or len(events) > MAX_BATCH:
            raise ValueError("input batch must contain 1..%d events" % MAX_BATCH)
        with self._lock:
            for event in events:
                if not isinstance(event, dict) or event.get("t") not in {"k", "m", "b"}:
                    raise ValueError("invalid input event")
                allowed = {"t", "actor"} | ({"key", "down"} if event["t"] == "k" else
                    {"x", "y"} if event["t"] == "m" else {"button", "down"})
                if set(event) - allowed:
                    raise ValueError("unknown input event fields")
                validator = getattr(self.injector, "validate", None)
                if validator:
                    validator(event)
            self._sequence = max(self._sequence + 1, int(sequence or 0))
            accepted, dropped, reason, released = 0, 0, "", 0
            if actor == "human":
                state = self.control.human_takeover()
                released = self._release_held()
                self._last_human = time.monotonic()
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                        notifier.sendto(json.dumps({"type": "human_preempt", "humanEpoch": state["humanEpoch"],
                            "worldRevision": state["worldRevision"], "timestamp": time.time()},
                            separators=(",", ":")).encode(),
                            os.environ.get("PAIRPUTER_BRAIN_PREEMPT_SOCKET", "/run/pairputer/brain-preempt.sock"))
                except OSError:
                    pass
                for event in events:
                    self.injector.inject(event); accepted += 1
            elif actor == "agent_raw":
                # Open CUA surface: a stock computer-use loop (OpenAI/Anthropic) sends raw
                # click/type/scroll by coordinate. It cannot produce target_proof or track
                # epochs, so we DROP those requirements — but keep the human-first spine intact:
                # the human cooldown still blocks the agent right after any human input, and a
                # human event still seizes focus + preempts. The disposable VM absorbs mistakes.
                state = self.control.snapshot()
                if time.monotonic() - self._last_human < COOLDOWN_S:
                    dropped, reason = len(events), "human_active"
                else:
                    for event in events:
                        if time.monotonic() - self._last_human < COOLDOWN_S:
                            dropped += len(events) - accepted; reason = "human_active"; break
                        # Note BEFORE injecting, per event: the idle-poller must never observe an
                        # unattributed edge from our own injection, and a long batch (60-event type,
                        # ~0.7s) outlives a single post-loop note's 0.5s attribution window.
                        self.note_agent_injected()
                        self.injector.inject(event); self._track(event); accepted += 1
                    if reason and (self._agent_keys or self._agent_buttons):
                        released += self._release_held()
                    if accepted:
                        self.control.set_owner("agent")
                        state = self.control.snapshot()
            else:
                state = self.control.snapshot()
                if expected_epoch is None or int(expected_epoch) != state["humanEpoch"]:
                    dropped, reason = len(events), "human_epoch_changed"
                elif display_revision is not None and int(display_revision) != state["worldRevision"]:
                    dropped, reason = len(events), "display_revision_changed"
                elif time.monotonic() - self._last_human < COOLDOWN_S:
                    dropped, reason = len(events), "human_active"
                elif proof_reason := self._validate_target_proof(target_proof, events):
                    dropped, reason = len(events), proof_reason
                else:
                    for event in events:
                        latest = self.control.snapshot()
                        if latest["humanEpoch"] != int(expected_epoch):
                            dropped += len(events) - accepted; reason = "human_epoch_changed"; break
                        if display_revision is None or latest["worldRevision"] != int(display_revision):
                            dropped += len(events) - accepted; reason = "display_revision_changed"; break
                        if event.get("t") == "k" and not self._keyboard_focus_matches(target_proof):
                            dropped += len(events) - accepted; reason = "focused_window_changed"; break
                        # Note BEFORE injecting, per event (see the agent_raw loop above).
                        self.note_agent_injected()
                        self.injector.inject(event); self._track(event); accepted += 1
                    if reason and (self._agent_keys or self._agent_buttons):
                        released += self._release_held()
                    if accepted:
                        self.control.set_owner("agent")
                        state = self.control.snapshot()
            self._dropped += dropped
            try: x, y = self.injector.cursor()
            except Exception: x, y = 0, 0
            receipt = {"sequence": self._sequence, "accepted": dropped == 0,
                       "acceptedEvents": accepted, "droppedEvents": dropped,
                       "owner": "human" if actor == "human" else state["owner"],
                       "humanEpoch": state["humanEpoch"], "worldRevision": state["worldRevision"],
                       "reason": reason, "releasedHeldInputs": released,
                       "actualCursorX": x, "actualCursorY": y}
            self._last_receipt = receipt
            return receipt

    def snapshot(self):
        with self._lock:
            state = self.control.snapshot(); width, height = self.injector.display_size()
            try:
                cur_x, cur_y = self.injector.cursor()
            except Exception:
                cur_x, cur_y = None, None
            return {**state, "agentDropped": self._dropped,
                    "heldAgentKeys": len(self._agent_keys), "heldAgentButtons": len(self._agent_buttons),
                    "display": {"displayRevision": state["worldRevision"], "logicalWidth": width,
                                "logicalHeight": height, "encodedWidth": width, "encodedHeight": height,
                                "deviceScale": 1.0, "rotation": 0},
                    # live pointer position so an overlay tracks the cursor between input events
                    "cursorX": cur_x, "cursorY": cur_y,
                    "lastReceipt": self._last_receipt}

    def freeze_barrier(self):
        """Synchronously release agent-held physical state before suspension."""
        with self._lock:
            released = self._release_held()
            state = self.control.human_takeover()
            self._last_human = time.monotonic()
            return {**state, "releasedHeldInputs": released, "barrier": "freeze"}


def _key():
    try: return open(AGENT_KEY_FILE, encoding="utf-8").read().strip()
    except OSError: return ""


def _websocket_origin(ws):
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None) or getattr(ws, "request_headers", {})
    return headers.get("Origin")


CONTROL = None
ARBITER = None
INJECTION_LOCK = asyncio.Lock()


def serve_state():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(ARBITER.snapshot(), separators=(",", ":")).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            # Read-only co-play state: allow cross-origin GET so a local overlay viewer (or the
            # widget's fallback poll) can read owner + cursor position. POST stays Origin-blocked.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            if self.headers.get("Origin") is not None:
                self.send_error(403); return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.send_error(415); return
            if self.path == "/lifecycle/pre-freeze":
                result = ARBITER.freeze_barrier()
            elif self.path == "/lifecycle/post-thaw":
                result = CONTROL.human_takeover()
            else:
                self.send_error(404); return
            body = json.dumps(result, separators=(",", ":")).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    http.server.ThreadingHTTPServer((HOST, STATE_PORT), Handler).serve_forever()


async def handler(ws):
    if _websocket_origin(ws) is not None:
        await ws.close(code=1008, reason="direct browser input is forbidden")
        return
    agent = False
    async for raw in ws:
        try:
            if isinstance(raw, bytes) or len(raw) > 256 * 1024:
                raise ValueError("invalid input payload")
            message = json.loads(raw)
            if not isinstance(message, dict): raise ValueError("input object required")
            if message.get("t") == "auth":
                key = _key(); agent = bool(key) and hmac.compare_digest(str(message.get("key", "")), key)
                await ws.send(json.dumps({"authenticated": agent})); continue
            events = message.get("events") if message.get("t") == "batch" else [message]
            # An authenticated batch may opt into the open CUA "raw" path (no proof/epoch);
            # unauthenticated input is always "human". Raw still yields to the human via cooldown.
            if agent:
                actor = "agent_raw" if message.get("mode") == "raw" else "agent"
            else:
                actor = "human"
            async with INJECTION_LOCK:
                receipt = ARBITER.submit(actor, events,
                    message.get("expected_human_epoch"), message.get("display_revision"),
                    message.get("sequence"), message.get("target_proof"))
            await ws.send(json.dumps(receipt, separators=(",", ":")))
        except Exception as exc:
            await ws.send(json.dumps({"accepted": False, "reason": "invalid_input", "detail": str(exc)[:200]}))


async def main():
    global CONTROL, ARBITER
    import websockets
    CONTROL = ControlState(os.environ.get("PAIRPUTER_CONTROL_STATE_DIR", "/run/pairputer"))
    ARBITER = InputArbiter(XTestInjector(os.environ.get("DISPLAY", ":1")), CONTROL)
    threading.Thread(target=serve_state, daemon=True).start()

    def _human_detect_loop():
        # Poll the X display so a human driving via VNC/console (input the arbiter never sees on its
        # own channel) still flips owner -> human, and a stale owner decays to idle. Uses its OWN Xlib
        # connection so idle reads never contend with the injector's input connection.
        try:
            probe = XTestInjector(os.environ.get("DISPLAY", ":1"))
        except Exception:
            return
        while True:
            try:
                idle = probe.display_idle_ms()
                ARBITER.detect_display_human_activity(idle_ms=idle)
            except Exception:
                pass
            time.sleep(HUMAN_DETECT_POLL_S)
    threading.Thread(target=_human_detect_loop, daemon=True, name="human-detect").start()

    async with websockets.serve(handler, HOST, PORT, max_size=256 * 1024):
        await asyncio.Future()


if __name__ == "__main__": asyncio.run(main())
