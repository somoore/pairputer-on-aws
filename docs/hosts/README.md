# Host integrations — capability matrix + probe log

pairputer's MCP layer serves multiple chat hosts from ONE server + ONE widget. Per-host differences
live in `substrate/mcp-server/hosts/` (profiles: reconnect UX, resource URI/mime) and one Cognito app
client per host. This doc is the source of truth for what each host supports and what we've proven
empirically. Per-host guides: [`codex.md`](./codex.md) · [`chatgpt.md`](./chatgpt.md) ·
[`claude.md`](./claude.md).

## Capability matrix (updated as probes land)

| Capability | Codex | ChatGPT (web/desktop) | Claude (web/desktop) |
|---|---|---|---|
| Widget bridge | `window.openai` | `window.openai` | ✅ MCP Apps `ui/*` postMessage (standard, proven) |
| Resource mime accepted | `text/html;profile=mcp-app` (proven) | ✅ same resource | ✅ `text/html;profile=mcp-app` (proven — one resource, all 3 hosts) |
| Widget cross-origin fetch/SSE/WS | ❌ blocked (wall #10) → iframe player | ✅ connect_domains (kept iframe player) | ✅ connect-src (used by direct-connect player) |
| Streaming path | iframe player | iframe player | ✅ direct-connect (no frame-src) |
| Display modes | inline only | inline / fullscreen / **PiP** (PiP→fullscreen on mobile) | inline / **fullscreen** (no PiP — Claude doesn't advertise it) |
| Widget-initiated callTool | ❌ unreliable (wall #17) | ✅ works | ✅ works (post-handshake) |
| Tool-call ceiling | ~25s hard (wall: codex-mcp-tool-timeout) | ~60s | generous (streamable-http) |
| OAuth client | public PKCE + localhost hash callback | public PKCE + fixed https callback | public PKCE + fixed https callback |
| DCR needed | no (static client_id in config.toml) | no (static/predefined client supported) | no (expected; verify) |

## Probe log

### PROBE-4 — AgentCore RFC 9728 protected-resource metadata: ✅ YES (2026-07-08)
Unauthenticated POST to the live MCP endpoint returns:

```
HTTP/2 401
www-authenticate: Bearer resource_metadata="https://bedrock-agentcore.us-east-1.amazonaws.com/
  runtimes/<escaped-arn>/invocations/.well-known/oauth-protected-resource?qualifier=DEFAULT"
```

and that URL returns 200 with a valid RFC 9728 document:

```json
{"authorization_servers":["https://cognito-idp.us-east-1.amazonaws.com/us-east-1_3G4qFi1nZ"],
 "resource":"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/<escaped-arn>/invocations?qualifier=DEFAULT"}
```

**Decision: NO CloudFront MCP front door needed.** AgentCore natively speaks the MCP authorization
discovery flow. ChatGPT/Claude connect to the same bedrock-agentcore endpoint Codex uses.
(`nested/mcp-front.yaml` from the original plan is dropped unless PROBE-6 fails.)

### PROBE-6 (partial) — Cognito AS metadata discovery: ✅ likely OK (2026-07-08)
- `<issuer>/.well-known/oauth-authorization-server` → **400** (Cognito doesn't serve RFC 8414).
- `<issuer>/.well-known/openid-configuration` → **200**, full document (authorize/token on the hosted
  UI domain, PKCE-capable authorization_endpoint).

The MCP auth spec requires clients to fall back to OIDC discovery when RFC 8414 isn't served; ChatGPT
documents spec conformance. Final confirmation = the live dev-mode connector OAuth round-trip (Phase 2
gate). If a host hard-requires RFC 8414, revive the front-door design (see plan history).

### PROBE-1 — ChatGPT renders `text/html;profile=mcp-app`? ✅ YES (2026-07-08, live dev-mode connector)
The UNCHANGED Codex resource (same URI, same mime, `openai/outputTemplate` binding) rendered in
ChatGPT web on the first try — full widget chrome (Freeze/Thaw/Trash/Fullscreen/Sound). One
resource serves both OpenAI hosts; the `app-std.html` standard variant stays for Claude.
PROBE-2 is therefore moot for ChatGPT.

### PROBE-7 — ChatGPT delivers widget-initiated callTool? ✅ YES (2026-07-08)
The widget's boot reconciliation (`pairputer_session ensure_running=false`) and its
Launch/Thaw buttons all round-trip through `window.openai.callTool` — CallToolRequests visible in
AgentCore logs. **The wall-#17 Codex limitation does NOT apply to ChatGPT**: token refresh and
lifecycle buttons work as designed.

### Auth wall discovered — ChatGPT connect requests EVERY scope Cognito advertises
The connect flow requested `openid email phone profile` (Cognito discovery `scopes_supported`)
regardless of the connector's configured scope checkboxes → Cognito instantly bounced
`invalid_scope` (the "popup opens and closes with a red OpenAI error" symptom). Fix: the ChatGPT
app client allows the standard OIDC set + `pairputer-mcp/invoke` (identity.yaml). Diagnose by
replaying `/oauth2/authorize` with curl and bisecting scopes; allow ~1 min Cognito propagation.

### Widget wall discovered — never hardcode a default capsule id in app.html
ChatGPT rendered the widget from a state read (no play payload), so the widget's old hardcoded
`imageId='doom'` poisoned explicit calls (`thaw` → "unknown image_id: doom") on a stack whose sole
capsule is `agent-doom`. Fixed: empty default; the server resolves `""` to the sole capsule.
Locked by tests/test_hosts.py.

### Dev-mode CSP toggle exists (Settings → Apps → Advanced): "Enforce CSP in developer mode"
OFF (default): dev widgets get unrestricted network. Before trusting any CSP-dependent behavior
(connect_domains, frameDomains), re-run the checks with this ON — that is what production enforces.
Current testing ran with CSP OFF (badge shows on the widget).

### Live milestone (2026-07-08, ChatGPT web, dev-mode, CSP off)
DOOM streamed at 30 FPS in the ChatGPT widget through the UNCHANGED relay player iframe
(frameDomains honored); click fired a shot (ammo 56→55), arrow keys turned, human input revoked the
in-VM autopilot (arbiter working); Freeze → SUSPENDED·billing-paused → Thaw → same session resumed.
The whole Codex data plane ported with ZERO relay/player changes.

### PROBE-3 — nested-iframe autoplay/audio permissions in ChatGPT sandbox — PENDING (retest with CSP ON)
### PROBE-8 — PiP transitions: ✅ stream SURVIVES (2026-07-08, web)
Pop out → PiP (floating window, chat scrolls beneath) → back inline: the player iframe and its
SSE streams stayed live through both transitions (30 FPS continuous, no remount observed on web).
The widgetState boot fallback remains in place for hosts/platforms that DO remount (Android bug).

### PROBE-9 FINAL — Claude streams DOOM live: ✅ (2026-07-09, human-confirmed)
Full parity: widget renders + reveals, and video/audio/keyboard/mouse work via the direct-connect
in-widget player (Claude blocks frame-src, so no iframe). Root-caused entirely in a local host
harness. See docs/hosts/claude.md for the full fix chain (nested meta, appInfo handshake,
tools/call gating, direct-connect streaming + SSE self-heal + iframe→direct fallback).

### Widget resource cache-bust on ChatGPT (wall)
Reloading the conversation re-mounts the widget but may serve CACHED resource HTML. The reliable
bust: Settings → Apps → pairputer → **Refresh** (bumps the app version, "Actions refreshed"), THEN
reload the conversation.
### PROBE-9 — Claude renders the standard resource: ✅ WIDGET REVEALS (2026-07-09)
Root-caused via a local host harness (scratchpad/host.html): the reveal was blocked by the widget's
boot tools/call racing the ui/initialize handshake ("AppBridge received tools/call before
ui/notifications/initialized" → host refuses to reveal). Fixed (gate opens only after 'initialized';
standard bridge renders from the tool-result payload, no boot tools/call). Widget iframe confirmed
visibility:visible 736x531 with full chrome. Remaining: the relay PLAYER iframe (video/audio) is
blocked — Claude's sandbox allows the relay in connect-src but NOT frame-src (doesn't honor
ui.csp.frameDomains). Live stream needs the direct-connect path (relay already in connect-src).
Details: docs/hosts/claude.md.

## How to run the interactive probes

```bash
substrate/local-dev.sh                                   # capsule + local MCP on :8000
cloudflared tunnel --url http://localhost:8000            # public URL for the host
# ChatGPT → Settings → Apps & Connectors → Advanced → Developer mode → New connector
#   → <tunnel-url>/mcp (no auth in local mode)
# then: ask ChatGPT to run play_capsule; observe render/stream/PiP/callTool behavior.
```

Record every answer here with date + evidence.
