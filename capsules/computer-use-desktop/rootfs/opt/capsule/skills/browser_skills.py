"""Visible-browser skill abstractions over a protected loopback adapter."""

from __future__ import annotations

import inspect
from typing import Any, Mapping
from urllib.parse import urlparse

from .base import BaseSkill, RawResult, SkillContext, SkillDefinition, Verification


async def _call(adapter: Any, method: str, **kwargs: Any) -> Mapping[str, Any]:
    if adapter is None or not hasattr(adapter, method):
        raise BrowserUnavailable(f"browser adapter does not provide {method}")
    result = getattr(adapter, method)(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise BrowserUnavailable("browser adapter returned an invalid result")
    return result


def _observable_url_identity(raw: str) -> tuple[str, str, int | None, str] | None:
    """Return the URL fields that the redacted browser state can prove.

    Browser receipts deliberately omit query strings and fragments because
    either can contain credentials or other sensitive values.  Navigation
    verification must therefore bind to the exact scheme, host, effective
    port, and path without requiring the intentionally unavailable fields.
    """

    parsed = urlparse(str(raw))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return (
        parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port,
        parsed.path or "/",
    )


class BrowserNavigateSkill(BaseSkill):
    definition = SkillDefinition(
        "browser.navigate", "browser.navigate", "browser_navigate", "local_reversible",
        "interruptible", "inspect_before_retry", ("visible", "hybrid"),
        ("browser_navigated",), 60, "inspect_navigation_state",
    )

    async def inspect(self, args, snapshot, context):
        parsed = urlparse(str(args.get("url") or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise BrowserUnavailable("browser navigation requires an http(s) URL")
        return {"origin": f"{parsed.scheme}://{parsed.hostname}"}

    async def execute(self, prepared, lease, context):
        context.control.checkpoint(lease)
        result = await _call(
            context.services.get("browser"), "navigate", url=str(prepared.action["url"]),
            task_id=context.task_id, action_id=context.action_id,
            expected_human_epoch=lease.human_epoch,
            expected_world_revision=lease.world_revision,
            allowed_domains=context.services.get("allowed_domains", ()),
        )
        return RawResult(True, result, retry_safety="inspect_before_retry")

    async def verify(self, prepared, raw, snapshot, context):
        state = await _call(
            context.services.get("browser"), "state", task_id=context.task_id,
            allowed_domains=context.services.get("allowed_domains", ()),
        )
        expected = str(prepared.action["url"])
        final_url = str(state.get("url") or "")
        verified = (
            _observable_url_identity(final_url) is not None
            and _observable_url_identity(final_url) == _observable_url_identity(expected)
            and bool(state.get("loaded", True))
        )
        return Verification(
            verified, {"browser_navigated": verified}, state,
            "final browser origin, port, path, and load state observed; query and fragment redacted",
        )


class BrowserQuerySkill(BaseSkill):
    definition = SkillDefinition(
        "browser.query", "browser.query", "observe", "read_only",
        "interruptible", "retryable", ("fast", "visible", "hybrid"),
        ("browser_query_observed",), 30, "safe",
    )

    async def execute(self, prepared, lease, context):
        context.control.checkpoint(lease)
        result = await _call(
            context.services.get("browser"), "query",
            selector=prepared.action.get("selector"), task_id=context.task_id,
            allowed_domains=context.services.get("allowed_domains", ()),
        )
        return RawResult(False, result)

    async def verify(self, prepared, raw, snapshot, context):
        matches = raw.result.get("matches")
        verified = isinstance(matches, (list, tuple)) and bool(matches)
        return Verification(verified, {"browser_query_observed": verified}, raw.result, "bounded DOM or AX query observed")


class BrowserInteractSkill(BaseSkill):
    definition = SkillDefinition(
        "browser.interact", "browser.interact", "browser_interact", "unknown",
        "interruptible", "inspect_before_retry", ("visible", "hybrid"),
        ("browser_interaction_verified",), 60, "never_blindly_retry_commit",
    )
    _EFFECTS = {
        "focus": "browser_interact", "click": "external_submit", "fill": "external_submit",
        "submit": "external_submit", "send": "external_submit", "publish": "external_submit",
        "upload": "upload", "download": "download", "enter_credential": "credential_entry",
        "change_permission": "permission_change", "purchase": "purchase",
    }

    def canonical_action(self, args):
        operation = str(args.get("operation") or "")
        effect = self._EFFECTS.get(operation, "unknown")
        clean = {key: value for key, value in args.items() if key not in {"effect", "kind", "risk", "risk_class", "capability", "requires_approval"}}
        return {"kind": effect, "capability": self.definition.capability, **clean}

    async def inspect(self, args, snapshot, context):
        operation = str(args.get("operation") or "")
        if operation not in self._EFFECTS:
            raise BrowserUnavailable("unsupported browser operation")
        if not args.get("selector"):
            raise BrowserUnavailable("browser interaction requires a semantic selector")
        if operation != "focus":
            parsed = urlparse(str(args.get("url") or ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise BrowserUnavailable("browser actions require their current http(s) URL")
        return {"selector_present": True, "operation": args["operation"]}

    async def execute(self, prepared, lease, context):
        context.control.checkpoint(lease)
        result = await _call(
            context.services.get("browser"), "interact",
            operation=str(prepared.action["operation"]), selector=prepared.action["selector"],
            value=prepared.action.get("value"), task_id=context.task_id,
            action_id=context.action_id, expected_human_epoch=lease.human_epoch,
            expected_world_revision=lease.world_revision,
            allowed_domains=context.services.get("allowed_domains", ()),
        )
        return RawResult(True, result, retry_safety="unknown_outcome" if prepared.action["operation"] in {"click", "fill", "submit", "send", "publish", "upload", "purchase"} else "inspect_before_retry")

    async def verify(self, prepared, raw, snapshot, context):
        result = await _call(
            context.services.get("browser"), "verify",
            operation=str(prepared.action["operation"]), selector=prepared.action["selector"],
            expected=prepared.action.get("expected"), task_id=context.task_id,
            allowed_domains=context.services.get("allowed_domains", ()),
        )
        verified = bool(result.get("verified", False))
        return Verification(verified, {"browser_interaction_verified": verified}, result, "browser postcondition observed")


class BrowserUnavailable(RuntimeError):
    pass
