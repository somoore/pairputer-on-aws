"""Claude (web + desktop) host profile.

Claude renders the MCP Apps standard (SEP-1865): the spec requires the resource mime to be EXACTLY
text/html;profile=mcp-app and the tool binding to be the NESTED _meta.ui.resourceUri — so Claude
binds to the SAME resource as Codex/ChatGPT (one widget, one URI, three hosts). The widget's raw
ui/* postMessage branch is the bridge Claude exercises (no window.openai).

Empirical status (2026-07-09): OAuth ✅ (after the same allow-all-advertised-scopes fix as ChatGPT),
tools ✅, play_capsule executes ✅ (VM RUNNING). Widget render = the nested-meta fix below; outcomes
tracked in docs/hosts/claude.md.
"""
from . import HostProfile

PROFILE = HostProfile(
    id="claude",
    reconnect_command="Claude: Settings > Connectors > pairputer > Reconnect",
    reconnect_hint=(
        "Claude refreshes the pairputer connector's OAuth session automatically. If the session "
        "expires or is revoked, reconnect it: Settings > Connectors > pairputer > Reconnect."
    ),
    resource_uri="ui://pairputer-platform/app.html",
    resource_mime="text/html;profile=mcp-app",
    # Empty here on purpose: Claude declares its granted modes at runtime via the MCP Apps
    # hostContext.availableDisplayModes (inline + fullscreen, NOT pip). The widget reads that from
    # ui/initialize and shows the Fullscreen button — so this static list stays empty and the
    # capability is discovered live. (openai hosts, which don't send hostContext, use this list.)
    display_modes=(),
    # Claude's sandbox allows the relay in connect-src but NOT frame-src (it doesn't honor
    # ui.csp.frameDomains), so the nested player iframe is blocked. Stream directly from the widget.
    stream_mode="direct",
    # MCP Apps does not currently give this server a proven native equivalent
    # of OpenAI's requiresApproval enforcement. Consequential actions remain
    # visible but their approval token cannot be minted through Claude.
    native_approval_enforced=False,
)
