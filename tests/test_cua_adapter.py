"""CUA adapter: stock computer-use action vocab -> primitive XTEST events.

Covers the drop-in path for OpenAI CUA / Anthropic Computer Use loops. The adapter is
pure translation (no I/O), so these run without a desktop.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "capsules/computer-use-desktop/rootfs/opt/capsule"))
from cua_adapter import to_events, CuaError  # noqa: E402


def _btn_downs(events):
    return [e for e in events if e["t"] == "b" and e["down"]]


def test_click_moves_then_presses_left():
    events, wait = to_events({"action": "click", "x": 100, "y": 200})
    assert wait == 0
    assert events[0] == {"t": "m", "x": 100, "y": 200}
    assert events[1] == {"t": "b", "button": 0, "down": True}
    assert events[2] == {"t": "b", "button": 0, "down": False}


def test_double_click_presses_twice():
    events, _ = to_events({"action": "double_click", "x": 5, "y": 5})
    assert len(_btn_downs(events)) == 2


def test_right_and_middle_click_buttons():
    assert _btn_downs(to_events({"action": "right_click", "x": 1, "y": 1})[0])[0]["button"] == 2
    assert _btn_downs(to_events({"action": "middle_click", "x": 1, "y": 1})[0])[0]["button"] == 1


def test_anthropic_coordinate_shape():
    events, _ = to_events({"action": "left_click", "coordinate": [42, 43]})
    assert events[0] == {"t": "m", "x": 42, "y": 43}


def test_type_emits_key_pairs():
    events, _ = to_events({"action": "type", "text": "ab"})
    assert [e["key"] for e in events] == ["a", "a", "b", "b"]
    assert [e["down"] for e in events] == [True, False, True, False]


def test_type_newline_and_tab_map_to_named_keys():
    events, _ = to_events({"action": "type", "text": "\n\t"})
    assert events[0]["key"] == "Return"
    assert events[2]["key"] == "Tab"


def test_key_chord_string_press_then_reverse_release():
    events, _ = to_events({"action": "key", "keys": "ctrl+shift+s"})
    downs = [e["key"] for e in events if e["down"]]
    ups = [e["key"] for e in events if not e["down"]]
    assert downs == ["Control", "Shift", "s"]
    assert ups == ["s", "Shift", "Control"]  # released in reverse


def test_key_list_form_and_anthropic_single_key():
    assert [e["key"] for e in to_events({"action": "keypress", "keys": ["cmd", "a"]})[0] if e["down"]] == ["Super", "a"]
    # Anthropic emits {"action":"key","text":"Return"} or {"key":"Return"}
    events, _ = to_events({"type": "key", "key": "Return"})
    assert events[0]["key"] == "Return"


def test_scroll_down_uses_wheel_button_and_count():
    events, _ = to_events({"action": "scroll", "x": 1, "y": 2, "scroll_y": 3})
    assert events[0] == {"t": "m", "x": 1, "y": 2}
    assert len(_btn_downs(events)) == 3
    assert all(e["button"] == 4 for e in _btn_downs(events))  # wheel-down


def test_scroll_direction_amount_form():
    events, _ = to_events({"action": "scroll", "scroll_direction": "up", "scroll_amount": 2})
    assert len(_btn_downs(events)) == 2
    assert all(e["button"] == 3 for e in _btn_downs(events))  # wheel-up


def test_drag_path_holds_button_across_moves():
    events, _ = to_events({"action": "drag", "path": [[0, 0], [10, 10], [20, 20]]})
    assert events[0] == {"t": "m", "x": 0, "y": 0}
    assert events[1] == {"t": "b", "button": 0, "down": True}
    assert events[-1] == {"t": "b", "button": 0, "down": False}
    moves = [e for e in events if e["t"] == "m"]
    assert moves[-1] == {"t": "m", "x": 20, "y": 20}


def test_wait_returns_no_events_and_ms():
    events, wait = to_events({"action": "wait", "ms": 750})
    assert events == [] and wait == 750


def test_wait_is_clamped():
    _, wait = to_events({"action": "wait", "ms": 999999})
    assert wait == 10000


def test_screenshot_action_yields_no_input():
    assert to_events({"action": "screenshot"}) == ([], 0)


def test_unsupported_action_fails_loudly():
    with pytest.raises(CuaError):
        to_events({"action": "teleport", "x": 1, "y": 1})
    with pytest.raises(CuaError):
        to_events({})  # no action


def test_move_requires_coordinates():
    with pytest.raises(CuaError):
        to_events({"action": "move"})
