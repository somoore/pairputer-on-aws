# Pairputer Workbench implementation index

This document tracks the `computer-use-desktop` capsule implementation - the **Pairputer Workbench**,
the substrate's bundled reference capsule. The generalized substrate remains capsule-agnostic; other
capsules deploy as cartridges alongside it.

## Baseline and decisions

- Implementation baseline: `560d520ff338b55e809f8eb93d3756f5b5f5a89d` on 2026-07-09.
- Architecture and Phase 0 decisions: [ADR 0001](adr/0001-computer-use-desktop-foundation.md).
- Host-neutral shared-computer contract: [shared-computer experience](shared-computer-experience.md).
- Capsule identity: `computer-use-desktop` / **Pairputer Workbench**.
- Default workspace: `/home/app/workspace`.
- Default presentation: `hybrid`; idle assistance is off unless a task contract explicitly enables it.
- Capsule IAM: none.

## Known limitation - proactive idle auto-suspend is not implemented (2026-07-12)

The widget's "Auto-suspend" selector sets the AWS MicroVM `idlePolicy.maxIdleDurationSeconds`, applied
at run/resume. But the MicroVM runs with `autoResumeEnabled: true`, and the workbench holds an always-on
video/audio stream - so the platform idle timer effectively never trips while a viewer is connected (any
traffic re-wakes the VM). Net effect: the box does NOT auto-freeze on its own when a human walks away; it
only suspends on an explicit **Freeze** (which the relay suspend-guard now makes reliable - walls #21/#22).
Real proactive idle-suspend needs **app-level idle detection** (track last human input via the input
arbiter; auto-invoke `freeze()` after the chosen window when no input AND no active drive). Deferred by
choice; the guard + explicit Freeze is the shipped behavior. See [walls #21/#22](walls-and-lessons.md).

## Human desktop shell - dock + on-demand browser (2026-07-13)

The desktop is AI-driven (typed `apps_open`/`browser_open`), but a human who takes the wheel gets a
usable shell:

- **App-launcher dock** (`launcher-panel.py`): a GTK top bar (Pairputer · Files · Editor · Browser ·
  VS Code · Terminal) started from `session.sh`. **Run it with `python3` (system 3.9), NEVER
  `python3.11`** - PyGObject (`gi`) is only installed for 3.9; 3.11 has no `gi` and the launcher
  crash-loops invisibly (wall #24). `python-xlib` (3.11-only, used for the `_NET_WM_STRUT` reservation)
  is imported defensively, so the strut is simply skipped on 3.9 and the dock still shows via its DOCK
  hint + keep_above.
- **The browser NEVER auto-opens.** Chromium is not launched at boot and readiness does not gate on it
  (`chromium_cdp`/`chromium_visible` are informational only). It starts ONLY on demand - dock Browser/VS
  Code buttons, `apps_open("browser")`, or `browser_open`. `browser_open` launches it if CDP is down
  (`_ensure_browser`); a cold CDP start can exceed the ~22s tool budget, so it returns
  `reason=browser_starting, retrySafety=safe` and the retry lands on a warm browser (wall #25).
- **Debugging the desktop headlessly:** `PAIRPUTER_DEBUG=true` exposes `/vmdbg?f=session`, which serves a
  root-owned `/var/log/pairputer-session.log` (session.sh stderr + an Xlib window-geometry dump). This is
  how the `gi` crash-loop was finally found - reach for it before blind-iterating image rebuilds.

Open follow-up: code-server (VS Code in the browser) shows a WebSocket 1006 error when reached via the
`pairputer-preview.invalid` http proxy domain - the browser treats it as an insecure context. Functional
enough to render, but the live WebSocket features degrade; not yet chased.

## Workstream ownership

| Workstream | Owned surface | Integration gate |
|---|---|---|
| Runtime and streaming | Desktop image, X11/Xvnc, audio/video/input, readiness, browser and human apps | ARM64 Docker build; non-black frame; input and semantic self-tests |
| Semantic service | Private proto/gRPC, bridge, workspace/process/app/window/browser/AT-SPI services | Bounded structured evidence; loopback-only ports; bridge tests |
| Brain and safety | Contracts, state machine, SQLite journal, ledgers, skills, policy, approvals, recovery | No success without evidence; epoch and approval race suites |
| Substrate and viewport | Manifest schemas, per-capsule bridges/resources, hard binding, display/cursor/events | Two-capsule tests; second-capsule regression; rendered widget QA |
| Evaluation and release | Fixtures, deterministic workflows, packaging, Docker/AWS lifecycle, security review | Release gates and reproducibility bundle |

## Validation ladder

Run the cheapest deterministic checks first:

```bash
pytest -q
pytest -q tests/test_computer_use_runtime.py tests/test_computer_use_brain.py
docker build --platform linux/arm64 -t pairputer-capsule-computer-use-desktop:local \
  capsules/computer-use-desktop
CAPSULE=computer-use-desktop substrate/local-dev.sh --capsule-only
```

The local container must expose the same runtime contracts used in AWS: `6902` audio, `6903` video,
`6904` input, `6905` authenticated bridge target, `6906` co-control events/state, `9000` lifecycle and
diagnostics, and loopback-only `50051` gRPC plus browser debugging.

Deployed validation is required for MicroVM build, authenticated proxying, relay/host embedding,
freeze/thaw reconciliation, tag/SSM discovery, hot-add invocation, and removal. Never weaken a local
security invariant to make the deployed path pass.

## Verified release snapshot - 2026-07-12 (current)

- AWS: `pairputer-capsule-computer-use-desktop` CREATE_COMPLETE; Workbench **image version 10.0** active.
  Version history: v1 first prod deploy; v2 agent-cursor attribution; v3 slim tool surface; v4 durable
  per-tenant workspace; v6 first cursor-glide; v7-v8 universal-presence + read-only regressions; v9
  X-capture fix; **v10 the `/observe` 502 fix**. The full multi-round live-QA arc (freeze auto-thaw,
  trash-doesn't-stick, stream jitter, live-push "next launch" bug, fullscreen/PiP, visible cursor, the
  `/observe` 502, cold-boot X-capture flakiness) is documented in `live-qa-2026-07-12.md` - READ IT
  before touching the widget lifecycle, the bridge dispatcher, or the presentation layer.
- **Durable per-tenant workspace + chat-reachable storage** (v4): `workspace/persistent/` mirrors to
  a per-tenant S3 prefix at freeze/trash and restores on fresh boot; the `persistent_storage` platform
  tool + widget Files drawer reach it with NO VM running. See `persistent-workspace.md`.
- **Visible-by-default cursor presence** (v6+): a `_present_action` bridge hook glides the real cursor
  to an action's screen target (browser/UI/app) or keeps the owner=agent halo lit for locationless
  actions (file/shell). The halo is a WIDGET overlay over the video - not in VM pixels. KNOWN GAP: the
  browser glide-to-target is inconsistent (CDP fresh-tab geometry often fails → in-place nudge). See
  `open-multi-dialect-surface.md`.
- Earlier state (image v3.0, commits `bb2f045`/`1226af2`/`cb367ca`): all CI-green; 934 tests then, 941+
  now.
- **Slim advertised surface**: exactly **12 of 33** tools registered in `tools/list` (observe,
  screenshot, computer_action, ground_target, drive_task, task_status, run_command,
  workspace_read/write/upload, browser_open, browser_query). The other 21 carry `advertise: false`
  in the manifest - identical gates, callable via `capsule_invoke` (`capsule_id`, namespaced or bare
  tool name), discoverable via `capsule_metadata`. A contract test pins the advertised set. This is a
  per-turn context-cost optimization only; no capability changed.
- Deploy walls fixed en route: manifests over the 8 KiB SSM cap are now **chunked** across immutable
  `/partN` params behind a digest-chained `chunked:v1:` header (deploy-capsule.sh writes, server.py
  reassembles + verifies); the pinned BtbN ffmpeg autobuild had been **pruned upstream** (404 on every
  cold-cache build) and is now mirrored in the public launch bucket, sha256-pinned, for BOTH capsules.
- Headless production E2E (M2M principal): cold boot → RUNNING ~6s; video SSE ~42 KiB/s via CloudFront
  after ~30s in-VM warmup; all three dialects live; freeze → SUSPENDED with stale-token 403; thaw ~5s
  with workspace state surviving suspend/resume; `capsule_approve` refuses the headless caller (fails
  closed); trash → TERMINATED.
- File transfer verified on cloud VMs twice: 293 KB binary, 3 chunks, 6.9s, byte-identical; and real
  Codex (gpt-5.5) composing the full `workspace_upload` envelope from schema alone, then verifying the
  sha via the hidden `workspace_describe` through `capsule_metadata` → `capsule_invoke` discovery
  (SLIM-PASS).
- Agent-cursor attribution is truthful under sustained agent driving (receipts + `observe` both report
  owner=agent, zero spurious human-takeovers/drops, ~1.5s decay to idle) - three live-found races fixed
  in `input_ws.py` (stale receipt snapshot; note-after-inject window; one-shot XResetScreenSaver edges
  debounced with a two-poll requirement).

## Verified release snapshot - 2026-07-10 (superseded)

- Source and deployed capsule context SHA-256:
  `82b76230c8fd5dd3dabb6dc2d7f24db6c7076151c5d45846367da6334ceb17cd`.
- Local release suite: 733 tests passed; all 17 cached-and-continuously-revalidated readiness checks
  passed; all seven strict deterministic bridge workflows passed with the independent workspace oracle.
- AWS: the `pairputer` substrate stack and both capsule cartridge stacks are complete; Workbench image version `2.0` is active at an
  8 GiB minimum. No failed version-suffixed image or stack remains.
- Authenticated AgentCore smoke: 59 tools across both deployed capsules, all 26
  Workbench namespaced tools, and the generic `capsule_invoke` hot-add route.
- Deployed behavior: launch and fused observation succeeded at 1440×900; a confined file was written,
  described with SHA-256 evidence, and reversibly trashed; suspend, thaw, post-thaw observation, and
  final suspend all succeeded. The retained validation VM is suspended.
- The public relay rejects an unsigned/bare request with HTTP 403. The compressed SSM manifest decodes
  to the same capsule ID, 26-tool catalog, memory tier, and context digest above.
- The canonical security diff scan produced no reportable finding. Final lifecycle-only changes were
  then covered by the complete test suite, clean Docker build, live AWS hooks, least-privilege process
  checks, and the authenticated production smoke.

The external same-model A/B/C/D comparisons, 60-minute operator soaks, and any competitive superiority
statement remain explicit launch/claim gates. They are not inferred from implementation results and no
such claim is made here.

## Claim status

No competitive superiority claim is authorized by implementation alone. Such a claim requires the
same-model A/B/C/D evaluation, contemporaneous baseline pinning, statistical thresholds, and published
failure/safety results defined by the plan. Until then the supported statement is:

> Pairputer is designed to provide a more verifiable, resumable, and collaborative computer-use runtime
> around frontier models.
