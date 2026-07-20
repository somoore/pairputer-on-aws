# Host: ChatGPT (web + desktop)

Status: **web AND desktop WORKING end-to-end (human-confirmed 2026-07-08/09)** - OAuth, tools
(incl. tag-discovered capsule cartridge tools), widget render, 30 FPS video through the relay
player iframe, keyboard/mouse/audio, gameplay, freeze/thaw/launch via widget buttons
(widget-initiated callTool works on ChatGPT), PiP pop-out on web (floating) and desktop (docked
panel with fill layout + stream-stall auto-recovery). Outstanding: CSP-ON retest.

Distribution target: **Developer Mode connectors** (private). App-store submission (dedicated widget
domain `_meta.ui.domain`, OpenAI review - stricter for iframe embeds) is a documented follow-up.

---

## End-to-end setup: zero → driving the Pairputer Workbench in ChatGPT web

Every step below was executed and verified live on 2026-07-08. Prerequisites: a deployed pairputer
stack (multi-host update or later), a ChatGPT account with Developer mode available (Pro / Plus /
Business / Enterprise / Edu), a pairputer Cognito user (the super-admin from the deploy works), and
AWS CLI credentials for the deploying account (one command in step 4 needs them).

### 1. Collect your stack values + verify the auth chain

```bash
substrate/wire-chatgpt.sh
```

This prints the two values you'll paste into ChatGPT - `McpEndpoint` and `ChatGPTClientId` - and
verifies the discovery chain ChatGPT depends on (401 `WWW-Authenticate resource_metadata` →
protected-resource metadata → Cognito OIDC discovery). All three checks must be `[ok]` before
touching ChatGPT. (No CloudFront proxy or extra infra is needed - Bedrock AgentCore serves the
RFC 9728 metadata natively.)

### 2. Enable Developer mode

ChatGPT web → **Settings → Apps** (a.k.a. Apps & Connectors) → **Advanced settings** →
toggle **Developer mode** ON.

Note the second toggle, **"Enforce CSP in developer mode"**: OFF (default) gives dev widgets
unrestricted network and shows a `CSP off` badge on every widget; ON applies the production CSP.
Leave it OFF for first bring-up; retest with it ON before trusting any CSP-dependent behavior.

### 3. Create the app (connector)

Settings → Apps → Advanced settings → **Create app**:

| Field | Value |
|---|---|
| Name | `pairputer` |
| Description | optional |
| Connection | **Server URL** = the `McpEndpoint` output (the full `https://bedrock-agentcore..../invocations?qualifier=DEFAULT` URL) |
| Authentication | **OAuth** |

Then open **Advanced OAuth settings** (it will say it discovered OAuth settings - that's the
metadata chain from step 1 working):

- **Registration method:** `User-Defined OAuth Client` (DCR/CIMD will show as unavailable - expected; Cognito supports neither, and neither is required).
- **Callback URL:** ChatGPT displays this connector's own callback,
  `https://chatgpt.com/connector/oauth/<id>`. **Copy it - you need it in step 4.**
- **OAuth Client ID:** the `ChatGPTClientId` stack output.
- **OAuth Client Secret:** leave empty. **Token endpoint auth method:** `none` (public PKCE client).
- **Default scopes:** leave as discovered. (Don't bother unchecking email/phone/profile - the
  connect flow requests everything Cognito advertises regardless; the Cognito client allows the
  full standard set for exactly this reason. See Walls below.)
- **Base scopes:** add `pairputer-mcp/invoke`.

Check **"I understand and want to continue"** → **Create**.

### 4. Register the per-connector callback in Cognito

```bash
substrate/wire-chatgpt.sh --register-callback 'https://chatgpt.com/connector/oauth/<id-from-step-3>'
```

This adds the URL to the Cognito ChatGPT client (idempotent; preserves previously registered
connector callbacks). Skipping this step = the OAuth popup bounces with `redirect_mismatch`.

### 5. Connect (OAuth)

On the app's page click **Connect** → **Sign in with pairputer** → a popup opens the Cognito hosted
UI → sign in with your pairputer user (e.g. the super-admin email + password). The popup closes and
the app shows **Connected**.

### 6. Pull the tools

On the app's page click **Refresh** (next to Information). "Actions refreshed" should appear and
the Actions list should show the platform tools (`play_capsule`, `freeze`, `thaw`, `trash_microvm`,
`pairputer_session`, `list_capsules`, …) plus any capsule cartridge tools (`computer_use_desktop__*`).
Re-click Refresh any time the server's tool surface changes - ChatGPT does not re-pull on its own.

### 7. Play

New chat → type:

> Use the pairputer app to open the Pairputer Workbench (play_capsule) so we can share a live desktop.

The widget renders inline; if the pill shows `STOPPED`/`SUSPENDED`, click the
widget's own **🔥 Launch/Thaw** button (widget-initiated tool calls work on ChatGPT). Wait
~15-30 s for a cold boot; video starts at ~30 FPS. Click the game to focus, play with
keyboard/mouse; 🔊 Sound toggles audio; ❄ Freeze suspends the VM (billing paused), 🔥 Thaw resumes
the exact same session.

**Desktop:** nothing extra - once connected on web, the app is available in the ChatGPT desktop
app automatically.

---

## Walls we hit (so you don't)

| Symptom | Cause | Fix |
|---|---|---|
| OAuth popup opens, instantly closes, red OpenAI error | ChatGPT's connect flow requests **every** scope in Cognito's discovery (`openid email phone profile`) regardless of the connector's scope config; the client rejected the extras → `invalid_scope` bounce | The `ChatGPTClient` in `identity.yaml` allows the full standard OIDC set + `pairputer-mcp/invoke`. If you see this on an old stack, redeploy identity or `wire-chatgpt.sh --register-callback` (it re-asserts scopes too). Diagnose by replaying `/oauth2/authorize` with curl and bisecting scopes; allow ~1 min for Cognito config propagation |
| OAuth popup shows Cognito error page | Callback not registered (`redirect_mismatch`) | Step 4 |
| OAuth `redirect_mismatch` AFTER a deploy (was working before) | **Every `deploy.sh` re-deploys identity.yaml, and CloudFormation RESETS the ChatGPT client CallbackURLs to just the static legacy URL - dropping the per-connector callback ChatGPT uses.** | Re-run `substrate/wire-chatgpt.sh --register-callback '<the connector callback URL>'` after any deploy. The connector's callback id is stable per connector instance, so it's the same URL as before unless you deleted+recreated the connector. (wire-chatgpt.sh preserves existing callbacks + the full scope set.) |
| Any call errors: `unknown image_id: …` | A caller used a stale or guessed id | Expected fail-closed behavior. Run `list_capsules` and retry with the exact registered id; the widget itself defaults to the current capsule. |
| "No app actions available yet" | ChatGPT hasn't pulled tools | Step 6 (Refresh) |
| Widget shows stale behavior after a server redeploy | ChatGPT caches the widget resource per app version | Settings → Apps → pairputer → **Refresh** (bumps the version), then reload the conversation - reload alone is NOT enough |
| Tool calls time out | ChatGPT's tool budget is ~60 s | `play_capsule` cold boot (~15-30 s) fits; anything longer must return fast (see the fire-and-forget `drive_goal` pattern) |
| **Desktop:** widget flashes then disappears; model reports the launch "blocked by the platform safety check" | Desktop applies the app-permission gate to write tools (`play_capsule`) more aggressively than web; a stale conversation can wedge it | Start a **new chat** (fixed it live 2026-07-08); if it persists, Settings → Apps → pairputer → Permissions → allow. The widget's own 🔥 Launch button (user gesture) also bypasses the model-call gate |
| **Desktop:** Pop out docks a right-side panel; stream stalls (FPS 0.0 · connection terrible) | Desktop's pop-out is a docked panel and the transition severs the player's SSE streams (web survives) | Widget ≥2026-07-09: stall watchdog auto-reconnects (re-mints token, stop→start streams) within ~5 s; exclusive mode-button labels + safeArea padding fix the stacked/overlapped chrome |

## What ChatGPT gives us beyond Codex (all verified live except where noted)

- **Widget-initiated `callTool` WORKS** (boot state reads, Launch/Thaw/Freeze buttons, token
  refresh). The Codex wall #17 does not apply.
- **Direct outbound networking from the widget** to `openai/widgetCSP.connect_domains` origins is
  documented (fetch/SSE/WebSocket) - unverified by us; we kept the relay player iframe (works via
  `frameDomains`, zero player duplication). Direct-connect is a possible later simplification
  (needs CORS on the relay).
- **Display modes:** inline / fullscreen / **PiP** via `window.openai.requestDisplayMode({mode})` - negotiated, user-gesture-only, PiP → fullscreen on mobile; transitions may REMOUNT the widget
  (verify with PROBE-8). Phase 4 wires the Pop-out UX.
- **~60 s tool budget** (vs Codex ~25 s).

## Auth architecture (why there's no proxy)

Bedrock AgentCore natively answers unauthenticated MCP requests with
`WWW-Authenticate: Bearer resource_metadata="<.well-known/oauth-protected-resource>"` and serves
that document pointing at the Cognito issuer (PROBE-4, [README](./README.md)). Cognito serves OIDC
discovery (`/.well-known/openid-configuration`); the RFC 8414 path 400s, and the MCP spec's OIDC
fallback covers it - ChatGPT completed discovery against it live. Static public PKCE client; no
DCR, no CIMD, no client secret.

## E2E checklist

1. OAuth round-trip completes; tools listed after Refresh. ✅ 2026-07-08
2. `play_capsule` → widget renders → video streams → keyboard/mouse reach the capsule → audio
   plays. ✅ all human-confirmed 2026-07-08
3. Freeze → leave/reopen thread (frozen overlay, VM not woken) → Thaw → play resumes. ✅ (freeze +
   thaw verified; leave/reopen-while-frozen still to run)
4. **Pop out (PiP)** → game floats while chat scrolls beneath → return inline; stream survives
   both transitions. ✅ 2026-07-08 (fullscreen toggle shipped; PiP button is host-gated - Codex
   never sees it)
5. >15-min session: widget-initiated `pairputer_session` refresh keeps the stream alive.
6. Repeat 2-5 on ChatGPT **desktop**. ✅ 2026-07-09 human-confirmed (gameplay, sound,
   keyboard/video/mouse, pop-out - docked panel, stall watchdog recovers the stream)
7. Repeat with **Enforce CSP in developer mode ON**.
8. Codex regression: the checklist in [`codex.md`](./codex.md) still passes. ✅ 2026-07-08

## Probe results

See [`README.md`](./README.md): PROBE-1 ✅ (renders `text/html;profile=mcp-app` unchanged),
PROBE-7 ✅ (widget callTool works), PROBE-4/6 ✅ (auth discovery). Pending: PROBE-3 (audio/autoplay
with CSP ON), PROBE-8 (display-mode remount).
