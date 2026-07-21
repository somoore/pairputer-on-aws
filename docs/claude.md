# Connect Claude to pairputer

Add pairputer as a custom connector in Claude, then open the Pairputer Workbench from any chat. Once
you connect Claude on the web, the connector also works in the Claude desktop and mobile apps.

Claude is simpler to connect than ChatGPT: its redirect URLs are pre-registered at deploy time, so
there is no callback-registration step and no OAuth scope to add.

## Before you begin

You need:

- A deployed pairputer stack (the 1-click launch or `substrate/deploy.sh`).
- A claude.ai account on the web.
- Your super-admin credentials from the invite email the deploy sent you.
- Two values from your CloudFormation stack's **Outputs** tab: `McpEndpoint` and `ClaudeClientId`.

## 1. Collect your stack values

Open your CloudFormation stack's **Outputs** tab and copy the `McpEndpoint` and `ClaudeClientId`
values.

If you have the AWS CLI, this command prints both values and verifies the auth discovery chain (a 401
`WWW-Authenticate resource_metadata` response, protected-resource metadata, then Cognito OIDC
discovery):

```bash
substrate/wire-claude.sh
```

Claude needs no callback registration - its callbacks are pre-baked into the stack.

## 2. Add the custom connector

claude.ai → **Settings → Customize → Connectors** → **Add** (top-right) → **Add custom connector**.
(The old *Settings → Connectors* path now redirects here - "Connectors have moved to Customize".)

| Field | Value |
|---|---|
| Name | `pairputer` |
| Remote MCP server URL | the `McpEndpoint` stack output (the full `https://bedrock-agentcore..../invocations?qualifier=DEFAULT` URL) |
| Advanced settings → OAuth Client ID | the `ClaudeClientId` stack output |
| OAuth Client Secret | leave BLANK - it's a public PKCE client |

Click **Add**.

> Verified 2026-07-17 using a fresh 1-click deploy: the modal wants exactly Name + URL + Client ID (no
> secret); Connect launches the Cognito login at
> `pairputer-<accountid>.auth.<region>.amazoncognito.com` with `redirect_uri=…/api/mcp/auth_callback`
> already accepted.

## 3. Connect

The connector appears with a **Connect** button → **Connect** → sign in on the Cognito hosted UI
with your super-admin email + the temp password from the invite email (first login forces a
permanent password) → it auto-redirects back. Done.

Reconnect UX (shown by the widget on auth expiry): Settings → Customize → Connectors → pairputer →
**Reconnect**. (After a stack redeploy, Reconnect is NOT enough - see the troubleshooting table.)

## 4. Open the Workbench

Open a **new** chat → type:

> Use the pairputer app to open the Pairputer Workbench (play_capsule) so we can share a live desktop.

The widget reveals inline and streams directly (canvas, no iframe). Click to focus, drive with
keyboard/mouse; ❄ Freeze suspends the VM (billing paused), 🔥 Thaw resumes the exact same session.

**Desktop + mobile:** nothing extra - once connected on web, the connector is available in the
Claude desktop and mobile apps automatically.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Cognito error page shows `invalid_request` with a client_id that isn't in your pool | The connector pins the OAuth client of a DELETED/replaced stack; "Reconnect" cannot fix it | Fully remove the connector and re-add it with the new stack's `McpEndpoint` (see [Redeployed the stack?](#redeployed-the-stack-reconnect-checklist) below) |
| OAuth popup bounces with `invalid_scope` | Claude requests **every** scope Cognito's discovery advertises; a client that disallows any of them bounces | The `ClaudeClient` in `identity.yaml` allows the full standard OIDC set + `pairputer-mcp/invoke`. If you narrowed it, restore the full set |
| Widget renders stale UI after a widget/server redeploy | Claude caches the widget per conversation render | Start a **NEW** chat; old conversations may keep the stale widget (expected) |
| Widget stuck invisible on open | The reveal handshake failed (a `tools/call` raced `ui/notifications/initialized`, or `appInfo` was mis-keyed) | This is a widget-code bug class, not a user setup issue - see Capabilities below and the E2E checklist |

---

---

## Redeployed the stack? Reconnect checklist

A fresh stack means a **new Cognito user pool, new client ids, and a new `McpEndpoint` URL**. Every
chat host's connector still pins the OLD registration, and the failure modes are confusingly different:

- `invalid_request` on the Cognito hosted UI, with an old `client_id` in the URL: the connector is
  presenting the deleted stack's OAuth client. **"Reconnect" does NOT fix this** - it only refreshes
  tokens for the same (dead) registration. **Fully remove the connector and re-add it** with the new
  stack's `McpEndpoint` output.
- `redirect_mismatch` with the NEW client id (ChatGPT only): the recreated ChatGPT connector's
  **per-connector callback URL** (`https://chatgpt.com/connector/oauth/<id>`, shown in the connector's
  settings) is not registered on the fresh pool. Register it:
  `substrate/wire-chatgpt.sh --register-callback '<that url>'`.
- Claude needs no callback step (its redirect URLs are static and pre-baked in `identity.yaml`);
  a full remove and re-add with the new `McpEndpoint` is sufficient.
- Only the **ChatGPT (web)** and **Claude (web)** connectors need setup: each covers that product's
  web, desktop, and mobile apps, and **Codex rides the ChatGPT connector**.

---

# Reference

The rest of this page is background for maintainers: verified capabilities, host quirks, and a
regression checklist. You do not need any of it to connect Claude.

**Status:** web and desktop verified end-to-end (2026-07-09; re-verified on fresh 1-click stacks
2026-07-17 and 2026-07-20). Claude was the first MCP-Apps-standard host (SEP-1865) - no
`window.openai`, no iframe player; adding it surfaced every hidden OpenAI-specific assumption in
the widget.

## Capabilities and constraints (empirically proven)

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
- **Display modes:** inline + fullscreen (granted live using `hostContext.availableDisplayModes`);
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
  bedrock-agentcore endpoint the other hosts use.

## E2E checklist (regression gate for any MCP-layer or widget-bridge change)

1. Fresh chat → `play_capsule` → widget **reveals** (not stuck hidden) and renders from the
   delivered tool-result.
2. Video + audio stream through direct-connect (canvas, not an iframe); keyboard/mouse reach the
   capsule through `POST /input`.
3. Fullscreen granted and reversible; no PiP offered.
4. Freeze → suspended overlay; reopen conversation → VM stays suspended. Thaw → stream resumes.
5. Session >15 min → in-widget token refresh using post-handshake `callTool` (no reconnect overlay).
6. After any widget redeploy: old chat may show the stale widget (expected); new chat shows the
   new one.
