# Capsule architecture - cartridges, not baked-in

**Decision (Scott, 2026-07-03):** capsules are **game cartridges/discs**, fully decoupled from the
pairputer substrate. This supersedes the single-capsule-baked-into-the-root-stack model.

## The model

1. **The substrate (pairputer platform) deploys first** and on its own - Cognito, the MCP control plane
   (AgentCore), the relay/data plane, session store. It ships with **no capsule required** (the current
   `BundleReferenceCapsule=false` path already proves the platform runs capsule-empty).
2. **Each capsule is its own CloudFormation stack**, deployed **after** the substrate - like inserting a
   cartridge. A capsule stack builds its `AWS::Lambda::MicrovmImage` and registers itself. Deploying a new
   capsule = deploy its stack; removing one = delete its stack. **No substrate rebuild, ever.**
3. **The MCP server discovers capsules by TAG, not by a baked-in registry.** `list_capsules` enumerates
   MicroVM images tagged as pairputer capsules (for example, `pairputer:capsule=true` + `pairputer:capsule-id`,
   `pairputer:capsule-name`, and a pointer to the capsule's manifest). It lists/describes ONLY
   pairputer-tagged capsule images - never MicroVM images created outside/for-use-outside pairputer.
   Tag-scoped discovery is what makes it dynamic without redeploying the control plane.

## Why this is right

- **Capsule lifecycle ⊥ platform lifecycle.** The friction that once forced a full substrate rebuild to
  test one capsule change (2026-07-03) disappears - capsules come and go independently.
- **N capsules, no CloudFormation loops.** The "CFN has no loops" problem dissolves: there's no N-in-one
  template. Each capsule is one stack; the substrate only needs to grant the control plane permission to
  discover + control any pairputer-tagged image, and the MCP reads the tag namespace at runtime.
- **Third parties ship capsules** as standalone stacks against a deployed substrate - the real
  "bring your own capsule" promise.

## What the substrate must provide for this

- **Discovery IAM**: the MCP ControllerRole needs `lambda:ListMicrovmImages` (or tag-based
  `resourcegroupstaggingapi:GetResources`) + `Get/Run/Suspend/Resume/Terminate/CreateMicrovmAuthToken`
  scoped to `pairputer:capsule=true`-tagged image ARNs (a tag condition, not a hardcoded ARN list).
- **A capsule-stack template** (`capsules/<name>/stack.yaml` or a shared `nested/capsule.yaml`) that any
  capsule instantiates: builds the image from the capsule's context, TAGS it with the pairputer capsule
  namespace + its manifest, done.
- **MCP `list_capsules`/registry** reads the tag namespace at call time (cache + refresh) instead of the
  static `PAIRPUTER_IMAGE_REGISTRY` env - or keeps the env as a fallback/seed.

## Migration from the single-capsule root stack (COMPLETE 2026-07-20)

The root stack originally nested a bespoke image stack with the registry/manifest baked into AgentCore
env (capped at the 4 KB env budget). The migration is complete, and the end state goes further than
planned: **the bundled capsule uses the exact cartridge template** (`capsules/nested/capsule-stack.yaml`)
in `StageManifestFromContext` mode - an in-stack custom resource reads `capsule.manifest.json` out of the
context zip in S3 and stages the chunked immutable SSM manifest itself, so even a pure console 1-click
registers an any-size capsule with zero local tooling. The bundled default is the **Pairputer Workbench**;
`deploy.sh` deploys the substrate, and `deploy-capsule.sh <name>` inserts any additional cartridge.

## Status (2026-07-03)

**Built (additive - the existing bundled path still works):**
- `capsules/nested/capsule-stack.yaml` - standalone capsule stack (used for cartridges AND the bundled capsule).
  Builds ONE capsule image and tags it: `pairputer:capsule=true`, `pairputer:capsule-id/-name/-description`,
  `pairputer:capsule-manifest-ssm`, and `pairputer:capsule-release-ssm`. Keeps the proven MicroVM reaper
  (safe teardown).
- `substrate/deploy-capsule.sh <name>` - inserts a cartridge: snapshots and packages the context, stages a
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

**Advertised vs hidden tools (2026-07-11):** a manifest tool may set `advertise: false` - the MCP
server then skips registering it in `tools/list` (a pure per-turn context-cost optimization; every
connected tool's schema is serialized into the model context on every turn). Hidden tools keep
identical gates and remain callable through `capsule_invoke` (same approval + sensitive-pattern
screening at call time) and discoverable through `capsule_metadata`. Put discovery breadcrumbs in the
advertised tools' descriptions so models find the long tail when they need it. Full rationale,
measured numbers, the Workbench's reference split, and the capsule-author checklist:
[`mcp-tool-efficiency.md`](./mcp-tool-efficiency.md).
- MCP tag-based discovery: `_discover_capsules_by_tag()` (tag:GetResources on `pairputer:capsule=true`,
  cached) + `_effective_registry()` (env seed ∪ discovered, discovered wins). `list_capsules`,
  `_image_arn`, `_default_image_id`, `_capsule_name` all read the effective registry - so a cartridge
  deployed AFTER the substrate is listed/controlled with NO control-plane redeploy.
- IAM: control-plane can `Run/Get/Suspend/Resume/Terminate/CreateMicrovmAuthToken` on ANY
  `pairputer:capsule=true`-tagged image (tag condition, not a fixed ARN list) + `tag:GetResources` for
  discovery. Tag-scoped, so it grants nothing over non-pairputer MicroVM images.

**Per-capsule manifest → per-capsule tool registration (DONE 2026-07-03):**
- The MCP now registers the UNION of Tier 2 tools across EVERY capsule known at startup - the env-seeded
  bundled capsule PLUS each tag-discovered capsule, whose manifest is read from its SSM param
  (`_read_manifest_from_ssm`) using the `pairputer:capsule-manifest-ssm` tag. `_all_capsule_manifests()`
  returns `{image_id: manifest}`; the registration loop registers each capsule's namespaced tools, each
  **bound to its own capsule's image_id** (so the agent needn't pass it) and screened against **that
  capsule's own** `safety.sensitivePatterns`. Tool-name dedup: first-declared wins, so a name collision
  across capsules never cross-binds. Tier 1 primitives turn on if ANY capsule declares `interaction.tier1`;
  the bridge gate (`_AGENT_INTERACT_ALLOWED`) reflects all capsules. FastMCP is static-registration, so
  this happens once at startup - a capsule inserted later is picked up on the next server start (acceptable;
  a `capsule_invoke(id,tool,args)` hot-add path is deliberately deferred until a capsule ships mid-session).
  Proven in-process by `tests/test_agent_capsule.py::test_n_capsules_each_register_their_own_tools`
  (an env-seeded capsule + a discovered SSM-manifest capsule both register on one server).

**Cartridge model PROVEN LIVE (2026-07-04):**
- The substrate deploys BARE (`PAIRPUTER_BUNDLE_REFERENCE_CAPSULE=false ./deploy.sh`): no bundled
  capsule, no env manifest. The first reference capsule ran as a pure cartridge
  (`deploy-capsule.sh <name>` → stack `pairputer-capsule-<name>`).
- Verified end-to-end on AWS: the MCP **discovered the cartridge by tag, read its manifest from SSM,
  and registered its platform-namespaced tools** with ZERO capsule config on the control plane.
  `list_capsules` showed the correct manifest name; the capsule's typed tools drove the live session.
  Session tokens carry a per-capsule `agentInteract` claim.
- Deploy-path bugs fixed en route: override-validator preview-SDK hang (lazy require), suspended-VM
  image pin invisible to ListMicrovms (terminate using the session-table id), MicroVM tag charset (sanitize).
- **Hot-add SHIPPED (2026-07-04)**: `capsule_invoke(capsule_id, tool, args)` resolves the capsule's
  manifest AT CALL TIME (tag discovery + SSM, cached) with the same gates as registered tools - a
  cartridge inserted mid-session is drivable immediately. Typed `<capsule>__<verb>` tools still register
  at startup (preferred surface); capsule_invoke covers the gap until instance churn. The bridge gate is
  per-capsule and call-time-capable (`_agent_interact_for`): one capsule's declaration never opens
  another's bridge. Verified live: observe/act moved the player through capsule_invoke.
- **Runtime logs - durable, relay-shipped (2026-07-04)**: a RunMicrovm gameplay VM does NOT stream its
  console to CloudWatch (only image-build/Ready probes do). The RELAY ships them: it pulls each active
  session's capsule service logs from the loopback :9000 /dbg endpoints (now served unconditionally, with
  an ?offset incremental protocol) and PutLogEvents to a relay-owned per-capsule group
  /pairputer/capsule-runtime/<imageId> (stream <src>/<microvmId>) from its OWN task role - the VM keeps
  iamRole:none (no in-VM AWS access). Verified live: a gameplay session's inputws AUDIT lines
  (connect/disconnect, auth) + bridge tool calls (POST /input, /observe, GET /screen) landed durably.
  ALSO FIXED en route: the relay's IAM only covered BUNDLED capsules, so it 502'd on every cartridge
  (video/state/input/logs) - added the tag-conditioned RelayTaggedCapsules grant (matching the MCP
  controller) so the relay reaches any pairputer:capsule=true image. capsule services still also tee to
  the MicroVM group AND the /dbg files; input_ws AUDIT() logs security/lifecycle events unconditionally
  (agent auth REFUSED,
  connections) while per-keystroke chatter stays DEBUG-gated. Verified in the v3 image's streams.

## Dual-mode capsules - local Docker AND MicroVM cartridge (2026-07-08)

A capsule that carries logic (an in-VM agent brain) is developed and shipped in **two runtimes from
one source tree**:

- **Local Docker** - the tight iteration loop. Run the capsule as a container, `docker cp` the changed
  source in, restart, and drive it over its loopback bridge port. This is dev iteration, **not** a deploy:
  the image is only current after a rebuild, and a local container restarted mid-session may sit un-ready
  (never re-passed its gate) - a stale local artifact is not a prod signal.
- **MicroVM cartridge** - the shipped shape. `deploy-capsule.sh <name>` builds the
  `AWS::Lambda::MicrovmImage`, tags it, writes its manifest to SSM; the hosted MCP tag-discovers it and
  streams it into the chat host exactly like the bundled Workbench capsule.

### Packaging rule: `.contextignore`, not `.gitignore`

The image build context is packed by walking the filesystem (`rsync`), so **`.gitignore` does not exclude
anything from it** - a gitignored directory is invisible to git and the diff but still rides into the
context zip. Each capsule carries a `.contextignore` (an rsync exclude-from) for build-irrelevant weight.
One capsule's `.contextignore` drops 4.1GB of eval model weights, outputs/tooling, and docs, bringing
the context from 3.45GB back to ~192KB / 40 files with every rootfs module intact. A large manifest also
needs SSM **Intelligent-Tiering** (a 13-tool manifest already exceeds the 4KB standard-tier limit as
JSON; the Workbench's 33-tool manifest additionally chunks - see above).

### Deploy-freshness discipline

A capsule deploy reporting success - and a launch returning `status: success` - proves the **plumbing**,
not that you shipped (and correctly drove) the code you wrote. A 2-hour waste (2026-07-08) traced a wrong
`objective` NOT to a stale image (the image hash-matched `git HEAD`; a cold-booted VM still parsed wrong)
but to a **malformed verification harness**: tier-2 tools take their payload nested under `args`
(`{"args": {"goal": "..."}}`), the harness sent it flat, the brain received an empty goal and correctly
defaulted to `objective: survive`. The new brain was running the whole time. Discipline:

- **Verify latest at each step:** after packaging, grep the artifact for a new symbol and sanity-check its
  size (hash against `git HEAD` when you can); after deploy, confirm the image version advanced; after
  launch, assert a value **only the new code emits** (a changed parse result, a new field, a version
  string) - not `status: success`.
- **Verify your verifier.** A malformed test request produces a real, plausible server response, not an
  error - prove your harness sends what you think it sends (correct `args` nesting) before concluding the
  system is wrong.
- **`trash_microvm` before `play_capsule` after a rebuild is good hygiene** (a warm resume of the old image
  is a real trap), but rule it out by proving it dead - hash the image, cold-boot - not by assuming it.

## Formerly-open sub-decisions (both resolved)
- **Manifest delivery - RESOLVED:** the capsule stack owns the manifest end to end. Script deploys stage
  it to SSM; console deploys stage it in-stack from the context zip (`StageManifestFromContext`). The MCP
  reads it per capsule at runtime, so a manifest change never redeploys the control plane.
- **Batteries-included 1-click - RESOLVED:** the substrate bundles the **Pairputer Workbench** by default
  (`BundleReferenceCapsule=true`) through the same cartridge template, so "bundled" is no longer a
  special path - it is a cartridge the root stack happens to deploy for you.
