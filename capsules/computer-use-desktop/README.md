# Pairputer Workbench (`computer-use-desktop`)

A **shared, disposable Linux desktop** that a human and any frontier AI drive together — live-verified,
end to end. It's an independently deployable Pairputer capsule (a cartridge on the existing substrate:
same auth, streaming, freeze/thaw, tag/SSM discovery), additive to Agent DOOM.

The product isn't "our model clicks better." It's the **system around the model**:

> A disposable MicroVM the human can pause/trash anytime, where a human and any frontier agent are
> first-class co-operators. The agent uses whatever control surface it's good at — pixels, semantics,
> or shell — and the human can grab the wheel at any instant. It's a remote session on a real system.

No provider SDK, model key, or AWS role lives in the guest. The host model brings the intelligence; the
capsule brings the world's best hands, semantic eyes, and undo button.

## Proven live (real, not aspirational)

Everything below was verified against a running container:

- ✅ **Builds (~3.85 GB, native ARM64) and boots** with every readiness check green (X11, WM, D-Bus,
  AT-SPI, video, audio, rendered frame, Chromium+CDP, XTEST, tmux, workspace, journal).
- ✅ **The agent drives the desktop** — cursor moves pixel-perfect, click/type/Enter land, apps open,
  browser navigates real sites (example.com, IANA, CNN Lite).
- ✅ **Real multi-step task**: browse → find the featured article → copy → switch apps → paste. Done.
- ✅ **`drive_task` → SUCCEEDED → real file created** with hash evidence.
- ✅ **Host↔VM file transfer** — a 111 KB host file uploaded byte-identical (SHA matched); a PNG
  transferred to the VM Desktop and shown in the file manager.
- ✅ **Real Codex (gpt-5.5) drove the desktop** through the capsule's own MCP tools (see "Multi-host").
- ✅ **Watchable co-presence** — a live "🤖 Agent" cursor overlay tracks the real pointer, with truthful
  human-vs-agent attribution.

## Three control dialects, one desktop

The capsule exposes **every** dialect a host might bring, at once, on the same X11 desktop. A connecting
agent picks what it's good at.

| Dialect | Who brings it | Tools | Sees the desktop via |
|---|---|---|---|
| **Pixel / CUA** | OpenAI CUA, Anthropic Computer Use, any screenshot-loop | `screenshot` + `computer_action` | inline PNG image |
| **Semantic** | a11y-first / Pairputer-native agents | `observe`, `ui_tree`, `ground_target`, `browser_query`, `run_command`, `workspace_*` | structured trees (AT-SPI + CDP DOM/AX) — no vision needed |
| **Human** | the person | direct mouse/keyboard via the relay (or the noVNC dev view) | their own eyes |

All three drive the same pointer/keyboard through the same **human-first arbiter**. `computer_action`
speaks the standard computer-use vocabulary (`click/double_click/right_click/type/key/scroll/drag/wait`,
both OpenAI and Anthropic shapes) so a stock loop works **drop-in** — no `target_proof`, no epoch
ceremony. A synthetic click also focuses/raises the window under it (like a real click), so typed keys
land. See [docs/open-multi-dialect-surface.md](docs/open-multi-dialect-surface.md).

## Autonomy: reckless in the box, safe at the edge

The VM is disposable (pause/trash anytime), so **in-VM effects run with zero friction** — no capability
allow-list, no risk budget, no per-action approval. `run_command`, file writes, clicks, browser
navigation all just run. The **only** things that still gate are effects that **escape the VM**:

- `external_commit` (form submit, upload, publish) and `credential` entry → require approval
- `financial`/`legal` → require human takeover
- the caller's own `forbidden_effects` denylist is always honored

This is deliberate: trashing the VM can't un-send an email or un-charge a card, and untrusted web/file
content (prompt injection) reaching an external effect is the real danger. So the external-commit gate is
what *lets* the agent be reckless everywhere else. Enforced in `policy.py`; on by default
(`PAIRPUTER_WORKBENCH_AUTONOMY=true`). Set it `false` for the original strict, approval-per-action mode.

## Human + agent co-presence

- **Human always wins.** Any human input — via the relay OR a raw VNC viewer / physical console — seizes
  focus, bumps `humanEpoch`, preempts the brain, and releases agent-held keys. Attribution is truthful:
  an X11-idle detector flips owner → **human** for input from *any* source, → **agent** for the agent's
  own injection, → **idle** when quiet, so the overlay never mislabels who's driving.
- **Cooperative turn-taking**, not a fight over one cursor: the agent yields for a brief cooldown after
  you act, then resumes when you're idle. You can genuinely work on one window (edit a file) while the
  agent works on another (run a command) — it doesn't need the cursor for shell/file/CDP actions.
- **Watchable.** The widget renders a "🤖 Agent" halo tracking the *real* cursor (from input receipts, so
  it can't drift), plus a click ripple and an owner badge. A local overlay viewer
  (`scratchpad/workbench-view.html`) shows the same thing over the noVNC dev stream.

## What's inside

- **Desktop**: X11/Xvnc at `1440x900`, Mutter WM, GNOME Text Editor, Nautilus, Xterm/tmux, sandboxed
  Chromium (persistent profile, loopback-only CDP), Python/Node/Git/build tools. H.264 video + Opus audio.
- **Semantic layer** (the no-vision bet): AT-SPI accessibility trees + Chromium CDP DOM/AX, with
  **A11y-Compressor-style compaction** (drop inert scaffolding, dedup) and **Prune4Web-style
  `ground_target`** (natural-language intent → short ranked candidate list, not a guessed coordinate).
- **Task brain** (`desktop_brain_runtime.py`): typed task contracts, an SQLite event journal, skills,
  mandatory postconditions (no false "done"), retry/recovery. Deterministic — no model in the guest.
- **Persistent workspace** at `/home/app/workspace`, preserved across freeze/thaw.
- Readiness on `:9000/ready` fails closed until every subsystem is up.

## File transfer (host ↔ VM)

The MicroVM shares **no filesystem** with the host (isolated by design; no drag-and-drop, no shared
clipboard). Files cross the boundary through the capsule's own tools, integrity-verified:

- `workspace_write` — author content directly (fastest).
- `workspace_upload` — transfer a file's bytes as base64 chunks (per-chunk + whole-file SHA, ≤ 8 MiB).
  **Just works**: auto-commits when the bytes reach `total_size` (no `final` flag), auto-creates missing
  parent dirs (no `mkdir`). Fetch a fresh `expected_world_revision` right before each call.

Full model + worked example: [docs/file-transfer.md](docs/file-transfer.md).

## Multi-host (drive-able by any MCP host)

The capsule is a spec-correct **MCP computer-use target**. The pairputer MCP server exposes the desktop
tools (`computer_use_desktop__screenshot`, `computer_action`, `observe`, …); any host connects and drives.

- **`screenshot` returns an inline image** (MCP `ImageContent`, base64 PNG) — a remote host can *see* the
  desktop, not just a file path. VM-internal paths are stripped from the result text so the host looks at
  the image. Call it bare (`{}`) — no envelope needed.
- **Verified with real Codex (gpt-5.5)**: it drove the desktop through these tools, opened apps, typed
  text. Codex's `exec`/TUI don't yet render MCP tool images into vision (a Codex limitation — our block
  is spec-correct), but Codex **completed the task accurately anyway by falling back to the semantic
  tools** (`observe`/accessibility). That's the multi-dialect design proving itself: when a host can't
  use one modality, another carries it. A host that renders MCP images (Claude, ChatGPT) gets the full
  vision path.

## Resilience

Each service runs in a self-healing restart loop; a service crash (e.g. desktopd OOM) restarts cleanly
and **never takes down the MicroVM** — the `set -e` / `wait -n` cascade that used to kill the whole
capsule on one fault is fixed and guarded by a test. Freeze records a barrier, flushes the journal,
expires approvals, and releases agent input; thaw defaults to human ownership and grants no execution
authority. Prepared/unknown effects are never blindly replayed.

## Local build and smoke

```bash
docker build --platform linux/arm64 --provenance=false --sbom=false \
  -t pairputer-workbench:dev -f Dockerfile .

docker run -d --name workbench --platform linux/arm64 \
  --cap-add=NET_ADMIN --cap-add=SYS_ADMIN \
  -e PAIRPUTER_WORKBENCH_AUTONOMY=true \
  -p 6901:6901 -p 6903:6903 -p 6905:6905 -p 6906:6906 -p 9000:9000 \
  pairputer-workbench:dev

curl -fsS http://127.0.0.1:9000/ready | jq          # wait for 200 (all checks green)
```

**Watch it live**: open `http://127.0.0.1:6901/vnc_app.html?autoconnect=1&resize=scale` (the raw
desktop), or serve `scratchpad/workbench-view.html` for the desktop **with the agent-cursor overlay**.

**Drive it via the bridge** (dev): grab the per-boot key with
`docker exec workbench cat /run/pairputer/bridge-ingress.key`, then POST to `:6905` with the
`X-Pairputer-Bridge-Capability` header — e.g. `/screenshot`, `/computer/action`, `/observe`,
`/accessibility/ground`, `/workspace/upload`.

**Drive it via MCP** (a real host): run the pairputer MCP server in local mode (`PAIRPUTER_LOCAL_MODE=1`
against this container's `:6905`) and point any MCP host at `http://127.0.0.1:8000/mcp`. See
`substrate/local-dev.sh` for the canonical wiring.

Local uses the manifest-selected `chromium-namespaces-v1` seccomp profile (extends Moby's default only
for Chromium's namespace sandbox calls). Production runs inside the Lambda MicroVM, not the host Docker
engine. `substrate/local-dev.sh --stop` tears down the local capsule.

## Deploy and remove (AWS)

```bash
AWS_PROFILE=Production/AdministratorAccess PAIRPUTER_AWS_REGION=us-east-1 \
  substrate/deploy-capsule.sh computer-use-desktop

AWS_PROFILE=Production/AdministratorAccess aws cloudformation delete-stack \
  --region us-east-1 --stack-name pairputer-capsule-computer-use-desktop
```

Deployment packages this directory, stores the manifest in SSM, requests the 8 GiB minimum, and creates a
separately tagged MicroVM image stack. The running MCP discovers it by tag — no substrate rebuild;
`capsule_invoke` is the late-bound route until the next server restart advertises the named tools.

> **Base image is AWS-managed.** `AWS::Lambda::MicrovmImage` only accepts AWS's `al2023-1` managed base —
> you can't swap in Ubuntu for the Lambda-hosted product. Everything above the OS (all the Python capsule
> code) is distro-portable, but the runtime substrate is AL2023.

## Support matrix

| Surface | Status | Primary backend | Fallback |
|---|---|---|---|
| Workspace (read/write/patch/move/trash/**upload**) | supported | confined atomic service | fail closed |
| Commands / tests | supported | tracked PTY / process group | explicit shell mode |
| Chromium (navigate/observe/act) | supported when readiness passes | loopback CDP / DOM / AX | ask host/user |
| Editor / files / terminal | supported | AT-SPI + EWMH | XTEST / visual host reasoning |
| Pixel control (screenshot + `computer_action`) | supported | XTEST + inline-image screenshot | — |
| Grounding (`ground_target`) | supported | AT-SPI intent ranking | ask host/user |
| Image viewer | **not installed** | — | Nautilus thumbnail / add a viewer to the image |
| LibreOffice | deferred | not installed | export standard files directly |
| External / financial commits | policy-gated | exact approval / human takeover | fail closed |

## Safety notes

- Workspace ops are dir-fd/no-follow confined, staged, atomically committed with SHA checks; delete is
  reversible trash by default.
- Browser: loopback-only CDP, non-root Chromium with a verified user-namespace sandbox, sign-in/autofill/
  sync disabled. Navigation SSRF guards (no metadata / link-local / private hosts) hold **even in
  autonomy mode** — autonomy skips the per-task domain *grant*, not the anti-SSRF checks.
- Untrusted content (webpages, docs, terminal output, filenames, downloads) can supply *facts* but can
  never revise the task, add scope, or approve an effect.
- Capsule IAM is `none`; no substrate credentials enter the guest. Pinned + SHA-verified downloads
  (Chromium, FFmpeg, noVNC).

See [EVALS.md](EVALS.md) for the evaluation contracts. No "better than the labs" claim is made without the
same-model A/B/C/D thresholds defined there — the realistic goal is a differentiated *harness* that
borrows frontier capability, not out-modeling the labs.
