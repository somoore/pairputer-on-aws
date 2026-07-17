"""Typed, bounded success-predicate assertion contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


CAPSULE = Path(__file__).parents[1] / "capsules" / "computer-use-desktop" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE))

from task_contract import EvidenceAssertion, Step, evaluate_evidence_assertions  # noqa: E402


def assertion(operator: str, expected=...):
    value = {
        "predicate": "research_complete",
        "path": "results.items",
        "operator": operator,
    }
    if expected is not ...:
        value["expected"] = expected
    return EvidenceAssertion.from_dict(value)


def test_allowlisted_operators_evaluate_bounded_nested_observations():
    observed = {
        "results": {
            "items": ["white Jordan 1", "white Jordan 4", "white Jordan 11"],
            "title": "Size 12 white Air Jordan research",
            "ready": True,
        },
    }
    assert assertion("equals", ["white Jordan 1", "white Jordan 4", "white Jordan 11"]).evaluate(observed)
    assert assertion("contains", "white Jordan 4").evaluate(observed)
    assert assertion("contains_all", ["white Jordan 1", "white Jordan 11"]).evaluate(observed)
    assert assertion("contains_any", ["missing", "white Jordan 4"]).evaluate(observed)
    assert assertion("non_empty").evaluate(observed)
    assert assertion("truthy").evaluate(observed)
    assert assertion("count_at_least", 3).evaluate(observed)
    title = EvidenceAssertion.from_dict({
        "predicate": "research_complete", "path": "results.title",
        "operator": "contains_all", "expected": ["Size 12", "white", "Jordan"],
    })
    ready = EvidenceAssertion.from_dict({
        "predicate": "research_complete", "path": "results.ready", "operator": "truthy",
    })
    assert title.evaluate(observed) and ready.evaluate(observed)
    count_text = EvidenceAssertion.from_dict({
        "predicate": "research_complete", "path": "results.title",
        "operator": "count_at_least", "expected": 3,
    })
    assert count_text.evaluate(observed) is False


def test_dot_path_traverses_lists_and_missing_or_oversized_paths_fail_closed():
    item = EvidenceAssertion.from_dict({
        "predicate": "price_observed", "path": "results.0.price",
        "operator": "equals", "expected": "$120.00",
    })
    observed = {"results": [{"price": "$120.00"}]}
    assert item.evaluate(observed)
    assert not item.evaluate({"results": []})
    assert not item.evaluate({"results": [{"other": "$120.00"}]})
    assert not assertion("non_empty").evaluate({"results": {"items": [0] * 4097}})


def test_equals_is_type_strict_and_collection_operators_do_not_coerce():
    numeric = EvidenceAssertion.from_dict({
        "predicate": "count", "path": "value", "operator": "equals", "expected": 1,
    })
    assert numeric.evaluate({"value": 1})
    assert not numeric.evaluate({"value": True})
    assert not numeric.evaluate({"value": 1.0})
    contains = EvidenceAssertion.from_dict({
        "predicate": "count", "path": "values", "operator": "contains", "expected": 1,
    })
    assert not contains.evaluate({"values": [True]})


def test_expected_fact_binding_is_explicit_and_contains_any_binds_every_alternative():
    bound = EvidenceAssertion.from_dict({
        "predicate": "main contains $215 and White Metallic",
        "path": "matches.0.text", "operator": "contains_all",
        "expected": ["$215", "White/Metallic"],
    })
    assert bound.explicitly_represents_expected_facts()
    partial_any = EvidenceAssertion.from_dict({
        "predicate": "main contains a white result",
        "path": "matches.0.text", "operator": "contains_any",
        "expected": ["white", "silver"],
    })
    assert not partial_any.explicitly_represents_expected_facts()
    vacuous_any = EvidenceAssertion.from_dict({
        "predicate": "main contains white",
        "path": "matches.0.text", "operator": "contains_any",
        "expected": ["", "white"],
    })
    assert not vacuous_any.explicitly_represents_expected_facts()
    bounded_count = EvidenceAssertion.from_dict({
        "predicate": "at least 3 offers were observed",
        "path": "matches", "operator": "count_at_least", "expected": 3,
    })
    assert bounded_count.explicitly_represents_expected_facts()
    zero_count = EvidenceAssertion.from_dict({
        "predicate": "at least 0 offers were observed",
        "path": "matches", "operator": "count_at_least", "expected": 0,
    })
    assert not zero_count.explicitly_represents_expected_facts()


@pytest.mark.parametrize("value", [
    {},
    {"predicate": "p", "path": "x"},
    {"predicate": "p", "path": "x", "operator": "regex", "expected": "x"},
    {"predicate": "p", "path": "x", "operator": "truthy", "expected": True},
    {"predicate": "p", "path": "x", "operator": "equals"},
    {"predicate": "p", "path": "x", "operator": "contains_all", "expected": []},
    {"predicate": "p", "path": "x", "operator": "count_at_least", "expected": True},
    {"predicate": "p", "path": "x", "operator": "count_at_least", "expected": 10001},
    {"predicate": "p", "path": "__class__", "operator": "truthy"},
    {"predicate": "p", "path": "x.-1", "operator": "truthy"},
    {"predicate": "p", "path": "x", "operator": "truthy", "surprise": 1},
    {"predicate": None, "path": "x", "operator": "truthy"},
    {"predicate": "p", "path": ["x"], "operator": "truthy"},
    {"predicate": "p", "path": "x", "operator": ["truthy"]},
])
def test_assertion_parser_rejects_ambiguous_unsupported_or_unbounded_specs(value):
    with pytest.raises(ValueError):
        EvidenceAssertion.from_dict(value)


def test_expected_values_are_json_only_finite_and_bounded():
    for expected in ({1, 2}, object(), float("nan"), "x" * 17000, list(range(129)), 1 << 5000):
        with pytest.raises(ValueError):
            EvidenceAssertion.from_dict({
                "predicate": "p", "path": "value", "operator": "equals", "expected": expected,
            })


def test_spec_and_group_digests_are_canonical_and_order_independent():
    first = EvidenceAssertion.from_dict({
        "operator": "contains", "expected": "Jordan", "path": "title", "predicate": "match",
    })
    same = EvidenceAssertion.from_dict(json.loads(json.dumps(first.as_dict(), sort_keys=True)))
    second = EvidenceAssertion.from_dict({
        "predicate": "match", "path": "size", "operator": "equals", "expected": 12,
    })
    assert first.spec_digest == same.spec_digest
    one = evaluate_evidence_assertions((first, second), {"title": "Air Jordan", "size": 12})
    two = evaluate_evidence_assertions((second, first), {"title": "Air Jordan", "size": 12})
    assert one == two
    assert one["match"]["verified"] is True
    assert one["match"]["summary"] == "2/2 evidence assertions passed"
    failed = evaluate_evidence_assertions((first, second), {"title": "Air Jordan", "size": 11})
    assert failed["match"]["verified"] is False
    assert failed["match"]["spec_digest"] == one["match"]["spec_digest"]


def test_step_round_trips_explicit_assertion_mappings_and_rejects_shorthand():
    data = {
        "step_id": "research", "skill": "browser.query", "arguments": {"selector": "body"},
        "success_predicates": ["research_complete"],
        "evidence_assertions": [
            {"predicate": "research_complete", "path": "matches", "operator": "count_at_least", "expected": 3},
            {"predicate": "research_complete", "path": "url", "operator": "non_empty"},
        ],
    }
    step = Step.from_dict(data)
    assert len(step.evidence_assertions) == 2
    assert Step.from_dict(step.as_dict()).as_dict() == step.as_dict()
    with pytest.raises(ValueError, match="explicit mapping"):
        Step.from_dict({**data, "evidence_assertions": ["matches_non_empty"]})
    with pytest.raises(ValueError, match="declared success predicates"):
        Step.from_dict({
            **data,
            "evidence_assertions": [
                {"predicate": "undeclared", "path": "matches", "operator": "non_empty"},
            ],
        })
    duplicate = data["evidence_assertions"][0]
    with pytest.raises(ValueError, match="unique"):
        Step.from_dict({**data, "evidence_assertions": [duplicate, dict(duplicate)]})
