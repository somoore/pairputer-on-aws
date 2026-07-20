# Host: Claude (web + desktop)

The first MCP-Apps-standard host (SEP-1865) - no `window.openai`, no iframe player. Adding it
exposed every hidden OpenAI-specific dependency in the widget; the full war story is `blog5.md`.
All facts below are empirically proven (live, human-confirmed 2026-07-09: video + audio + keyboard
+ mouse + freeze/thaw).

## Capabilities / constraints (all empirically proven)

- **Bridge:** standard MCP Apps `ui/*` postMessage - NOT `window.openai`. Tool `_meta` must carry
  the **nested** `_meta.ui.resourceUri` (a flat `ui/resourceUri` key is ignored); resource mime
  must be **exactly** `text/html;profile=mcp-app`. One resource serves all three hosts
  (`ui://pairputer-platform/app.html` - never change it; locked by tests/test_hosts.py).
- **Strict reveal handshake.** Claude keeps the widget `visibility:hidden` until the app completes
  `ui/initialize` → `ui/notifications/initialized`, and **refuses to reveal if ANY `tools/call`
  arrives before `initialized`**. Two traps inside that:
  - `ui/initialize` params are `{appInfo, appCapabilities, protocolVersion}` - it's **`appInfo`,
    NOT `clientInfo`**. Wrong key = permanently veiled widget, no error.
  - The widget must render from the delivered `ui/notifications/tool-result` payload instead of
    firing a boot-time session call (the boot `tools/call` racing the handshake was the original
    invisible-widget bug).
- **`frame-src` is blocked; `connect-src` is allowed** → **direct-connect streaming**
  (`stream_mode="direct"` in `hosts/claude.py`). The widget runs the SAME WebCodecs H.264/Opus
  decode + batched `POST /input` engine IN-WIDGET (`makeDirectPlayer` in app.html), hitting the
  relay's CORS-open (`access-control-allow-origin: *`) SSE/POST endpoints directly - no player
  iframe. An iframe that fails to boot in 6s auto-falls-back to direct.
- **Display modes:** inline + fullscreen (granted live via `hostContext.availableDisplayModes`);
  **no PiP**. The profile declares `()` and the widget merges what the host actually grants.
- **Widget-initiated `callTool` works** (post-handshake) - token refresh and the
  Freeze/Thaw/Trash buttons round-trip normally. The wall-#17 Codex limitation does not apply.
- **Caches the widget per conversation render** - after a widget/server redeploy, start a **NEW
  chat**; reopening an old conversation may keep the stale widget.
- **Debugging is blind from the page.** The inner content iframe is cross-origin
  (`*.claudemcpcontent.com`), so its console is unreadable. Use the **local host harness**
  (`scratchpad/host.html` - a minimal AppBridge embedding the real app.html against the live
  relay); it reproduced the reveal + direct-connect bugs in seconds instead of ~3-min redeploys.
  Rebuild it whenever you touch the widget's host bridge.
- **OAuth:** static public PKCE client (`ClaudeClientId` stack output). Callbacks are **fixed and
  registered at deploy time** (`https://claude.ai/api/mcp/auth_callback` + the claude.com twin in
  identity.yaml) - unlike Codex/ChatGPT there is NO post-deploy callback step. Claude requests
  **every scope Cognito's discovery advertises**, so the app client allows the full standard OIDC
  set (`openid email phone profile`) + `pairputer-mcp/invoke` - narrowing it bounces the popup with
  `invalid_scope` (same wall as ChatGPT).
- **Auth discovery:** the same RFC 9728 chain as the other hosts (PROBE-4) - AgentCore's 401
  `resource_metadata` → Cognito OIDC discovery. No front door, no DCR; Claude connects to the same
  bedrock-agentcore endpoint Codex uses.

## Setup

1. `substrate/wire-claude.sh` - verifies the discovery chain and prints the values below (no
   registration step; callbacks are baked in at deploy time).
2. Claude → Settings → Connectors → **Add custom connector**:
   - URL = the `McpEndpoint` stack output
   - OAuth Client ID = the `ClaudeClientId` stack output (no secret)
3. Connect → sign in via the Cognito hosted UI → open a **new** chat → "open pairputer" /
   `play_capsule`.

Reconnect UX (shown by the widget on auth expiry): Settings → Connectors → pairputer → Reconnect.

## E2E checklist (regression gate for any MCP-layer or widget-bridge change)

1. Fresh chat → `play_capsule` → widget **reveals** (not stuck hidden) and renders from the
   delivered tool-result.
2. Video + audio stream via direct-connect (canvas, not an iframe); keyboard/mouse reach the
   capsule through `POST /input`.
3. Fullscreen granted and reversible; no PiP offered.
4. Freeze → suspended overlay; reopen conversation → VM stays suspended. Thaw → stream resumes.
5. Session >15 min → in-widget token refresh via post-handshake `callTool` (no reconnect overlay).
6. After any widget redeploy: old chat may show the stale widget (expected); new chat shows the
   new one.
