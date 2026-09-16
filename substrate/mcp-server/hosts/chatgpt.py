"""ChatGPT (web + desktop) host profile.

ChatGPT renders MCP Apps widgets via the OpenAI Apps SDK (window.openai bridge, same dialect as
Codex) but with a materially friendlier sandbox: direct fetch/SSE/WebSocket to widgetCSP
connect_domains, cross-origin iframes via frameDomains, inline/fullscreen/PiP display modes, ~60s
tool budget. See docs/chatgpt.md and docs/chatgpt-csp-retest.md for setup and probe status.

PROBE-1 verified text/html;profile=mcp-app on 2026-07-08. Keep the shared resource URI/MIME stable;
existing connector installs cache that binding. CSP-ON and current remount validation remain open.
"""
from . import HostProfile

PROFILE = HostProfile(
    id="chatgpt",
    reconnect_command="ChatGPT: Settings > Apps & Connectors > pairputer > Sign in",
    reconnect_hint=(
        "ChatGPT refreshes the pairputer connector's OAuth session automatically. If the session "
        "expires or is revoked, reconnect it: Settings > Apps & Connectors > pairputer > Sign in."
    ),
    resource_uri="ui://pairputer-platform/app.html",
    resource_mime="text/html;profile=mcp-app",
    # Verified live 2026-07-08 (web). PiP falls back to fullscreen on mobile — the host negotiates;
    # the widget always renders from the GRANTED mode.
    display_modes=("pip", "fullscreen"),
    native_approval_enforced=True,
)
