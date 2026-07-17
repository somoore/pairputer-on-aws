"""Deterministic element grounding (Prune4Web-style): rank UI elements by intent so the
model picks from a short list instead of grounding blind. Pure function, no desktop needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "capsules/computer-use-desktop/rootfs/opt/capsule"))
from element_grounding import rank_elements, score_element, _tokens  # noqa: E402


def _tree():
    return [
        {"name": "Save", "role": "push button", "actions": ["click"], "visible": True, "depth": 3},
        {"name": "Save As…", "role": "menu item", "visible": True, "depth": 5},
        {"name": "Cancel", "role": "push button", "visible": True, "depth": 3},
        {"name": "Open", "role": "push button", "visible": True, "depth": 3},
        {"name": "Filename", "role": "entry", "visible": True, "depth": 4},
        {"name": "Search the web", "role": "entry", "visible": True, "depth": 4},
        {"name": "document body", "role": "panel", "visible": True, "depth": 1},
        {"name": "Save", "role": "push button", "visible": False, "depth": 9},  # hidden dup
    ]


def test_click_save_button_ranks_the_visible_save_first():
    out = rank_elements("click the Save button", _tree(), top_k=3)
    top = out["candidates"][0]
    assert top["name"] == "Save" and top["role"] == "push button"
    assert top["mark"] == 1
    assert out["considered"] == 8


def test_visible_element_outranks_hidden_duplicate():
    out = rank_elements("click Save", _tree(), top_k=8)
    saves = [c for c in out["candidates"] if c["name"] == "Save"]
    assert len(saves) == 2
    assert saves[0]["score"] > saves[1]["score"]  # visible beats hidden


def test_type_intent_surfaces_the_entry_over_buttons():
    out = rank_elements("type the filename", _tree(), top_k=3)
    assert out["candidates"][0]["role"] == "entry"
    assert out["candidates"][0]["name"] == "Filename"


def test_inert_container_never_ranks_first():
    out = rank_elements("click Save", _tree(), top_k=5)
    assert out["candidates"][0]["role"] != "panel"


def test_pruning_returns_far_fewer_than_considered():
    # a big noisy tree -> a short candidate list (the whole point)
    noise = [{"name": f"row {i}", "role": "list item", "visible": True, "depth": 6}
             for i in range(200)]
    tree = _tree() + noise
    out = rank_elements("click the Save button", tree, top_k=5)
    assert out["returned"] <= 5
    assert out["considered"] == len(tree)
    assert out["truncated"] is True
    assert out["candidates"][0]["name"] == "Save"


def test_dom_ax_element_shape_is_supported():
    # DOM/AX nodes carry attributes/value/text rather than AT-SPI actions
    dom = [
        {"role": "button", "name": "Submit order", "backendNodeId": 42,
         "attributes": {"aria-label": "Submit order"}, "bounds": [10, 20, 80, 24]},
        {"role": "textbox", "name": "", "attributes": {"placeholder": "Email address"}, "backendNodeId": 7},
    ]
    out = rank_elements("submit the order", dom, top_k=2)
    assert out["candidates"][0]["backendNodeId"] == 42
    # placeholder text is matchable for a "type email" intent
    email = rank_elements("type my email address", dom, top_k=2)
    assert any(c.get("backendNodeId") == 7 for c in email["candidates"])


def test_empty_or_irrelevant_intent_returns_nothing_confidently():
    out = rank_elements("xyzzy nonexistent widget", _tree(), top_k=5)
    # nothing scores above threshold -> empty, not a wrong guess
    assert out["returned"] == 0
    assert out["candidates"] == []


def test_score_is_deterministic():
    el = {"name": "Save", "role": "push button", "visible": True}
    toks = _tokens("click the save button")
    verbs = {"click"}
    assert score_element(el, toks, verbs) == score_element(el, toks, verbs)
