#!/usr/bin/env python3
"""Fail-closed effect policy, provenance boundary, and exact approvals."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from evidence import canonical_json, redact
from task_contract import ActionEnvelope, TaskContract
from task_journal import ApprovalConflict, TaskJournal


class RiskClass(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_REVERSIBLE = "local_reversible"
    LOCAL_DESTRUCTIVE = "local_destructive"
    EXTERNAL_COMMIT = "external_commit"
    CREDENTIAL_OR_ACCESS = "credential_or_access"
    FINANCIAL_OR_LEGAL = "financial_or_legal"
    UNKNOWN = "unknown"


class EffectClass(StrEnum):
    OBSERVE = "observe"
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    WORKSPACE_TRASH = "workspace_trash"
    PERMANENT_DELETE = "permanent_delete"
    PROCESS_START = "process_start"
    PROCESS_SHELL = "process_shell"
    PROCESS_CANCEL = "process_cancel"
    APP_FOCUS = "app_focus"
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_INTERACT = "browser_interact"
    EXTERNAL_SUBMIT = "external_submit"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DOWNLOAD_EXECUTE = "download_execute"
    CREDENTIAL_ENTRY = "credential_entry"
    PERMISSION_CHANGE = "permission_change"
    PURCHASE = "purchase"
    UNKNOWN = "unknown"


class ProvenanceClass(StrEnum):
    DIRECT_HUMAN = "direct_human"
    APPROVED_HOST = "approved_host"
    WEBPAGE = "webpage"
    DOCUMENT = "document"
    EMAIL = "email"
    CHAT_MESSAGE = "chat_message"
    TERMINAL_OUTPUT = "terminal_output"
    CODE_COMMENT = "code_comment"
    FILENAME = "filename"
    SCREENSHOT = "screenshot"
    TOOL_OUTPUT = "tool_output"
    DOWNLOAD = "download"


TRUSTED_AUTHORITY = frozenset({ProvenanceClass.DIRECT_HUMAN, ProvenanceClass.APPROVED_HOST})


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    risk_class: RiskClass
    effect_class: EffectClass
    requires_approval: bool
    requires_human_takeover: bool
    reason: str


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    task_id: str
    step_id: str
    action_id: str
    action_digest: str
    human_epoch: int
    world_revision: int
    expires_at: float
    nonce: str
    preview: Mapping[str, Any]


class PolicyEngine:
    """Deterministic policy.  Model-provided risk labels are never authoritative."""

    _CLASSIFICATION: Mapping[EffectClass, RiskClass] = {
        EffectClass.OBSERVE: RiskClass.READ_ONLY,
        EffectClass.WORKSPACE_READ: RiskClass.READ_ONLY,
        EffectClass.WORKSPACE_WRITE: RiskClass.LOCAL_REVERSIBLE,
        EffectClass.WORKSPACE_TRASH: RiskClass.LOCAL_REVERSIBLE,
        EffectClass.PERMANENT_DELETE: RiskClass.LOCAL_DESTRUCTIVE,
        # An arbitrary executable/interpreter is not confined by its cwd.
        EffectClass.PROCESS_START: RiskClass.LOCAL_DESTRUCTIVE,
        EffectClass.PROCESS_SHELL: RiskClass.LOCAL_DESTRUCTIVE,
        EffectClass.PROCESS_CANCEL: RiskClass.LOCAL_DESTRUCTIVE,
        EffectClass.APP_FOCUS: RiskClass.LOCAL_REVERSIBLE,
        EffectClass.BROWSER_NAVIGATE: RiskClass.LOCAL_REVERSIBLE,
        EffectClass.BROWSER_INTERACT: RiskClass.LOCAL_REVERSIBLE,
        EffectClass.EXTERNAL_SUBMIT: RiskClass.EXTERNAL_COMMIT,
        EffectClass.UPLOAD: RiskClass.EXTERNAL_COMMIT,
        EffectClass.DOWNLOAD: RiskClass.LOCAL_REVERSIBLE,
        EffectClass.DOWNLOAD_EXECUTE: RiskClass.CREDENTIAL_OR_ACCESS,
        EffectClass.CREDENTIAL_ENTRY: RiskClass.CREDENTIAL_OR_ACCESS,
        EffectClass.PERMISSION_CHANGE: RiskClass.CREDENTIAL_OR_ACCESS,
        EffectClass.PURCHASE: RiskClass.FINANCIAL_OR_LEGAL,
        EffectClass.UNKNOWN: RiskClass.UNKNOWN,
    }

    def classify(self, action: Mapping[str, Any]) -> tuple[EffectClass, RiskClass]:
        raw = str(action.get("effect") or action.get("kind") or "unknown").lower()
        aliases = {
            "read": EffectClass.WORKSPACE_READ, "list": EffectClass.OBSERVE,
            "inspect": EffectClass.OBSERVE, "create": EffectClass.WORKSPACE_WRITE,
            "patch": EffectClass.WORKSPACE_WRITE, "write": EffectClass.WORKSPACE_WRITE,
            "move": EffectClass.WORKSPACE_WRITE, "trash": EffectClass.WORKSPACE_TRASH,
            "delete": EffectClass.PERMANENT_DELETE, "run_command": EffectClass.PROCESS_START,
            "cancel_job": EffectClass.PROCESS_CANCEL, "open_app": EffectClass.APP_FOCUS,
            "focus_app": EffectClass.APP_FOCUS, "browser_open": EffectClass.BROWSER_NAVIGATE,
            "browser_query": EffectClass.OBSERVE, "browser_interact": EffectClass.BROWSER_INTERACT,
            "submit": EffectClass.EXTERNAL_SUBMIT, "send": EffectClass.EXTERNAL_SUBMIT,
            "publish": EffectClass.EXTERNAL_SUBMIT, "upload": EffectClass.UPLOAD,
            "download": EffectClass.DOWNLOAD, "execute_download": EffectClass.DOWNLOAD_EXECUTE,
            "enter_credential": EffectClass.CREDENTIAL_ENTRY, "change_permission": EffectClass.PERMISSION_CHANGE,
            "purchase": EffectClass.PURCHASE,
        }
        try:
            effect = EffectClass(raw)
        except ValueError:
            effect = aliases.get(raw, EffectClass.UNKNOWN)
        return effect, self._CLASSIFICATION[effect]

    # Effects that ESCAPE the disposable VM into the real world. Trashing the VM cannot undo
    # these (a sent email stays sent, a charge stays charged), so disposability stops protecting
    # us here — and untrusted content (prompt injection) reaching these is the real danger. These
    # are the ONLY things autonomy still gates.
    _ESCAPES_VM = {RiskClass.EXTERNAL_COMMIT, RiskClass.CREDENTIAL_OR_ACCESS, RiskClass.FINANCIAL_OR_LEGAL}

    def evaluate(self, contract: TaskContract, action: Mapping[str, Any]) -> PolicyDecision:
        effect, risk = self.classify(action)
        capability = str(action.get("capability") or "")
        if effect == EffectClass.UNKNOWN:
            return PolicyDecision(False, risk, effect, False, False, "unclassified effects fail closed")

        # AUTONOMY: reckless in the box, safe at the edge. The VM is disposable and the human can
        # pause/trash it anytime, so EVERY in-VM effect runs with ZERO friction — no capability
        # allow-list, no risk budget, no approval. We honor only the caller's OWN explicit
        # forbidden_effects denylist, and we still gate effects that leave the VM (see _ESCAPES_VM).
        if bool(getattr(contract, "autonomy", False)):
            if effect.value in contract.forbidden_effects or risk.value in contract.forbidden_effects:
                return PolicyDecision(False, risk, effect, False, False, "effect is on the task's forbidden list")
            if risk == RiskClass.FINANCIAL_OR_LEGAL:
                return PolicyDecision(False, risk, effect, False, True, "financial or legal commitment requires human takeover")
            if risk in self._ESCAPES_VM:
                return PolicyDecision(True, risk, effect, True, risk == RiskClass.CREDENTIAL_OR_ACCESS, "external-world commit requires approval even in autonomy mode")
            return PolicyDecision(True, risk, effect, False, False, "autonomy: in-VM effect allowed without friction")

        # STRICT mode (autonomy off): the original conservative gating — allow-list, forbidden
        # effects, workspace/domain scope, risk budget, and per-action approval for sensitive effects.
        if capability and capability not in contract.allowed_capabilities:
            return PolicyDecision(False, risk, effect, False, False, "capability is outside the task contract")
        if effect.value in contract.forbidden_effects or risk.value in contract.forbidden_effects:
            return PolicyDecision(False, risk, effect, False, False, "effect is forbidden by the task contract")
        scope_error = self._scope_error(contract, action)
        if scope_error:
            return PolicyDecision(False, risk, effect, False, False, scope_error)
        if risk == RiskClass.FINANCIAL_OR_LEGAL:
            return PolicyDecision(False, risk, effect, False, True, "financial or legal commitment requires human takeover")
        budget_order = [RiskClass.READ_ONLY, RiskClass.LOCAL_REVERSIBLE, RiskClass.LOCAL_DESTRUCTIVE, RiskClass.EXTERNAL_COMMIT, RiskClass.CREDENTIAL_OR_ACCESS]
        try:
            allowed_index = budget_order.index(RiskClass(contract.risk_budget))
            actual_index = budget_order.index(risk)
        except ValueError:
            return PolicyDecision(False, risk, effect, False, False, "invalid or unsupported task risk budget")
        if actual_index > allowed_index:
            return PolicyDecision(False, risk, effect, False, False, "effect exceeds the task risk budget")
        if risk in {RiskClass.CREDENTIAL_OR_ACCESS, RiskClass.EXTERNAL_COMMIT, RiskClass.LOCAL_DESTRUCTIVE}:
            return PolicyDecision(True, risk, effect, True, risk == RiskClass.CREDENTIAL_OR_ACCESS, "exact action approval required")
        return PolicyDecision(True, risk, effect, False, False, "allowed within scope")

    @staticmethod
    def _scope_error(contract: TaskContract, action: Mapping[str, Any]) -> str | None:
        if action.get("path"):
            try:
                roots = [Path(root).expanduser().resolve(strict=False) for root in contract.workspace_roots]
                raw_target = Path(str(action["path"])).expanduser()
                target = (raw_target if raw_target.is_absolute() else roots[0] / raw_target).resolve(strict=False)
            except (OSError, ValueError):
                return "invalid workspace path"
            if not any(target == root or root in target.parents for root in roots):
                return "workspace path is outside task scope"
        if action.get("url"):
            parsed = urlparse(str(action["url"]))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return "only http(s) browser targets are supported"
            host = parsed.hostname.lower().rstrip(".")
            allowed = tuple(domain.lower().rstrip(".") for domain in contract.allowed_domains)
            if not any(host == domain or host.endswith("." + domain) for domain in allowed):
                return "domain is outside task scope"
        if action.get("app") and str(action["app"]) not in contract.allowed_apps:
            return "application is outside task scope"
        return None


class ProvenanceBoundary:
    """Content may contribute facts, never authority."""

    @staticmethod
    def may_revise_contract(provenance: ProvenanceClass | str) -> bool:
        return ProvenanceClass(provenance) in TRUSTED_AUTHORITY

    @staticmethod
    def assert_contract_authority(provenance: ProvenanceClass | str) -> None:
        if not ProvenanceBoundary.may_revise_contract(provenance):
            raise PromptInjectionRejected("content-originated instructions cannot widen authority")

    @staticmethod
    def content_fact(value: Any, provenance: ProvenanceClass | str) -> dict[str, Any]:
        source = ProvenanceClass(provenance)
        return {"value": redact(value), "provenance": source.value, "authoritative": source in TRUSTED_AUTHORITY}


class ApprovalAuthority:
    """HMAC-signed exact action approvals with durable single-use consumption."""

    def __init__(self, journal: TaskJournal, secret: bytes | None = None):
        self.journal = journal
        self._secret = secret or os.urandom(32)
        if len(self._secret) < 32:
            raise ValueError("approval signing key must be at least 256 bits")

    @staticmethod
    def _binding(envelope: ActionEnvelope, *, expires_at: float, nonce: str, approval_id: str) -> dict[str, Any]:
        return {
            "approval_id": approval_id,
            "task_id": envelope.task_id,
            "step_id": envelope.step_id,
            "action_id": envelope.action_id,
            "action_digest": envelope.digest,
            "human_epoch": envelope.expected_human_epoch,
            "world_revision": envelope.expected_world_revision,
            "expires_at": expires_at,
            "nonce": nonce,
        }

    def request(self, envelope: ActionEnvelope, preview: Mapping[str, Any], *, ttl_seconds: float = 300) -> ApprovalRequest:
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("approval TTL must be in [1, 3600] seconds")
        approval_id = f"approval_{uuid.uuid4().hex}"
        nonce = uuid.uuid4().hex
        binding = self._binding(envelope, expires_at=time.time() + ttl_seconds, nonce=nonce, approval_id=approval_id)
        request = ApprovalRequest(**binding, preview=redact(preview))
        self.journal.store_approval_request({**binding, "preview": request.preview})
        return request

    def approve(self, approval_id: str) -> str:
        row = self.journal.approval(approval_id)
        if not row:
            raise KeyError(approval_id)
        payload = {key: row[key] for key in (
            "approval_id", "task_id", "step_id", "action_id", "action_digest",
            "human_epoch", "world_revision", "expires_at", "nonce",
        )}
        encoded = canonical_json(payload).encode()
        signature = hmac.new(self._secret, encoded, hashlib.sha256).hexdigest()
        body = base64.urlsafe_b64encode(encoded).decode().rstrip("=")
        token = f"{body}.{signature}"
        self.journal.grant_approval(approval_id, hashlib.sha256(token.encode()).hexdigest())
        return token

    def validate_and_consume(self, token: str, envelope: ActionEnvelope) -> None:
        try:
            body, signature = token.split(".", 1)
            padded = body + "=" * (-len(body) % 4)
            encoded = base64.urlsafe_b64decode(padded.encode())
            if not hmac.compare_digest(signature, hmac.new(self._secret, encoded, hashlib.sha256).hexdigest()):
                raise ApprovalConflict("approval signature is invalid")
            payload = json.loads(encoded)
        except (ValueError, json.JSONDecodeError, binascii.Error) as exc:
            raise ApprovalConflict("approval token is malformed") from exc
        expected = self._binding(
            envelope,
            expires_at=float(payload.get("expires_at", 0)),
            nonce=str(payload.get("nonce", "")),
            approval_id=str(payload.get("approval_id", "")),
        )
        if canonical_json(payload) != canonical_json(expected):
            raise ApprovalConflict("approval token does not bind this exact action")
        self.journal.consume_approval(
            expected["approval_id"], hashlib.sha256(token.encode()).hexdigest(), expected,
        )


class PromptInjectionRejected(PermissionError):
    pass
