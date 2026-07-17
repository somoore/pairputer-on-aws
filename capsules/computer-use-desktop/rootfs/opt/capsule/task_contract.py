#!/usr/bin/env python3
"""Immutable task, revision, plan-step, and action-envelope contracts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping


_ASSERTION_OPERATORS = frozenset({
    "equals", "contains", "contains_all", "contains_any",
    "non_empty", "truthy", "count_at_least",
})
_ASSERTION_VALUE_OPERATORS = frozenset({
    "equals", "contains", "contains_all", "contains_any", "count_at_least",
})
_ASSERTION_PATH_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*)$")
_ASSERTION_UNSET = object()
_ASSERTION_MISSING = object()
_MAX_ASSERTIONS = 128
_MAX_ASSERTION_PATH_SEGMENTS = 32
_MAX_ASSERTION_EXPECTED_BYTES = 16 * 1024
_MAX_ASSERTION_COLLECTION = 128
_MAX_OBSERVED_COLLECTION = 4096
_MAX_OBSERVED_STRING = 1024 * 1024


def _validated_assertion_value(value: Any, *, depth: int = 0) -> Any:
    """Return a frozen JSON value, rejecting expensive or ambiguous inputs."""

    if depth > 8:
        raise ValueError("evidence assertion expected value exceeds maximum depth")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_ASSERTION_EXPECTED_BYTES:
            raise ValueError("evidence assertion string is too large")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value.bit_length() > 4096:
            raise ValueError("evidence assertion integer is too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence assertion numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_ASSERTION_COLLECTION:
            raise ValueError("evidence assertion mapping is too large")
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise ValueError("evidence assertion mapping keys must be bounded strings")
            clean[key] = _validated_assertion_value(item, depth=depth + 1)
        return MappingProxyType(clean)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_ASSERTION_COLLECTION:
            raise ValueError("evidence assertion collection is too large")
        return tuple(_validated_assertion_value(item, depth=depth + 1) for item in value)
    raise ValueError("evidence assertion expected values must be JSON-compatible")


def _strict_assertion_equal(left: Any, right: Any, *, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if len(left) > _MAX_ASSERTION_COLLECTION or len(right) > _MAX_ASSERTION_COLLECTION:
            return False
        if set(left) != set(right) or not all(isinstance(key, str) for key in left):
            return False
        return all(_strict_assertion_equal(left[key], right[key], depth=depth + 1) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right) or len(left) > _MAX_ASSERTION_COLLECTION:
            return False
        return all(_strict_assertion_equal(a, b, depth=depth + 1) for a, b in zip(left, right))
    # bool is an int subclass; exact types prevent True from satisfying equals: 1.
    return type(left) is type(right) and left == right


def _resolve_assertion_path(observed: Any, path: str) -> Any:
    current = observed
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return _ASSERTION_MISSING
            current = current[segment]
        elif isinstance(current, (list, tuple)) and segment.isdigit():
            if len(current) > _MAX_OBSERVED_COLLECTION:
                return _ASSERTION_MISSING
            index = int(segment)
            if index >= len(current):
                return _ASSERTION_MISSING
            current = current[index]
        else:
            return _ASSERTION_MISSING
    return current


def _bounded_container(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) <= _MAX_OBSERVED_STRING
    if isinstance(value, (Mapping, list, tuple)):
        return len(value) <= _MAX_OBSERVED_COLLECTION
    return False


def _assertion_contains(observed: Any, expected: Any) -> bool:
    if not _bounded_container(observed):
        return False
    if isinstance(observed, str):
        return isinstance(expected, str) and expected in observed
    if isinstance(observed, Mapping):
        return isinstance(expected, str) and expected in observed
    return any(_strict_assertion_equal(item, expected) for item in observed)


def _normalized_assertion_fact(value: Any) -> str:
    """Return the unambiguous, human-readable form used to bind facts to predicates."""

    if value is None:
        raw = "null"
    elif isinstance(value, bool):
        raw = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        raw = str(value)
    else:
        return ""
    normalized = unicodedata.normalize("NFKC", raw).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _assertion_expected_facts(value: Any) -> tuple[str, ...]:
    """Flatten bounded expected values into facts that must be named by the predicate."""

    if isinstance(value, Mapping):
        facts = tuple(
            fact for item in value.values() for fact in _assertion_expected_facts(item)
        )
    elif isinstance(value, (list, tuple)):
        facts = tuple(fact for item in value for fact in _assertion_expected_facts(item))
    else:
        fact = _normalized_assertion_fact(value)
        facts = (fact,) if fact else ()
    return tuple(dict.fromkeys(facts))


@dataclass(frozen=True)
class EvidenceAssertion:
    """A bounded, deterministic mapping from verifier observations to a predicate."""

    predicate: str
    path: str
    operator: str
    expected: Any = field(default=_ASSERTION_UNSET, repr=False)

    def __post_init__(self) -> None:
        if not all(isinstance(item, str) for item in (self.predicate, self.path, self.operator)):
            raise ValueError("evidence assertion predicate, path, and operator must be strings")
        predicate = self.predicate.strip()
        path = self.path.strip()
        operator = self.operator.strip()
        if not predicate or len(predicate) > 512:
            raise ValueError("evidence assertion predicate must be a bounded non-empty string")
        if not path or len(path) > 512:
            raise ValueError("evidence assertion path must be a bounded non-empty dot path")
        segments = path.split(".")
        if (len(segments) > _MAX_ASSERTION_PATH_SEGMENTS or
                any(not _ASSERTION_PATH_SEGMENT.fullmatch(item) for item in segments) or
                any(item.startswith("__") for item in segments) or
                any(item.isdigit() and int(item) > _MAX_OBSERVED_COLLECTION for item in segments)):
            raise ValueError("evidence assertion path contains an invalid segment")
        if operator not in _ASSERTION_OPERATORS:
            raise ValueError("unsupported evidence assertion operator")
        has_expected = self.expected is not _ASSERTION_UNSET
        if (operator in _ASSERTION_VALUE_OPERATORS) != has_expected:
            raise ValueError(f"evidence assertion operator {operator} has an invalid expected value")
        expected = self.expected
        if has_expected:
            expected = _validated_assertion_value(expected)
            encoded = json.dumps(_plain(expected), sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > _MAX_ASSERTION_EXPECTED_BYTES:
                raise ValueError("evidence assertion expected value is too large")
            if operator in {"contains_all", "contains_any"} and (
                    not isinstance(expected, tuple) or not expected):
                raise ValueError(f"evidence assertion operator {operator} requires a non-empty array")
            if operator == "count_at_least" and (
                    not isinstance(expected, int) or isinstance(expected, bool) or
                    expected < 0 or expected > 10_000):
                raise ValueError("count_at_least requires an integer in [0, 10000]")
        object.__setattr__(self, "predicate", predicate)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "operator", operator)
        object.__setattr__(self, "expected", expected)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceAssertion":
        if not isinstance(data, Mapping):
            raise ValueError("evidence assertions must be mapping objects")
        allowed = {"predicate", "path", "operator", "expected"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown evidence assertion fields: {', '.join(sorted(map(str, unknown)))}")
        missing = {"predicate", "path", "operator"} - set(data)
        if missing:
            raise ValueError(f"missing evidence assertion fields: {', '.join(sorted(missing))}")
        operator = data["operator"]
        if not isinstance(operator, str):
            raise ValueError("evidence assertion operator must be a string")
        if operator in _ASSERTION_VALUE_OPERATORS and "expected" not in data:
            raise ValueError(f"evidence assertion operator {operator} requires expected")
        if operator not in _ASSERTION_VALUE_OPERATORS and "expected" in data:
            raise ValueError(f"evidence assertion operator {operator} does not accept expected")
        return cls(
            predicate=data["predicate"], path=data["path"], operator=operator,
            expected=data.get("expected", _ASSERTION_UNSET),
        )

    def as_dict(self) -> dict[str, Any]:
        result = {"predicate": self.predicate, "path": self.path, "operator": self.operator}
        if self.expected is not _ASSERTION_UNSET:
            result["expected"] = _plain(self.expected)
        return result

    @property
    def spec_digest(self) -> str:
        raw = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def explicitly_represents_expected_facts(self) -> bool:
        """Whether every value that can satisfy this assertion is named in its predicate.

        This is deliberately stricter for ``contains_any``: every alternative must be named,
        because any one of them can make the assertion pass. ``count_at_least`` similarly binds
        the exact threshold. Empty values have no fact to bind and therefore fail closed.
        """

        if self.expected is _ASSERTION_UNSET:
            return False
        if self.operator == "count_at_least" and self.expected < 1:
            # A zero threshold proves every bounded collection, including an empty one.
            return False
        if self.operator in {"contains_all", "contains_any"}:
            # In particular, an empty contains_any alternative would match every string.
            candidate_facts = tuple(_assertion_expected_facts(item) for item in self.expected)
            if any(not facts for facts in candidate_facts):
                return False
        predicate = f" {_normalized_assertion_fact(self.predicate)} "
        facts = _assertion_expected_facts(self.expected)
        return bool(facts) and all(f" {fact} " in predicate for fact in facts)

    def evaluate(self, observed: Mapping[str, Any] | list[Any] | tuple[Any, ...]) -> bool:
        value = _resolve_assertion_path(observed, self.path)
        if value is _ASSERTION_MISSING:
            return False
        if self.operator == "equals":
            return _strict_assertion_equal(value, self.expected)
        if self.operator == "contains":
            return _assertion_contains(value, self.expected)
        if self.operator in {"contains_all", "contains_any"}:
            checks = tuple(_assertion_contains(value, item) for item in self.expected)
            return all(checks) if self.operator == "contains_all" else any(checks)
        if self.operator == "non_empty":
            return _bounded_container(value) and len(value) > 0
        if self.operator == "truthy":
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return math.isfinite(value) and value != 0
            return _bounded_container(value) and len(value) > 0
        if self.operator == "count_at_least":
            return (
                isinstance(value, (Mapping, list, tuple))
                and _bounded_container(value)
                and len(value) >= self.expected
            )
        return False  # pragma: no cover - constructor validation makes this unreachable


def evaluate_evidence_assertions(
    assertions: Iterable[EvidenceAssertion],
    observed: Mapping[str, Any] | list[Any] | tuple[Any, ...],
) -> dict[str, dict[str, Any]]:
    """Evaluate assertions, ANDing all specs mapped to the same predicate."""

    items = tuple(assertions)
    if len(items) > _MAX_ASSERTIONS or any(not isinstance(item, EvidenceAssertion) for item in items):
        raise ValueError("evidence assertions must be a bounded typed collection")
    grouped: dict[str, list[EvidenceAssertion]] = {}
    for item in items:
        grouped.setdefault(item.predicate, []).append(item)
    result: dict[str, dict[str, Any]] = {}
    for predicate, specs in grouped.items():
        ordered = sorted(specs, key=lambda item: item.spec_digest)
        outcomes = tuple(item.evaluate(observed) for item in ordered)
        canonical = [item.as_dict() for item in ordered]
        digest = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        passed = sum(outcomes)
        result[predicate] = {
            "verified": bool(outcomes) and all(outcomes),
            "spec_digest": digest,
            "summary": f"{passed}/{len(outcomes)} evidence assertions passed",
        }
    return result


class TaskState(StrEnum):
    QUEUED = "QUEUED"
    COMPILING = "COMPILING"
    READY = "READY"
    RUNNING = "RUNNING"
    RECONCILING = "RECONCILING"
    WAITING_FOR_HOST = "WAITING_FOR_HOST"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    PAUSED_BY_HUMAN = "PAUSED_BY_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class ControlOwner(StrEnum):
    IDLE = "IDLE"
    AGENT = "AGENT"
    HUMAN = "HUMAN"


class PresentationMode(StrEnum):
    FAST = "fast"
    VISIBLE = "visible"
    HYBRID = "hybrid"


class Interruptibility(StrEnum):
    INTERRUPTIBLE = "interruptible"
    ATOMIC_COMMIT = "atomic_commit"
    CONTINUE_BACKGROUND = "continue_background"
    ASK_ON_HANDOFF = "ask_on_handoff"


TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED})
ACTIVE_STATES = frozenset(set(TaskState) - set(TERMINAL_STATES))

_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.COMPILING, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.COMPILING: frozenset({TaskState.READY, TaskState.WAITING_FOR_HOST, TaskState.WAITING_FOR_USER, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.RECONCILING, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset({
        TaskState.RECONCILING, TaskState.WAITING_FOR_HOST, TaskState.WAITING_FOR_USER,
        TaskState.WAITING_FOR_APPROVAL, TaskState.PAUSED_BY_HUMAN, TaskState.SUCCEEDED,
        TaskState.CANCELED, TaskState.FAILED,
    }),
    TaskState.RECONCILING: frozenset({
        TaskState.RUNNING, TaskState.WAITING_FOR_HOST, TaskState.WAITING_FOR_USER,
        TaskState.WAITING_FOR_APPROVAL, TaskState.PAUSED_BY_HUMAN, TaskState.CANCELED,
        TaskState.FAILED,
    }),
    TaskState.WAITING_FOR_HOST: frozenset({TaskState.RECONCILING, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.WAITING_FOR_USER: frozenset({TaskState.RECONCILING, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.WAITING_FOR_APPROVAL: frozenset({TaskState.RECONCILING, TaskState.PAUSED_BY_HUMAN, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.PAUSED_BY_HUMAN: frozenset({TaskState.RECONCILING, TaskState.CANCELED, TaskState.FAILED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
}


def assert_transition(old: TaskState | str, new: TaskState | str) -> None:
    old_state, new_state = TaskState(old), TaskState(new)
    if new_state not in _TRANSITIONS[old_state]:
        raise InvalidTransition(f"invalid task transition {old_state} -> {new_state}")
    if old_state == TaskState.PAUSED_BY_HUMAN and new_state == TaskState.RUNNING:
        raise InvalidTransition("human-paused tasks must reconcile before running")


def _tuple_strings(values: Iterable[Any] | None) -> tuple[str, ...]:
    items = tuple(values or ())
    if len(items) > 256:
        raise ValueError("contract collections are limited to 256 items")
    clean: list[str] = []
    for item in items:
        text = str(item).strip()
        if len(text) > 4096:
            raise ValueError("contract item exceeds 4096 characters")
        if text and text not in clean:
            clean.append(text)
    return tuple(clean)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_deep_freeze(item) for item in value), key=repr))
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


@dataclass(frozen=True)
class IdleResumePolicy:
    enabled: bool = False
    idle_threshold_seconds: float = 20.0
    expires_at: float | None = None
    allowed_steps_or_capabilities: tuple[str, ...] = ()
    maximum_resume_burst: int = 1
    requires_visible_notice: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_steps_or_capabilities", _tuple_strings(self.allowed_steps_or_capabilities))
        if self.idle_threshold_seconds < 1:
            raise ValueError("idle threshold must be at least one second")
        if not 1 <= self.maximum_resume_burst <= 100:
            raise ValueError("maximum resume burst must be in [1, 100]")


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    created_at: float
    created_by: str
    goal: str
    constraints: tuple[str, ...]
    forbidden_effects: tuple[str, ...]
    success_predicates: tuple[str, ...]
    desired_artifacts: tuple[str, ...]
    workspace_roots: tuple[str, ...]
    allowed_apps: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    risk_budget: str
    approval_policy: str
    presentation_mode: PresentationMode
    idle_resume_policy: IdleResumePolicy
    deadline: float | None
    max_steps: int
    max_host_turns: int
    human_epoch_at_creation: int
    revision: int = 0
    # Autonomy mode: the VM is disposable (human can pause/trash anytime), so in-VM
    # effects (files, processes, browser nav, clicks) run WITHOUT per-action approval.
    # Only real-world commits (external submit/upload, credentials, purchases) still gate.
    autonomy: bool = False

    def __post_init__(self) -> None:
        if not self.task_id or not self.goal.strip() or not self.created_by.strip():
            raise ValueError("task_id, created_by, and goal are required")
        if len(self.goal) > 16_384:
            raise ValueError("task goal exceeds 16384 characters")
        if self.created_by not in {"direct_human", "approved_host"}:
            raise UnauthorizedRevision("untrusted content cannot create an authoritative task")
        if not self.success_predicates:
            raise ValueError("at least one success predicate is mandatory")
        for name in (
            "constraints", "forbidden_effects", "success_predicates", "desired_artifacts",
            "workspace_roots", "allowed_apps", "allowed_domains", "allowed_capabilities",
        ):
            object.__setattr__(self, name, _tuple_strings(getattr(self, name)))
        object.__setattr__(self, "presentation_mode", PresentationMode(self.presentation_mode))
        if self.max_steps < 1 or self.max_steps > 10000:
            raise ValueError("max_steps must be in [1, 10000]")
        if self.max_host_turns < 0 or self.max_host_turns > 1000:
            raise ValueError("max_host_turns must be in [0, 1000]")

    @classmethod
    def compile(cls, request: Mapping[str, Any], *, human_epoch: int, created_by: str = "direct_human") -> "TaskContract":
        goal = str(request.get("goal") or "").strip()
        # Autonomy: explicit request wins; otherwise the capsule-wide default env.
        autonomy = request.get("autonomy")
        if autonomy is None:
            autonomy = os.environ.get("PAIRPUTER_WORKBENCH_AUTONOMY", "").lower() in {"1", "true", "yes", "on"}
        autonomy = bool(autonomy)
        # In autonomy mode, raise the risk ceiling to cover any in-VM effect so the model
        # is never blocked on a reversible/destructive-but-local action. Real-world commits
        # (external_commit / credential / financial) are handled separately in policy.py.
        default_budget = "local_destructive" if autonomy else "local_reversible"
        idle = request.get("idle_resume_policy") or {}
        policy = idle if isinstance(idle, IdleResumePolicy) else IdleResumePolicy(
            enabled=bool(idle.get("enabled", False)),
            idle_threshold_seconds=float(idle.get("idle_threshold_seconds", 20)),
            expires_at=float(idle["expires_at"]) if idle.get("expires_at") is not None else None,
            allowed_steps_or_capabilities=_tuple_strings(idle.get("allowed_steps_or_capabilities")),
            maximum_resume_burst=int(idle.get("maximum_resume_burst", 1)),
            requires_visible_notice=bool(idle.get("requires_visible_notice", True)),
        )
        return cls(
            task_id=str(request.get("task_id") or f"task_{uuid.uuid4().hex}"),
            created_at=float(request.get("created_at") or time.time()),
            created_by=created_by,
            goal=goal,
            constraints=_tuple_strings(request.get("constraints")),
            forbidden_effects=_tuple_strings(request.get("forbidden_effects")),
            success_predicates=_tuple_strings(request.get("success_predicates")),
            desired_artifacts=_tuple_strings(request.get("desired_artifacts")),
            workspace_roots=_tuple_strings(request.get("workspace_roots") or ("/home/app/workspace",)),
            allowed_apps=_tuple_strings(request.get("allowed_apps")),
            allowed_domains=_tuple_strings(request.get("allowed_domains")),
            allowed_capabilities=_tuple_strings(request.get("allowed_capabilities")),
            risk_budget=str(request.get("risk_budget") or default_budget),
            approval_policy=str(request.get("approval_policy") or "exact_action"),
            presentation_mode=PresentationMode(request.get("presentation_mode") or "hybrid"),
            idle_resume_policy=policy,
            deadline=float(request["deadline"]) if request.get("deadline") is not None else None,
            max_steps=int(request.get("max_steps", 100)),
            max_host_turns=int(request.get("max_host_turns", 10)),
            human_epoch_at_creation=int(human_epoch),
            autonomy=autonomy,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskContract":
        idle = data.get("idle_resume_policy") or {}
        return cls(
            task_id=str(data["task_id"]), created_at=float(data["created_at"]),
            created_by=str(data["created_by"]), goal=str(data["goal"]),
            constraints=_tuple_strings(data.get("constraints")),
            forbidden_effects=_tuple_strings(data.get("forbidden_effects")),
            success_predicates=_tuple_strings(data.get("success_predicates")),
            desired_artifacts=_tuple_strings(data.get("desired_artifacts")),
            workspace_roots=_tuple_strings(data.get("workspace_roots")),
            allowed_apps=_tuple_strings(data.get("allowed_apps")),
            allowed_domains=_tuple_strings(data.get("allowed_domains")),
            allowed_capabilities=_tuple_strings(data.get("allowed_capabilities")),
            risk_budget=str(data.get("risk_budget") or "local_reversible"),
            approval_policy=str(data.get("approval_policy") or "exact_action"),
            presentation_mode=PresentationMode(data.get("presentation_mode") or "hybrid"),
            idle_resume_policy=IdleResumePolicy(
                enabled=bool(idle.get("enabled", False)),
                idle_threshold_seconds=float(idle.get("idle_threshold_seconds", 20)),
                expires_at=float(idle["expires_at"]) if idle.get("expires_at") is not None else None,
                allowed_steps_or_capabilities=_tuple_strings(idle.get("allowed_steps_or_capabilities")),
                maximum_resume_burst=int(idle.get("maximum_resume_burst", 1)),
                requires_visible_notice=bool(idle.get("requires_visible_notice", True)),
            ),
            deadline=float(data["deadline"]) if data.get("deadline") is not None else None,
            max_steps=int(data.get("max_steps", 100)), max_host_turns=int(data.get("max_host_turns", 10)),
            human_epoch_at_creation=int(data.get("human_epoch_at_creation", 0)), revision=int(data.get("revision", 0)),
            autonomy=bool(data.get("autonomy", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))

    @property
    def digest(self) -> str:
        raw = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class TaskContractRevision:
    revision_id: str
    task_id: str
    base_revision: int
    created_at: float
    created_by: str
    additions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    replacements: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.created_by not in {"direct_human", "approved_host"}:
            raise UnauthorizedRevision("external content cannot revise a task contract")
        allowed_additions = {"constraints", "forbidden_effects", "success_predicates", "desired_artifacts", "allowed_apps", "allowed_domains", "allowed_capabilities"}
        if set(self.additions) - allowed_additions:
            raise ValueError("unsupported additive contract field")
        allowed_replacements = {"deadline", "max_steps", "max_host_turns", "presentation_mode", "idle_resume_policy", "approval_policy", "risk_budget"}
        if set(self.replacements) - allowed_replacements:
            raise ValueError("revisions cannot replace original requirements or scope")
        object.__setattr__(self, "additions", _deep_freeze({key: _tuple_strings(value) for key, value in self.additions.items()}))
        object.__setattr__(self, "replacements", _deep_freeze(self.replacements))

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id, "task_id": self.task_id,
            "base_revision": self.base_revision, "created_at": self.created_at,
            "created_by": self.created_by, "additions": _plain(self.additions),
            "replacements": _plain(self.replacements), "reason": self.reason,
        }


def apply_revision(contract: TaskContract, revision: TaskContractRevision) -> TaskContract:
    if revision.task_id != contract.task_id or revision.base_revision != contract.revision:
        raise RevisionConflict("revision is not based on the current immutable contract")
    changes: dict[str, Any] = {"revision": contract.revision + 1}
    for field_name, additions in revision.additions.items():
        changes[field_name] = _tuple_strings((*getattr(contract, field_name), *additions))
    changes.update(dict(revision.replacements))
    revised = replace(contract, **changes)
    # Defense in depth: every original requirement and scope restriction survives.
    for field_name in ("constraints", "forbidden_effects", "success_predicates", "workspace_roots"):
        if not set(getattr(contract, field_name)).issubset(getattr(revised, field_name)):
            raise RevisionConflict(f"revision attempted to drop {field_name}")
    return revised


@dataclass(frozen=True)
class Step:
    step_id: str
    skill: str
    arguments: Mapping[str, Any]
    preconditions: tuple[str, ...]
    expected_effects: tuple[str, ...]
    success_predicates: tuple[str, ...]
    evidence_assertions: tuple[EvidenceAssertion, ...] = ()
    risk_class: str = "unknown"
    approval_requirement: str = "policy"
    interruptibility: Interruptibility = Interruptibility.INTERRUPTIBLE
    retry_policy: str = "safe_only"
    fallback_policy: str = "none"
    compensation_or_rollback: str = "none"
    presentation_mode: PresentationMode = PresentationMode.HYBRID

    def __post_init__(self) -> None:
        if not self.step_id or not self.skill or not self.success_predicates:
            raise ValueError("step_id, skill, and success predicates are required")
        object.__setattr__(self, "arguments", _deep_freeze(self.arguments))
        if len(json.dumps(_plain(self.arguments), sort_keys=True, default=str).encode()) > 1024 * 1024:
            raise ValueError("step arguments exceed one MiB")
        for name in ("preconditions", "expected_effects", "success_predicates"):
            object.__setattr__(self, name, _tuple_strings(getattr(self, name)))
        assertions = tuple(self.evidence_assertions)
        if len(assertions) > _MAX_ASSERTIONS or any(not isinstance(item, EvidenceAssertion) for item in assertions):
            raise ValueError("step evidence_assertions must be a bounded typed collection")
        if len({item.spec_digest for item in assertions}) != len(assertions):
            raise ValueError("step evidence assertions must be unique")
        if any(item.predicate not in self.success_predicates for item in assertions):
            raise ValueError("step evidence assertions must map to declared success predicates")
        object.__setattr__(self, "evidence_assertions", assertions)
        object.__setattr__(self, "interruptibility", Interruptibility(self.interruptibility))
        object.__setattr__(self, "presentation_mode", PresentationMode(self.presentation_mode))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], index: int = 0) -> "Step":
        if "arguments" not in data or not isinstance(data["arguments"], Mapping):
            raise ValueError("step arguments must be an explicit mapping object")
        raw_assertions = data.get("evidence_assertions", ())
        if not isinstance(raw_assertions, (list, tuple)):
            raise ValueError("step evidence_assertions must be an array of mapping objects")
        if any(not isinstance(item, Mapping) for item in raw_assertions):
            raise ValueError("step evidence_assertions require explicit mapping objects")
        return cls(
            step_id=str(data.get("step_id") or f"step_{index + 1}"),
            skill=str(data.get("skill") or ""),
            arguments=data["arguments"],
            preconditions=_tuple_strings(data.get("preconditions")),
            expected_effects=_tuple_strings(data.get("expected_effects")),
            success_predicates=_tuple_strings(data.get("success_predicates")),
            evidence_assertions=tuple(EvidenceAssertion.from_dict(item) for item in raw_assertions),
            risk_class=str(data.get("risk_class") or "unknown"),
            approval_requirement=str(data.get("approval_requirement") or "policy"),
            interruptibility=Interruptibility(data.get("interruptibility") or "interruptible"),
            retry_policy=str(data.get("retry_policy") or "safe_only"),
            fallback_policy=str(data.get("fallback_policy") or "none"),
            compensation_or_rollback=str(data.get("compensation_or_rollback") or "none"),
            presentation_mode=PresentationMode(data.get("presentation_mode") or "hybrid"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id, "skill": self.skill,
            "arguments": _plain(self.arguments), "preconditions": list(self.preconditions),
            "expected_effects": list(self.expected_effects),
            "success_predicates": list(self.success_predicates),
            "evidence_assertions": [item.as_dict() for item in self.evidence_assertions],
            "risk_class": self.risk_class,
            "approval_requirement": self.approval_requirement,
            "interruptibility": self.interruptibility.value, "retry_policy": self.retry_policy,
            "fallback_policy": self.fallback_policy,
            "compensation_or_rollback": self.compensation_or_rollback,
            "presentation_mode": self.presentation_mode.value,
        }


@dataclass(frozen=True)
class ActionEnvelope:
    task_id: str
    step_id: str
    action_id: str
    expected_world_revision: int
    expected_human_epoch: int
    idempotency_key: str
    effect_class: str
    risk_class: str
    interruptibility: Interruptibility
    presentation_mode: PresentationMode
    deadline: float | None
    action: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not all((self.task_id, self.step_id, self.action_id, self.idempotency_key)):
            raise ValueError("action envelope identifiers are required")
        object.__setattr__(self, "action", _deep_freeze(self.action))
        object.__setattr__(self, "interruptibility", Interruptibility(self.interruptibility))
        object.__setattr__(self, "presentation_mode", PresentationMode(self.presentation_mode))

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "step_id": self.step_id, "action_id": self.action_id,
            "expected_world_revision": self.expected_world_revision,
            "expected_human_epoch": self.expected_human_epoch,
            "idempotency_key": self.idempotency_key, "effect_class": self.effect_class,
            "risk_class": self.risk_class, "interruptibility": self.interruptibility.value,
            "presentation_mode": self.presentation_mode.value, "deadline": self.deadline,
            "action": _plain(self.action),
        }

    @property
    def digest(self) -> str:
        raw = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


class InvalidTransition(RuntimeError):
    pass


class RevisionConflict(RuntimeError):
    pass


class UnauthorizedRevision(PermissionError):
    pass
