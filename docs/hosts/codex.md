# Host: OpenAI Codex

The original host. Its constraints shaped the platform architecture; they're consolidated here so
new-host work doesn't re-learn them (sources: CLAUDE.md walls, blog2.md).

## Capabilities / constraints (all empirically proven)

- **Bridge:** `window.openai` (Apps SDK dialect). Reads `openai/outputTemplate` on tools; resource
  mime `text/html;profile=mcp-app`.
- **No cross-origin ANYTHING from the widget** - fetch, EventSource, WebSocket all blocked before
  CSP (`connectDomains` not honored). The relay player iframe (frame escape hatch using
  `frameDomains`) is the only data path. Input rides batched `POST /input` because `connect-src`
  allows only `https://` (wall #10).
- **Inline display mode only.** `requestDisplayMode` returns `inline`; the widget's granted-mode
  handling degrades gracefully.
- **~25s hard tool-call ceiling** (remote MCP; `tool_timeout_sec` does NOT raise it - see
  memory/codex-mcp-tool-timeout-25s). Anything long returns fast + rides the live stream.
- **Widget-initiated `callTool` is unreliable** (wall #17) - the launch payload must ride the
  initial `play_capsule` result; token refresh degrades to the reconnect overlay when callTool
  fails.
- **One widget per `tools/call`** - Codex paints a fresh player widget for EVERY tool call, so a
  retried `play_capsule` STACKS a second (stream-starved) player. Seen live 2026-07-09: the model
  called `play_capsule` with a stale legacy image id → server raised `unknown image_id` → model
  retried with the current id → two stacked widgets, the second stuck "FPS 0 · reconnecting". The widget
  now defaults to the current capsule; server routing nevertheless requires an exact registered id so a
  stale tool can never cross-bind to a different sole capsule. ChatGPT/Claude render one delivered
  payload rather than one widget per call.
- **Caches the tool→resource binding** across logout/login/version bumps: the resource URI
  `ui://pairputer-platform/app.html` + mime must NEVER change (locked by tests/test_hosts.py). Ship
  new widget HTML by changing the SERVER NAME; restart the Codex app to bust HTML cache.
- **OAuth:** static public PKCE client (`CodexClientId`), callback
  `http://localhost:5555/callback/<base64url(sha256(McpEndpoint)[:9])>` - computed + registered at
  deploy time (agentcore.yaml CallbackRegistration) and by `wire-codex.sh`.

## Setup

`substrate/deploy.sh` wires `~/.codex/config.toml` automatically; else `substrate/wire-codex.sh`.
Then `codex mcp login pairputer`.

For a **console 1-click** deploy (no CLI), paste the stack's `McpFullEndpoint` output into
`~/.codex/config.toml` and run `codex mcp login pairputer`. The output is a `[mcp_servers.pairputer]`
block - real multi-line TOML.

> **Newline gotcha (cost a real debugging session):** the AWS CloudFormation console flattens the
> copied `McpFullEndpoint` value onto ONE line. Pasting that is invalid TOML, so Codex fails to parse
> `config.toml` and silently shows NO MCP connectors at all (not only pairputer). Put each
> `key = value` and each `[…]` table header back on its own line. The invite email now warns about this.

> **Codex ↔ ChatGPT surface (observed 2026-07-17, mechanism unverified):** with the pairputer block in
> `~/.codex/config.toml`, the `pairputer` connector was also visible under ChatGPT - OpenAI appears to
> have converged the Codex/ChatGPT app surface. Not independently confirmed here; the config file is
> still the Codex source of truth. ChatGPT's own connector setup ([`chatgpt.md`](./chatgpt.md)) uses the
> `ChatGPTClientId`, not the Codex one.

## E2E checklist (regression gate for any MCP-layer change)

1. `codex mcp login pairputer` (or existing session) → `open pairputer` / `play_capsule`.
2. Widget renders; video+audio stream; keyboard/mouse reach the capsule.
3. Freeze → suspended overlay; leave thread + return → VM stays suspended.
4. Thaw → stream resumes. Trash → fresh VM.
5. Session >15 min → token refresh or (if callTool fails) reconnect overlay with
   `codex mcp login pairputer`.
