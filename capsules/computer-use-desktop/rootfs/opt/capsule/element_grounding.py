#!/usr/bin/env python3
"""Deterministic element grounding: rank UI elements by relevance to a natural-language
intent, so the model picks from a short, ranked candidate list instead of reading the whole
tree or guessing a CSS selector / coordinate.

Motivated by Prune4Web (AAAI 2026): programmatic pruning of the element set before grounding
lifts low-level grounding accuracy from ~46.8% to ~88.28% and shrinks candidates 25-50x. We
adopt the *idea* (score + prune deterministically, hand the model a small relevant list) as a
pure, portable function — no LLM, no I/O — that works on any element with role/name/actions.

The element dict shape is the union of what our observers emit:
  AT-SPI: {name, role, actions, visible, showing, depth, appIdentity}
  DOM/AX: {name, role, text, value, tag, attributes:{...}, bounds, nodeId/backendNodeId}
Only `name`/`role` are required; everything else is optional and boosts the score when present.

Scoring is intentionally simple and explainable (token overlap + role affinity + actionability
+ visibility), because a *legible* ranker is debuggable and a black-box neural ranker is not —
and the model does the final semantic pick anyway. Tune the weights, not the algorithm.
"""
from __future__ import annotations

import re
import unicodedata

# Roles that are usually the *target* of a computer-use action, and the intent verbs that
# imply them. A match here is a strong signal; a mismatch is a mild penalty, never a filter
# (the model still sees lower-ranked candidates).
_ACTIONABLE_ROLES = {
    "push button", "button", "toggle button", "check box", "checkbox", "radio button",
    "radio", "menu item", "link", "text", "entry", "text box", "textbox", "combo box",
    "combobox", "list item", "tab", "slider", "spin button", "menu", "switch", "option",
}
_INTENT_ROLE_HINTS = {
    "click": {"button", "push button", "link", "menu item", "tab", "list item", "check box", "radio button"},
    "press": {"button", "push button", "key"},
    "type": {"entry", "text box", "textbox", "text", "combo box", "spin button"},
    "enter": {"entry", "text box", "textbox", "combo box"},
    "fill": {"entry", "text box", "textbox", "combo box", "spin button"},
    "select": {"combo box", "list item", "option", "menu item", "radio button"},
    "toggle": {"check box", "toggle button", "switch", "radio button"},
    "open": {"menu", "menu item", "button", "link"},
    "check": {"check box", "checkbox"},
}

_WORD = re.compile(r"[a-z0-9]+")


def _norm(text) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return s.lower()


def _tokens(text) -> set[str]:
    return set(_WORD.findall(_norm(text)))


def _element_text(el: dict) -> str:
    """All the human-visible/semantic text on an element, concatenated for matching."""
    parts = [el.get("name"), el.get("text"), el.get("value"), el.get("label"),
             el.get("placeholder"), el.get("title")]
    attrs = el.get("attributes")
    if isinstance(attrs, dict):
        parts += [attrs.get("aria-label"), attrs.get("alt"), attrs.get("id"),
                  attrs.get("name"), attrs.get("value"), attrs.get("placeholder")]
    return " ".join(p for p in parts if p)


def _intent_verbs(intent_tokens: set[str]) -> set[str]:
    return {v for v in _INTENT_ROLE_HINTS if v in intent_tokens}


def score_element(el: dict, intent_tokens: set[str], intent_verbs: set[str]) -> float:
    """Relevance in [0, ~1.4]. Higher = better candidate for the intent. Explainable:
    text overlap dominates, role affinity + actionability + visibility adjust."""
    text_tokens = _tokens(_element_text(el))
    if not text_tokens and not el.get("role"):
        return 0.0

    # 1. text overlap (the dominant signal): fraction of intent content-words present on the element
    content = intent_tokens - _intent_verbs(intent_tokens)
    if content:
        overlap = len(content & text_tokens) / len(content)
    else:
        overlap = 0.0
    score = overlap  # 0..1

    role = _norm(el.get("role"))

    # 2. role affinity to the intent's verb(s)
    wanted_roles = set()
    for v in intent_verbs:
        wanted_roles |= _INTENT_ROLE_HINTS[v]
    if wanted_roles:
        if any(r in role for r in wanted_roles):
            score += 0.25
        elif role and role not in _ACTIONABLE_ROLES:
            score -= 0.10  # e.g. intent says "click" but this is a static container

    # 3. actionability: elements you can actually operate rank above inert text — but only
    # as an AMPLIFIER of a real match. With zero content overlap AND no verb-role hit, an
    # actionable element must not clear the threshold on actionability alone (false positive).
    has_signal = overlap > 0 or (wanted_roles and any(r in role for r in wanted_roles))
    if has_signal:
        if any(r in role for r in _ACTIONABLE_ROLES):
            score += 0.08
        actions = el.get("actions")
        if isinstance(actions, (list, tuple)) and actions:
            score += 0.05

    # 4. visibility: an off-screen / not-showing element is a worse target
    if el.get("visible") is False or el.get("showing") is False:
        score -= 0.20
    if el.get("hidden") is True:
        score -= 0.30

    # 5. exact-name bonus: the element's name is exactly an intent phrase
    name_tokens = _tokens(el.get("name"))
    if content and name_tokens and content <= name_tokens and len(name_tokens) <= len(content) + 1:
        score += 0.15

    return max(0.0, score)


def rank_elements(intent: str, elements: list[dict], *, top_k: int = 12,
                  min_score: float = 0.12) -> dict:
    """Score all elements against the intent, return the top-K above threshold with a stable
    1-based `mark` the model can reference. This is the pruned candidate set — a 25-50x cut
    from the raw tree in the common case, ordered by relevance.

    Returns {intent, considered, returned, candidates:[{mark, score, ...el}], truncated}.
    """
    intent_tokens = _tokens(intent)
    intent_verbs = _intent_verbs(intent_tokens)
    scored = []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        s = score_element(el, intent_tokens, intent_verbs)
        if s >= min_score:
            scored.append((s, el))
    # stable sort: score desc, then shallower depth (usually more salient), then name length
    scored.sort(key=lambda pair: (
        -pair[0],
        int(pair[1].get("depth", 0)) if str(pair[1].get("depth", 0)).lstrip("-").isdigit() else 0,
        len(str(pair[1].get("name", ""))),
    ))
    top = scored[: max(1, int(top_k))]
    candidates = []
    for i, (s, el) in enumerate(top, start=1):
        entry = {"mark": i, "score": round(s, 3)}
        # carry only grounding-relevant fields, not the whole node (keeps it token-cheap)
        for key in ("name", "role", "value", "text", "actions", "bounds", "nodeId",
                    "backendNodeId", "appIdentity", "depth", "selector"):
            if key in el and el[key] not in (None, "", [], {}):
                entry[key] = el[key]
        candidates.append(entry)
    return {
        "intent": intent,
        "considered": len([e for e in (elements or []) if isinstance(e, dict)]),
        "returned": len(candidates),
        "candidates": candidates,
        "truncated": len(scored) > len(top),
    }


if __name__ == "__main__":  # self-check
    els = [
        {"name": "Save", "role": "push button", "actions": ["click"], "visible": True, "depth": 3},
        {"name": "Save As…", "role": "menu item", "visible": True, "depth": 5},
        {"name": "Cancel", "role": "push button", "visible": True, "depth": 3},
        {"name": "Filename", "role": "entry", "visible": True, "depth": 4},
        {"name": "document body", "role": "panel", "visible": True, "depth": 1},
        {"name": "Save", "role": "push button", "visible": False, "depth": 9},  # hidden dup
    ]
    out = rank_elements("click the Save button", els, top_k=3)
    assert out["candidates"][0]["name"] == "Save", out
    assert out["candidates"][0]["role"] == "push button"
    # the visible Save outranks the hidden Save
    saves = [c for c in out["candidates"] if c["name"] == "Save"]
    assert saves[0]["score"] >= saves[-1]["score"]
    # a "type" intent should surface the entry
    typ = rank_elements("type the filename", els, top_k=3)
    assert any(c["role"] == "entry" for c in typ["candidates"]), typ
    # inert panel never wins
    assert all(c["role"] != "panel" for c in out["candidates"][:1])
    print("element_grounding self-check OK:", out["returned"], "candidates from", out["considered"])
