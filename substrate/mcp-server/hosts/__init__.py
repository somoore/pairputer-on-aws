"""Per-host (chat client) profiles for the pairputer MCP layer.

One MCP server + one widget serve every host; the ONLY things that vary per host are the strings and
resource bindings in a HostProfile. The host is identified by the Cognito app client_id in the caller's
JWT (each host gets its own app client), mapped via the PAIRPUTER_HOST_CLIENT_MAP env var
('{"<client_id>":"codex", ...}', set by CloudFormation). Unknown/M2M/local callers get the Codex
profile — today's behavior, unchanged.

HARD RULE: this package carries zero capsule knowledge. Profiles are strings/URIs only; nothing here
may import registry/capability machinery or name any capsule. Enforced by tests/test_hosts.py.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class HostProfile:
    id: str                 # "codex" | "chatgpt" | "claude"
    reconnect_command: str  # short copyable action for the widget's session-expired overlay
    reconnect_hint: str     # human sentence explaining when/why to use it
    resource_uri: str       # the ui:// resource this host renders
    resource_mime: str      # its mimeType (informational + test-locked)
    # Display modes the host actually grants beyond inline ("pip", "fullscreen"). The widget renders
    # a mode button ONLY for declared modes — so Codex (inline-only) never shows a dead PiP button.
    display_modes: tuple = ()
    # How the widget streams the capsule. "iframe" (default): embed the relay player page (needs the
    # host to allow frame-src=<relay>; Codex/ChatGPT do). "direct": run the decode/input engine IN
    # the widget, connecting to the relay's CORS-open SSE/POST directly — for hosts that allow the
    # relay in connect-src but NOT frame-src (Claude).
    stream_mode: str = "iframe"
    # True only when this OAuth client is a host whose native tool UI is known
    # to enforce openai/requiresApproval before invoking capsule_approve.
    native_approval_enforced: bool = False


from . import codex, chatgpt, claude  # noqa: E402  (need HostProfile defined first)

DEFAULT = codex.PROFILE
_BY_ID = {p.id: p for p in (codex.PROFILE, chatgpt.PROFILE, claude.PROFILE)}


def _client_map() -> dict:
    # Read per call, not at import: cheap, and tests/CFN set the env at different times.
    try:
        return json.loads(os.environ.get("PAIRPUTER_HOST_CLIENT_MAP", "") or "{}")
    except (ValueError, TypeError):
        return {}


def profile_for_client_id(client_id: str) -> HostProfile:
    """Map a caller's Cognito client_id to its host profile; default codex (M2M, local, unknown)."""
    return _BY_ID.get(_client_map().get(client_id or "", ""), DEFAULT)


def native_approval_enforced_for_client_id(client_id: str) -> bool:
    """Fail closed for unknown, M2M, and hosts without a verified approval UI."""
    host_id = _client_map().get(client_id or "", "")
    profile = _BY_ID.get(host_id)
    return bool(profile and profile.native_approval_enforced)


def all_profiles() -> list[HostProfile]:
    return list(_BY_ID.values())
