# Pairputer Workbench: one shared desktop, every control dialect

## The thesis

The Workbench is **not** "a non-vision computer-use agent." It is an **open, shared remote
desktop** — a real Linux MicroVM a human sits at — that speaks *every* control dialect an AI
(or human) might bring, at the same time, on the same machine.

The moat is not "our model clicks better." It is:

> A disposable, resumable MicroVM where a human and any frontier agent are first-class
> co-operators — the human can grab the mouse and *actually drive the real system* at any
> instant, and the agent can use whichever control surface it's good at: pixels, semantics,
> or raw shell.

Vision was never the enemy. **Forcing one modality** was. So we expose them all and let the
connecting agent choose.

## The three dialects (all live, same desktop)

| Dialect | Who brings it | Tools | Observation |
|---|---|---|---|
| **CUA / pixel** | OpenAI CUA, Anthropic Computer Use, any screenshot-loop agent | `screenshot` + `computer_action` | PNG pixels |
| **Semantic** | Pairputer-native / a11y-first agents | `observe`, `ui_tree`, `browser_query`, `workspace_*`, `run_command` | AT-SPI + CDP DOM/AX trees, structured |
| **Human** | The person | direct mouse/keyboard via the relay | their own eyes |

All three drive the **same X11 desktop through the same XTEST spine and the same human-first
arbiter**. There is one pointer, one keyboard, one screen. The human always wins.

### Advertised vs hidden tools (since image v3.0)

Only a 12-tool core is registered in the host's `tools/list` (`observe`, `screenshot`,
`computer_action`, `ground_target`, `drive_task`, `task_status`, `run_command`,
`workspace_read/write/upload`, `browser_open`, `browser_query`) — tool schemas cost model context on
*every* turn, and the 33-tool surface was ~8-10K tokens/turn. The other 21 carry `advertise: false` in
the manifest: **identical gates, zero capability change** — list them with `capsule_metadata`, call
them with `capsule_invoke` (accepts namespaced or bare tool names; parameter is `capsule_id`).
Live-proven: Codex (gpt-5.5) needed the hidden `workspace_describe`, discovered it via
`capsule_metadata`, and invoked it correctly, unprompted beyond the breadcrumb in `observe`'s
description.

## Visible-by-default cursor presence (agent halo)

The whole point of co-presence is that the human SEES the agent work. But the semantic dialects
(CDP/AT-SPI/filesystem) don't touch the pointer — only pixel `computer_action` moves the real cursor.
So without help, a human watching the stream sees pages change and files appear with no visible agent.

The fix is a single `_present_action` hook in the bridge that runs after EVERY effectful action
(`computer_action` excepted — it already injects real XTEST motion):

- **Glide routes** (`browser_open`/`browser_action`, `ui_action`/`accessibility_action`, `open_app`,
  `focus_window`): sweep the REAL cursor (~300ms smoothstep, via the `agent_raw` input path) to the
  action's `screenTarget`. Browser targets come from protocol-level CDP geometry
  (`Browser.getWindowForTarget` + `Page.getLayoutMetrics` — never `Runtime.evaluate`, a pinned no-page-JS
  invariant); `ui_action` gets AT-SPI screen extents (element center).
- **Keepalive routes** (`workspace_*`, `run_command`, `export_artifact`) with no natural screen location:
  a 1px cursor nudge re-asserts `owner=agent` so the blue halo + "Agent" label stay lit while the agent
  works in the shell/filesystem.

Real XTEST motion means real frames in the stream AND the truthful `owner=agent` attribution — no fake
second cursor. `presentation_mode="fast"` opts out. **The halo itself is a WIDGET overlay** drawn over
the video client-side (`#ghostcur`/`#coplay` in app.html, fed by the co-play `/events` stream) — it is
NOT in the VM's desktop pixels, so a VM screenshot never shows it; only the real chat widget renders it.

**KNOWN GAP (2026-07-12):** the browser glide-to-a-meaningful-target is inconsistent — CDP fresh-tab
geometry frequently fails, so a browser action often falls back to the in-place keepalive nudge instead
of gliding to the clicked element. Presence (`owner=agent`, halo lit) is reliable; the cursor moving to
the exact element is not yet. See `docs/live-qa-2026-07-12.md` §5.

## What makes it "drop-in" for OpenAI/Anthropic computer-use loops

A stock computer-use loop emits actions like `{action:"click", x, y}`, `{action:"type", text}`,
`{action:"key", keys:"ctrl+s"}` and expects a screenshot back each turn. To make that Just Work:

1. **`screenshot`** returns real desktop pixels (already existed).
2. **`computer_action`** accepts the *standard* CUA action vocabulary and maps it 1:1 to XTEST
   via `cua_adapter.py`. **No `target_proof`, no epoch bookkeeping** — the two things a stock
   loop cannot produce. It supports the union of OpenAI CUA and Anthropic Computer Use shapes:
   `click / double_click / right_click / middle_click / move / mouse_down / mouse_up / scroll /
   type / key / drag / wait`, single or `{actions:[...]}` batched, with both `x,y` and Anthropic's
   `coordinate:[x,y]`.

So integration is: point the agent's MCP at the Workbench, alias its computer-use tool to
`screenshot` + `computer_action`, and it drives the desktop with zero custom glue.

## Why opening this up is SAFE here (and would not be elsewhere)

The semantic path keeps `target_proof` + epochs (anti-drift, exact-consent). The CUA path drops
them. That's a deliberate, defensible trade because of what the substrate already guarantees:

- **The human always wins — structurally.** `computer_action` submits as the `agent_raw` actor.
  Any authenticated human pointer/keyboard event (a) seizes focus, (b) advances the epoch, (c)
  releases all agent-held keys/buttons, (d) preempts the brain, and (e) puts the agent in a
  cooldown during which every raw batch is dropped (`reason: "human_active"`). This is the same
  arbiter DOOM proved. A runaway CUA loop cannot lock the human out.
- **The VM is disposable.** `target_proof` exists to stop an agent acting on a *stale* target.
  But the human can Freeze, Thaw, or Trash the VM at will — a mis-click is free and recoverable.
  The cost/benefit that justifies proof for a persistent machine inverts for a throwaway one.
- **The real-world boundary still holds.** Autonomy mode frees *in-VM* effects, but `policy.py`
  still gates `external_commit` (form submit, upload, publish) and `credential`/`financial`
  effects at their true commit point — regardless of dialect. Opening pixel control does not
  open the door to the outside world.

Net: we relax the *anti-drift* control (cheap to lose here) while keeping the *human-authority*
and *external-world* controls (never relaxed).

## Autonomy posture (context)

The Workbench ships **autonomy-on** (`PAIRPUTER_WORKBENCH_AUTONOMY=true`): in-VM effects run
without per-action approval; only external-world commits gate. `computer_action`,
`run_command`, file writes, clicks, and browser navigation all just run. See
`policy.py::PolicyEngine.evaluate` and the `autonomy` field on `TaskContract`. Set the env to
`false` (or pass `autonomy:false` to `drive_task`) to restore strict, approval-per-action mode.

## Data path

```
frontier host (Codex / ChatGPT / Claude, or any MCP client)
   │  MCP: screenshot + computer_action   (or observe + ui_action + run_command …)
   ▼
generalized Pairputer MCP  ──►  authenticated :443 proxy  ──►  agent_bridge.py :6905
   /computer/action ──► cua_adapter.to_events() ──► _raw_input(mode="raw")
                                                        │  ws :6904, key-authed
                                                        ▼
                                        input_ws.py  InputArbiter.submit("agent_raw")
                                          human-first cooldown + XTEST inject
   the human's mouse/keyboard ──► relay ──► same InputArbiter.submit("human")  (always wins)
```

## Files

- `rootfs/opt/capsule/cua_adapter.py` — pure CUA-vocab → XTEST-event translator (unit-tested,
  `tests/test_cua_adapter.py`).
- `rootfs/opt/capsule/agent_bridge.py` — `/computer/action` route, `_computer_action`,
  `_raw_input` (mode="raw" submit).
- `rootfs/opt/capsule/input_ws.py` — `agent_raw` actor branch: no proof/epoch, keeps the
  human-active cooldown (`tests/test_computer_use_runtime.py::test_agent_raw_input_needs_no_proof_but_still_yields_to_human`).
- `capsule.yaml` — `computer_action` tool (open, `requiresApproval:false`).

## What this buys, and what it doesn't

**Buys:** any computer-use agent on the market can drive the Workbench today, on a machine a
human co-inhabits and controls. That co-presence + disposability is the differentiated product;
the agent's raw capability is borrowed from whichever frontier model connects.

**Doesn't buy:** SOTA on OSWorld by itself. The research is clear the frontier is grounding +
long-horizon reliability, and no single team out-models the labs. Where the Workbench can win is
the *system around the model*: shared authority, semantic + pixel + shell fused, verified effects,
and freeze/thaw continuity. The open surface is what lets us plug the best model of the week into
that system instead of betting on one.

## Next experiments (from the SOTA research)

1. **Deterministic execution paths over clicking** (BrowseComp 7.0%→29.6%, 4.2×): prefer
   `run_command` / CDP / workspace effects with evidence over pixel-clicking whenever possible.
2. **Programmatic DOM/AX pruning** (Prune4Web: grounding 46.8%→88.28%): LLM-emitted filter code
   over the CDP/AT-SPI tree instead of dumping it.
3. **A11y compaction** (A11y-Compressor: −78% tokens, +5.1pp): replace the blunt `max_nodes=500`
   truncation with semantic redundancy reduction.
4. **Verify-then-retry loop**: agents spend <7% of budget on verification; the Workbench's
   evidence/postcondition machinery already exists — expand it.
