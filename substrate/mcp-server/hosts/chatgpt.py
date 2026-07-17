"""ChatGPT (web + desktop) host profile.

ChatGPT renders MCP Apps widgets via the OpenAI Apps SDK (window.openai bridge, same dialect as
Codex) but with a materially friendlier sandbox: direct fetch/SSE/WebSocket to widgetCSP
connect_domains, cross-origin iframes via frameDomains, inline/fullscreen/PiP display modes, ~60s
tool budget. See docs/hosts/chatgpt.md for the connector setup + probe results.

resource_uri/mime: until PROBE-1 (does ChatGPT render text/html;profile=mcp-app?) says otherwise,
ChatGPT uses the same resource as Codex — one widget, one binding. If PROBE-1 fails, point this at
ui://pairputer-platform/app-std.html (registered with plain text/html) and flip the tool meta per the
plan's PROBE-2 outcome.
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
