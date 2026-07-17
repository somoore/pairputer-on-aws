"""OpenAI Codex host profile (the original host — its values are the platform defaults).

Codex specifics that shaped the platform (see docs/hosts/codex.md):
- widget bridge: window.openai; widgets cannot make ANY cross-origin request (frame escape hatch);
- inline display mode only; ~25s hard tool-call ceiling; widget-initiated callTool unreliable;
- caches the tool->resource binding across logout/login: the resource URI below must NEVER change
  (server.py documents the wall) — ship new widget HTML by changing the SERVER NAME.
"""
from . import HostProfile

RECONNECT_COMMAND = "codex mcp login pairputer"

PROFILE = HostProfile(
    id="codex",
    reconnect_command=RECONNECT_COMMAND,
    reconnect_hint=(
        "Codex stores the Cognito OAuth refresh token in the OS keyring and should refresh it "
        f"automatically. If the pairputer connection expires or is revoked, run: {RECONNECT_COMMAND}"
    ),
    resource_uri="ui://pairputer-platform/app.html",
    resource_mime="text/html;profile=mcp-app",
    native_approval_enforced=True,
)
