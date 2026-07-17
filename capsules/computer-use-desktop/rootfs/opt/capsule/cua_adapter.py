#!/usr/bin/env python3
"""CUA adapter: translate the stock computer-use action vocabulary (OpenAI CUA,
Anthropic Computer Use) into the capsule's primitive XTEST events (t=m/b/k).

The point is DROP-IN openness: a frontier host's existing computer-use loop emits
{action: "click", x, y} / {action: "type", text} / {action: "key", ...}. We map that
1:1 onto move/button/key events and hand them to the input arbiter as the "agent_raw"
actor — no target_proof, no epoch bookkeeping required from the caller. The human-first
arbiter still preempts instantly, and the VM is disposable, so this is safe to open.

This module is pure translation (no I/O), so it is trivially unit-testable. The bridge
route calls `to_events()` then submits the batch over the existing :6904 channel with
mode="raw".

Supported actions (superset of OpenAI CUA + Anthropic Computer Use):
  click / left_click / double_click / right_click / middle_click
  mouse_down / mouse_up / move / mouse_move
  scroll            (x, y, scroll_x, scroll_y  |  direction+amount)
  type              (text)
  key / keypress    (keys: "ctrl+s" or ["ctrl","s"], or Anthropic "key": "Return")
  drag              (path: [[x,y],...]  |  from/to)
  wait              (ms)  -> no events; the caller/bridge sleeps
  screenshot        -> no events; handled by the screenshot tool, not here
"""
from __future__ import annotations

# XTEST button numbers are 1-based at the injector (it adds +1 to event["button"]),
# so here "button" is 0-based: 0=left, 1=middle, 2=right, 3=wheel-up, 4=wheel-down.
_LEFT, _MIDDLE, _RIGHT, _WHEEL_UP, _WHEEL_DOWN = 0, 1, 2, 3, 4

# Map friendly key names (CUA/Anthropic spellings) to the injector's key vocabulary.
# The injector resolves these via XK.string_to_keysym, so X keysym names pass through;
# we normalize the common aliases the frontier loops emit.
_KEY_ALIASES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "space", "spacebar": "space", "backspace": "BackSpace",
    "delete": "Delete", "del": "Delete", "up": "Up", "down": "Down", "left": "Left",
    "right": "Right", "home": "Home", "end": "End", "pageup": "Prior", "page_up": "Prior",
    "pagedown": "Next", "page_down": "Next", "ctrl": "Control", "control": "Control",
    "cmd": "Super", "super": "Super", "win": "Super", "alt": "Alt", "option": "Alt",
    "shift": "Shift", "capslock": "Caps_Lock", "insert": "Insert",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}

_MODIFIERS = {"Control", "Alt", "Shift", "Super"}


class CuaError(ValueError):
    """A CUA action we cannot translate — surfaced to the caller, never guessed."""


def _norm_key(token: str) -> str:
    t = str(token).strip()
    if not t:
        raise CuaError("empty key token")
    return _KEY_ALIASES.get(t.lower(), t)


def _key_chord(keys) -> list[dict]:
    """A modifier chord: press all keys down in order, release in reverse."""
    if isinstance(keys, str):
        # "ctrl+s" or "cmd+shift+p"; a lone "+" means the plus key
        parts = [p for p in keys.replace(" ", "").split("+") if p] or ["+"]
    elif isinstance(keys, (list, tuple)):
        parts = list(keys)
    else:
        raise CuaError("keys must be a string or list")
    resolved = [_norm_key(k) for k in parts]
    events = [{"t": "k", "key": k, "down": True} for k in resolved]
    events += [{"t": "k", "key": k, "down": False} for k in reversed(resolved)]
    return events


def _type_text(text: str) -> list[dict]:
    """Type a literal string as individual key up/down events. The injector handles
    shift for uppercase/symbols via its needs_shift path, so we send the char as key."""
    events: list[dict] = []
    for ch in str(text):
        if ch == "\n":
            key = "Return"
        elif ch == "\t":
            key = "Tab"
        else:
            key = ch
        events.append({"t": "k", "key": key, "down": True})
        events.append({"t": "k", "key": key, "down": False})
    return events


def _click(button: int, x=None, y=None, count: int = 1) -> list[dict]:
    events: list[dict] = []
    if x is not None and y is not None:
        events.append({"t": "m", "x": int(x), "y": int(y)})
    for _ in range(max(1, count)):
        events.append({"t": "b", "button": button, "down": True})
        events.append({"t": "b", "button": button, "down": False})
    return events


def _scroll(x, y, scroll_x, scroll_y) -> list[dict]:
    """Wheel scroll via button 4/5 (X11 convention). scroll_y>0 = down, <0 = up."""
    events: list[dict] = []
    if x is not None and y is not None:
        events.append({"t": "m", "x": int(x), "y": int(y)})
    # one wheel "click" per unit; clamp to a sane cap so a huge value can't flood
    def wheel(button, ticks):
        for _ in range(min(int(abs(ticks)), 32)):
            events.append({"t": "b", "button": button, "down": True})
            events.append({"t": "b", "button": button, "down": False})
    sy = int(scroll_y or 0)
    if sy:
        wheel(_WHEEL_DOWN if sy > 0 else _WHEEL_UP, sy)
    # horizontal wheel is buttons 6/7 on X11; reuse the +1 offset (5=btn6, 6=btn7)
    sx = int(scroll_x or 0)
    if sx:
        wheel(5 if sx > 0 else 6, sx)
    return events


def _drag(points) -> list[dict]:
    if not points or len(points) < 2:
        raise CuaError("drag needs at least a start and end point")
    (x0, y0) = points[0]
    events = [{"t": "m", "x": int(x0), "y": int(y0)},
              {"t": "b", "button": _LEFT, "down": True}]
    for (x, y) in points[1:]:
        events.append({"t": "m", "x": int(x), "y": int(y)})
    events.append({"t": "b", "button": _LEFT, "down": False})
    return events


def to_events(action: dict) -> tuple[list[dict], int]:
    """Translate one CUA action -> (events, wait_ms). wait_ms>0 means the caller
    should sleep that long and inject no events (a 'wait' action)."""
    if not isinstance(action, dict):
        raise CuaError("action must be an object")
    kind = str(action.get("action") or action.get("type") or "").lower().strip()
    if not kind:
        raise CuaError("action.action (or .type) is required")

    x, y = action.get("x"), action.get("y")
    # Anthropic nests the point under "coordinate": [x, y]
    if x is None and isinstance(action.get("coordinate"), (list, tuple)) and len(action["coordinate"]) == 2:
        x, y = action["coordinate"]

    if kind in ("click", "left_click"):
        return _click(_LEFT, x, y), 0
    if kind in ("double_click", "double-click", "doubleclick"):
        return _click(_LEFT, x, y, count=2), 0
    if kind in ("right_click", "right-click"):
        return _click(_RIGHT, x, y), 0
    if kind in ("middle_click", "middle-click"):
        return _click(_MIDDLE, x, y), 0
    if kind in ("move", "mouse_move", "cursor_position"):
        if x is None or y is None:
            raise CuaError("move requires x,y")
        return [{"t": "m", "x": int(x), "y": int(y)}], 0
    if kind in ("mouse_down", "left_mouse_down"):
        pre = [{"t": "m", "x": int(x), "y": int(y)}] if x is not None else []
        return pre + [{"t": "b", "button": _LEFT, "down": True}], 0
    if kind in ("mouse_up", "left_mouse_up"):
        pre = [{"t": "m", "x": int(x), "y": int(y)}] if x is not None else []
        return pre + [{"t": "b", "button": _LEFT, "down": False}], 0
    if kind == "scroll":
        sx = action.get("scroll_x", 0)
        sy = action.get("scroll_y")
        if sy is None:
            # direction+amount form (Anthropic): {scroll_direction, scroll_amount}
            direction = str(action.get("scroll_direction", "down")).lower()
            amount = int(action.get("scroll_amount", 3))
            sy = amount if direction == "down" else -amount if direction == "up" else 0
            sx = amount if direction == "right" else -amount if direction == "left" else sx
        return _scroll(x, y, sx, sy), 0
    if kind in ("type", "text"):
        return _type_text(action.get("text", "")), 0
    if kind in ("key", "keypress", "key_press", "hotkey"):
        keys = action.get("keys")
        if keys is None:
            keys = action.get("key") or action.get("text")
        if keys is None:
            raise CuaError("key action requires 'keys' (or 'key'/'text')")
        return _key_chord(keys), 0
    if kind in ("drag", "left_click_drag"):
        path = action.get("path") or action.get("points")
        if path is None and action.get("from") and action.get("to"):
            path = [action["from"], action["to"]]
        if path is None and x is not None and action.get("to"):
            path = [[x, y], action["to"]]
        return _drag(path), 0
    if kind == "wait":
        ms = int(action.get("ms") or action.get("duration") or 500)
        return [], max(0, min(ms, 10000))
    if kind == "screenshot":
        # handled by the screenshot tool; no input events
        return [], 0
    raise CuaError(f"unsupported CUA action: {kind!r}")


if __name__ == "__main__":  # tiny self-check
    ev, _ = to_events({"action": "click", "x": 10, "y": 20})
    assert ev == [{"t": "m", "x": 10, "y": 20},
                  {"t": "b", "button": 0, "down": True},
                  {"t": "b", "button": 0, "down": False}], ev
    ev, _ = to_events({"action": "key", "keys": "ctrl+s"})
    assert ev[0] == {"t": "k", "key": "Control", "down": True}
    assert ev[1] == {"t": "k", "key": "s", "down": True}
    assert ev[-1] == {"t": "k", "key": "Control", "down": False}
    ev, _ = to_events({"action": "type", "text": "hi"})
    assert ev[0]["key"] == "h" and ev[0]["down"] is True
    ev, _ = to_events({"type": "key", "key": "Return"})  # Anthropic shape
    assert ev[0]["key"] == "Return"
    ev, w = to_events({"action": "wait", "ms": 250})
    assert ev == [] and w == 250
    ev, _ = to_events({"action": "double_click", "x": 5, "y": 5})
    assert sum(1 for e in ev if e["t"] == "b" and e["down"]) == 2
    ev, _ = to_events({"action": "scroll", "x": 1, "y": 2, "scroll_y": 3})
    assert sum(1 for e in ev if e["t"] == "b" and e["down"]) == 3
    print("cua_adapter self-check OK")
