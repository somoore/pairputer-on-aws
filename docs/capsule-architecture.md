# Capsule architecture — cartridges, not baked-in

**Decision (Scott, 2026-07-03):** capsules are **game cartridges/discs**, fully decoupled from the
pairputer substrate. This supersedes the single-capsule-baked-into-the-root-stack model.

## The model

1. **The substrate (pairputer platform) deploys first** and on its own — Cognito, the MCP control plane
   (AgentCore), the relay/data plane, session store. It ships with **no capsule required** (the current
   `BundleReferenceCapsule=false` path already proves the platform runs capsule-empty).
2. **Each capsule is its own CloudFormation stack**, deployed **after** the substrate — like inserting a
   cartridge. A capsule stack builds its `AWS::Lambda::MicrovmImage` and registers itself. Deploying a new
   capsule = deploy its stack; removing one = delete its stack. **No substrate rebuild, ever.**
3. **The MCP server discovers capsules by TAG, not by a baked-in registry.** `list_capsules` enumerates
   MicroVM images tagged as pairputer capsules (e.g. `pairputer:capsule=true` + `pairputer:capsule-id`,
   `pairputer:capsule-name`, and a pointer to the capsule's manifest). It lists/describes ONLY
   pairputer-tagged capsule images — never MicroVM images created outside/for-use-outside pairputer.
   Tag-scoped discovery is what makes it dynamic without redeploying the control plane.

## Why this is right

- **Capsule lifecycle ⊥ platform lifecycle.** The friction that forced a full rebuild to test agent-doom
  (2026-07-03) disappears — capsules come and go independently.
- **N capsules, no CloudFormation loops.** The "CFN has no loops" problem dissolves: there's no N-in-one
  template. Each capsule is one stack; the substrate just needs to grant the control plane permission to
  discover + control any pairputer-tagged image, and the MCP reads the tag namespace at runtime.
- **Third parties ship capsules** as standalone stacks against a deployed substrate — the real
  "bring your own capsule" promise.

## What the substrate must provide for this

- **Discovery IAM**: the MCP ControllerRole needs `lambda:ListMicrovmImages` (or tag-based
  `resourcegroupstaggingapi:GetResources`) + `Get/Run/Suspend/Resume/Terminate/CreateMicrovmAuthToken`
  scoped to `pairputer:capsule=true`-tagged image ARNs (a tag condition, not a hardcoded ARN list).
- **A capsule-stack template** (`capsules/<name>/stack.yaml` or a shared `nested/capsule.yaml`) that any
  capsule instantiates: builds the image from the capsule's context, TAGS it with the pairputer capsule
  namespace + its manifest, done.
- **MCP `list_capsules`/registry** reads the tag namespace at call time (cache + refresh) instead of the
  static `PAIRPUTER_IMAGE_REGISTRY` env — or keeps the env as a fallback/seed.

## Migration from today's single-capsule root stack

Today `DoomImageStack` is a nested stack inside the root, and the registry/manifest are baked into
AgentCore env. Steps:
1. Extract the capsule image build into a standalone deployable stack (`capsules/hellbox-doom/stack.yaml`,
   `capsules/agent-doom/stack.yaml`) that tags its image.
2. Substrate stops bundling a capsule by default (or keeps hellbox as an optional convenience bundle);
   the control plane's IAM moves from a fixed `CapsuleImageArns` list to a tag condition.
3. MCP: tag-based discovery for `list_capsules` + per-capsule manifest read from the tag/manifest pointer.
4. `deploy.sh` deploys the substrate; a new `deploy-capsule.sh <name>` deploys a capsule stack against it.

## Status (2026-07-03)

**Built (additive — the existing bundled path still works):**
- `capsules/nested/capsule-stack.yaml` — standalone capsule stack (generalized off the DOOM image stack).
  Builds ONE capsule image and tags it: `pairputer:capsule=true`, `pairputer:capsule-id/-name/-description`,
  `pairputer:capsule-manifest-ssm`, and `pairputer:capsule-release-ssm`. Keeps the proven MicroVM reaper
  (safe teardown).
- `substrate/deploy-capsule.sh <name>` — inserts a cartridge: snapshots and packages the context, stages a
  content-addressed immutable manifest, then deploys the capsule stack. A CloudFormation custom resource
  binds that exact manifest to the exact ACTIVE image version in an immutable release record and advances
  `/pairputer/capsules/<id>/current` only as its final write. Remove = delete the stack.

**Release consistency (2026-07-10):** capsule discovery advertises the stable current-pointer tag
`pairputer:capsule-release-ssm`. The pointer contains `capsuleId`, `releaseParameter`, and `releaseDigest`.
The immutable release contains the pinned `imageArn` + `imageVersion`, immutable `manifestParameter` +
`manifestDigest`, and build-context digest/URI. Digests cover the exact SSM bytes and canonical JSON.
CloudFormation serializes image publication with the pointer commit; a failed build or manifest check
leaves the previous pointer untouched, and rollback republishes the previous release. Runtimes must use
the pinned image version and fail closed for new launches when this release chain is absent or invalid.

**Chunked manifests (2026-07-11):** one SSM parameter caps at 8 KiB even compressed, and a rich tool
catalog (the Workbench's 33 tools) exceeds it. When the gzip+base64 manifest is over the cap,
`deploy-capsule.sh` splits it into immutable `/partN` parameters under the manifest's content-addressed
name and stores a `chunked:v1:<count>:<sha256-of-joined-payload>` header at the primary name. The
release `manifestDigest` still covers the primary value literally (so the CFN release publisher is
unchanged), and integrity chains release digest → header → joined payload. `server.py`
(`_expand_chunked_manifest`) reassembles and verifies on read; parts are staged before the primary so a
visible header always resolves; any part/header tamper fails closed (covered by
`tests/test_capsule_release_binding.py`).

**Advertised vs hidden tools (2026-07-11):** a manifest tool may set `advertise: false` — the MCP
server then skips registering it in `tools/list` (a pure per-turn context-cost optimization; every
connected tool's schema is serialized into the model context on every turn). Hidden tools keep
identical gates and remain callable through `capsule_invoke` (same approval + sensitive-pattern
screening at call time) and discoverable through `capsule_metadata`. Put discovery breadcrumbs in the
advertised tools' descriptions so models find the long tail when they need it. Full rationale,
measured numbers, the Workbench's reference split, and the capsule-author checklist:
[`mcp-tool-efficiency.md`](./mcp-tool-efficiency.md).
- MCP tag-based discovery: `_discover_capsules_by_tag()` (tag:GetResources on `pairputer:capsule=true`,
  cached) + `_effective_registry()` (env seed ∪ discovered, discovered wins). `list_capsules`,
  `_image_arn`, `_default_image_id`, `_capsule_name` all read the effective registry — so a cartridge
  deployed AFTER the substrate is listed/controlled with NO control-plane redeploy.
- IAM: control-plane can `Run/Get/Suspend/Resume/Terminate/CreateMicrovmAuthToken` on ANY
  `pairputer:capsule=true`-tagged image (tag condition, not a fixed ARN list) + `tag:GetResources` for
  discovery. Tag-scoped, so it grants nothing over non-pairputer MicroVM images.

**Per-capsule manifest → per-capsule tool registration (DONE 2026-07-03):**
- The MCP now registers the UNION of Tier 2 tools across EVERY capsule known at startup — the env-seeded
  bundled capsule PLUS each tag-discovered capsule, whose manifest is read from its SSM param
  (`_read_manifest_from_ssm`) via the `pairputer:capsule-manifest-ssm` tag. `_all_capsule_manifests()`
  returns `{image_id: manifest}`; the registration loop registers each capsule's namespaced tools, each
  **bound to its own capsule's image_id** (so the agent needn't pass it) and screened against **that
  capsule's own** `safety.sensitivePatterns`. Tool-name dedup: first-declared wins, so a name collision
  across capsules never cross-binds. Tier 1 primitives turn on if ANY capsule declares `interaction.tier1`;
  the bridge gate (`_AGENT_INTERACT_ALLOWED`) reflects all capsules. FastMCP is static-registration, so
  this happens once at startup — a capsule inserted later is picked up on the next server start (acceptable;
  a `capsule_invoke(id,tool,args)` hot-add path is deliberately deferred until a capsule ships mid-session).
  Proven in-process by `tests/test_agent_capsule.py::test_n_capsules_each_register_their_own_tools`
  (env capsule `doom_*` + a discovered SSM-manifest `shell_*` both register on one server).

**Bundle retired + cartridge PROVEN LIVE (2026-07-04):**
- The substrate now deploys BARE (`PAIRPUTER_BUNDLE_REFERENCE_CAPSULE=false ./deploy.sh`): no bundled
  capsule, no env manifest, no DoomImageStack. agent-doom is a pure cartridge
  (`deploy-capsule.sh agent-doom` → stack `pairputer-capsule-agent-doom`).
- Verified end-to-end on AWS: the MCP **discovered the cartridge by tag, read its manifest from SSM,
  and registered its platform-namespaced tools** (`agent_doom__observe/act/reset_episode/save_snapshot/
  load_snapshot` + Tier 1) with ZERO capsule config on the control plane. `list_capsules` shows the
  correct manifest name ("Agent DOOM"). `agent_doom__act` moved the live player; the agent started the
  game itself via Tier 1 keys. Session tokens carry a per-capsule `agentInteract` claim.
- Deploy-path bugs fixed en route: override-validator preview-SDK hang (lazy require), suspended-VM
  image pin invisible to ListMicrovms (terminate via session-table id), MicroVM tag charset (sanitize).
- **Hot-add SHIPPED (2026-07-04)**: `capsule_invoke(capsule_id, tool, args)` resolves the capsule's
  manifest AT CALL TIME (tag discovery + SSM, cached) with the same gates as registered tools — a
  cartridge inserted mid-session is drivable immediately. Typed `<capsule>__<verb>` tools still register
  at startup (preferred surface); capsule_invoke covers the gap until instance churn. The bridge gate is
  per-capsule and call-time-capable (`_agent_interact_for`): one capsule's declaration never opens
  another's bridge. Verified live: observe/act moved the player through capsule_invoke.
- **Runtime logs — durable, relay-shipped (2026-07-04)**: a RunMicrovm gameplay VM does NOT stream its
  console to CloudWatch (only image-build/Ready probes do). The RELAY ships them: it pulls each active
  session's capsule service logs from the loopback :9000 /dbg endpoints (now served unconditionally, with
  an ?offset incremental protocol) and PutLogEvents to a relay-owned per-capsule group
  /pairputer/capsule-runtime/<imageId> (stream <src>/<microvmId>) from its OWN task role — the VM keeps
  iamRole:none (no in-VM AWS access). Verified live: a gameplay session's inputws AUDIT lines
  (connect/disconnect, auth) + bridge tool calls (POST /input, /observe, GET /screen) landed durably.
  ALSO FIXED en route: the relay's IAM only covered BUNDLED capsules, so it 502'd on every cartridge
  (video/state/input/logs) — added the tag-conditioned RelayTaggedCapsules grant (matching the MCP
  controller) so the relay reaches any pairputer:capsule=true image. capsule services still also tee to
  the MicroVM group AND the /dbg files; input_ws AUDIT() logs security/lifecycle events unconditionally
  (agent auth REFUSED,
  connections) while per-keystroke chatter stays DEBUG-gated. Verified in the v3 image's streams.

## Dual-mode capsules — local Docker AND MicroVM cartridge (2026-07-08)

A capsule that carries logic (e.g. `agent-doom`'s brain) is developed and shipped in **two runtimes from
one source tree**:

- **Local Docker** — the tight iteration loop. Run the capsule as a container, `docker cp` the changed
  source in, restart, and drive it over its loopback bridge port. This is dev iteration, **not** a deploy:
  the image is only current after a rebuild, and a local container restarted mid-session may sit un-ready
  (never re-passed its gate) — a stale local artifact is not a prod signal.
- **MicroVM cartridge** — the shipped shape. `deploy-capsule.sh <name>` builds the
  `AWS::Lambda::MicrovmImage`, tags it, writes its manifest to SSM; the hosted MCP tag-discovers it and
  streams it into Codex exactly like the bundled DOOM capsule.

### Packaging rule: `.contextignore`, not `.gitignore`

The image build context is packed by walking the filesystem (`rsync`), so **`.gitignore` does not exclude
anything from it** — a gitignored directory is invisible to git and the diff but still rides into the
context zip. Each capsule carries a `.contextignore` (an rsync exclude-from) for build-irrelevant weight.
`agent-doom`'s drops `vision_bench/` (4.1GB of model weights), eval outputs/tooling, and docs, bringing
the context from 3.45GB back to ~192KB / 40 files with every rootfs module intact. A large manifest also
needs SSM **Intelligent-Tiering** (`agent-doom`'s 13-tool manifest exceeds the 4KB standard-tier limit as
JSON).

### Deploy-freshness discipline

A capsule deploy reporting success — and a launch returning `status: success` — proves the **plumbing**,
not that you shipped (and correctly drove) the code you wrote. A 2-hour waste (2026-07-08) traced a wrong
`objective` NOT to a stale image (the image hash-matched `git HEAD`; a cold-booted VM still parsed wrong)
but to a **malformed verification harness**: tier-2 tools take their payload nested under `args`
(`{"args": {"goal": "..."}}`), the harness sent it flat, the brain received an empty goal and correctly
defaulted to `objective: survive`. The new brain was running the whole time. Discipline:

- **Verify latest at each step:** after packaging, grep the artifact for a new symbol and sanity-check its
  size (hash against `git HEAD` when you can); after deploy, confirm the image version advanced; after
  launch, assert a value **only the new code emits** (a changed parse result, a new field, a version
  string) — not `status: success`.
- **Verify your verifier.** A malformed test request produces a real, plausible server response, not an
  error — prove your harness sends what you think it sends (correct `args` nesting) before concluding the
  system is wrong.
- **`trash_microvm` before `play_capsule` after a rebuild is good hygiene** (a warm resume of the old image
  is a real trap), but rule it out by proving it dead — hash the image, cold-boot — not by assuming it.

## The autopilot demo loop (agent-doom v13→v17, 2026-07-08)

The agent-doom cartridge's demo mode is a **watchability system**, not an eval optimizer (see the DEMO
contract in CLAUDE.md). The moving parts, and the invariant each protects:

- **Idle-takeover autopilot** (`autopilot.py`, in-VM supervisor): human idle ~20s → drives
  `drive_goal("clear the map of enemies and reach the exit")` in 120-tic bursts against the same bridge
  a human/Codex uses; any human input → instant handback (arbiter revoke). Chat toggle:
  `agent_doom__autopilot {"enabled": false}`.
- **Async `drive_goal`**: explicit chat commands return `{status: driving, async: true}` in ~8s and run
  the drive on a background thread. Codex hard-caps remote MCP tool calls at ~25s (`tool_timeout_sec`
  does NOT raise it), so no synchronous drive can ever fit. `wait`/`full`/`trace_recent`/autopilot
  callers still run sync. Explicit commands set a `_preempt` event; an in-flight autopilot burst yields
  the lock within a step.
- **Unstick stack** (drive-loop priority order): world-tick-freeze detector (level not simulating —
  intermission/menu/death view → press USE/FIRE to advance), then hard-wedge escape (no physical
  progress for 6 steps OR <16u net displacement over a 12-step window — oscillation defeats per-step
  deltas), which first tries **wedge-door USE** (face the nearest close door line and press USE,
  ignoring stale door memory, capped 5 presses/line and refunded when the wedge ends), then bulls toward
  the deepest open probe.
- **Hazard awareness**: nav cells exist only on non-damaging floors, so grid routes never wade. Combat
  vectors (rush/close/strafe/nudge) refuse to step OFF safe floor INTO a damaging sector — probes sample
  36/72/108u along the bearing because a single 72u sample lands on the far walkway of E1M1's zigzag
  while the path between crosses slime. Hazard escape sprints to the nearest **climbable** (≤24u
  step-up) standable ground, translating every step (acid ticks while you turn). Health-seeking gives up
  on unreachable pickups (40 picks without closing distance → blacklist) instead of grinding — the
  contract's "opportunistic, not an obsessive detour".
- **No hidden movers**: the post-goal disengage (12 cover-retreat steps when a drive ends mid-firefight)
  is **skipped for autopilot bursts** (the next burst starts in ~1s) and hazard-guarded otherwise. Those
  untraced steps were the "circles into the slime" bug — every traced step looked correct while an
  invisible retreat undid them after each burst.
- **Death and victory both continue**: the autopilot presses USE on `player_dead` (map restarts, play
  continues), and the world-tick-freeze detector advances the LEVEL FINISHED intermission (hitting the
  exit used to freeze the demo forever).

**Testing rules for anything that measures capsule behavior:**
- Disable the in-VM autopilot first (`POST /autopilot {"enabled": false}`) — it interleaves its own
  drives with your harness and corrupts every trace (it manufactured a fake "engine input latch" theory
  worth several hours).
- The verification bar for autopilot behavior is a **10+ minute unattended live soak** (tmux + pairputer
  MCP, poll `observe`, judge deaths/corpse-stuck/frozen-position/map progress). Short drives and burst
  sims all passed while the live demo was still broken; only the soak caught the corpse-forever and
  intermission-freeze failure modes.

## Open sub-decisions
- Manifest delivery: capsule stack writes the manifest to SSM/a tag, and the MCP reads it per capsule at
  runtime (so the control plane doesn't need redeploying when a capsule's manifest changes).
- Whether the substrate keeps an optional "bundle hellbox for a batteries-included 1-click" convenience,
  or 1-click = substrate + a separate capsule launch.
