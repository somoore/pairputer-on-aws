#!/usr/bin/env python3
"""A11y-Compressor-style compaction for accessibility trees.

The raw AT-SPI tree is dominated by REDUNDANT and INERT nodes — unnamed layout
containers, decorative separators/fillers, and duplicate text — that crowd out the
handful of actionable elements a model actually needs. A11y-Compressor (ACL 2026)
reports that reducing that redundancy cuts a11y input tokens to ~22% of raw (−78%)
while IMPROVING task success (+5.1pp), because the model stops drowning in noise.

We adopt the idea as two pure, testable passes over the flat node list the observer
already produces (`{name, role, visible, showing, actions, depth, appIdentity}`):

  1. keep(node)      — drop inert nodes: no name AND no actions AND a non-actionable
                       role (pure containers/decoration). Named or actionable nodes
                       always survive; a named container is still a landmark.
  2. dedup(nodes)    — collapse consecutive siblings with identical (name, role) that
                       carry no distinguishing action (e.g. a list of 300 blank cells).

This runs BEFORE the observer's max_nodes cap, so the budget is spent on signal, not
noise — a relevant deep node no longer gets starved by 500 shallow inert ones.

Pure functions, no AT-SPI import, so this is trivially unit-testable off a desktop.
"""
from __future__ import annotations

# Roles that are meaningful even when unnamed (landmarks / operables). Everything not
# here, if ALSO unnamed and action-less, is treated as inert scaffolding.
_MEANINGFUL_ROLES = {
    "push button", "button", "toggle button", "check box", "checkbox", "radio button",
    "radio", "menu item", "menu", "link", "entry", "text", "text box", "textbox",
    "combo box", "combobox", "list item", "tab", "tab list", "slider", "spin button",
    "switch", "option", "heading", "label", "document web", "dialog", "alert",
    "page tab", "tree item", "table cell", "icon", "image",
}

# Roles that are almost always pure layout/decoration when unnamed and action-less.
_SCAFFOLD_ROLES = {
    "filler", "separator", "panel", "section", "grouping", "group", "redundant object",
    "scroll pane", "viewport", "layered pane", "split pane", "unknown", "",
}


def _norm(text) -> str:
    return str(text or "").strip().lower()


def is_inert(node: dict) -> bool:
    """A node carries no semantic signal: unnamed, no actions, and a scaffold role."""
    if not isinstance(node, dict):
        return True
    name = _norm(node.get("name"))
    if name:
        return False  # a name is signal (landmark, label, heading, …)
    actions = node.get("actions")
    if isinstance(actions, (list, tuple)) and actions:
        return False  # operable even if unnamed
    role = _norm(node.get("role"))
    if role in _MEANINGFUL_ROLES:
        return False  # e.g. an unnamed icon/image can still be a target
    # unnamed + action-less + (scaffold role or anything unrecognized) => inert
    return role in _SCAFFOLD_ROLES or role not in _MEANINGFUL_ROLES


def compact_nodes(nodes: list[dict], *, drop_inert: bool = True,
                  dedup: bool = True) -> tuple[list[dict], dict]:
    """Return (compacted_nodes, stats). Order-preserving. stats = {input, kept,
    dropped_inert, dropped_dup} so the observer can report how much it saved."""
    stats = {"input": 0, "kept": 0, "dropped_inert": 0, "dropped_dup": 0}
    out: list[dict] = []
    prev_key = None
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        stats["input"] += 1
        if drop_inert and is_inert(node):
            stats["dropped_inert"] += 1
            continue
        # collapse consecutive identical (name, role) siblings with no actions
        actions = node.get("actions")
        has_action = isinstance(actions, (list, tuple)) and bool(actions)
        key = (_norm(node.get("name")), _norm(node.get("role")))
        if dedup and not has_action and key == prev_key and key != ("", ""):
            stats["dropped_dup"] += 1
            continue
        prev_key = key if not has_action else None
        out.append(node)
        stats["kept"] += 1
    return out, stats


if __name__ == "__main__":  # self-check
    raw = [
        {"name": "", "role": "filler", "actions": [], "depth": 1},           # inert
        {"name": "", "role": "panel", "actions": [], "depth": 1},            # inert
        {"name": "Save", "role": "push button", "actions": ["click"], "depth": 3},
        {"name": "", "role": "separator", "actions": [], "depth": 3},        # inert
        {"name": "Files", "role": "label", "actions": [], "depth": 2},       # named -> kept
        {"name": "row", "role": "table cell", "actions": [], "depth": 5},
        {"name": "row", "role": "table cell", "actions": [], "depth": 5},    # dup -> dropped
        {"name": "row", "role": "table cell", "actions": [], "depth": 5},    # dup -> dropped
        {"name": "", "role": "icon", "actions": ["activate"], "depth": 4},   # operable icon -> kept
    ]
    out, stats = compact_nodes(raw)
    names = [(n.get("name"), n.get("role")) for n in out]
    assert ("Save", "push button") in names
    assert ("Files", "label") in names
    assert stats["dropped_inert"] == 3, stats
    assert stats["dropped_dup"] == 2, stats
    assert stats["kept"] == 4, stats            # Save, Files, one row, icon
    # named/operable never dropped
    assert all(not is_inert(n) for n in out)
    print("atspi_compact self-check OK:", stats)
