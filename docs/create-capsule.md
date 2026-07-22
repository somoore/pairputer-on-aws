# Create a capsule

A **capsule** is the workload pairputer streams into your chat: a Linux MicroVM that produces a live
video, audio, and input stream, plus an optional set of typed tools the AI can call. The bundled
Pairputer Workbench is one capsule. This page explains how to build your own.

You do not need this page to use pairputer. Read it when you want to run something other than the
Workbench, for example a single application, a game, a kiosk, or your own agent environment.

## Capsules and the cartridge model

pairputer separates the **substrate** (the platform) from the **capsule** (the workload), the way a
console separates from its cartridges.

- The **substrate** deploys once: Cognito, the MCP control plane on Bedrock AgentCore, the streaming
  relay, and the session store. It runs with no capsule at all.
- Each **capsule** is its own CloudFormation stack, deployed after the substrate. Adding a capsule
  deploys its stack; removing one deletes its stack. The substrate never rebuilds.

The MCP server finds capsules by **image tag**, not a hard-coded list. A capsule stack builds an
`AWS::Lambda::MicrovmImage` and tags it `pairputer:capsule=true`, along with tags carrying its id, name,
description, and a pointer to its manifest. The server queries for images with that tag on a short cache
interval, so a newly deployed capsule appears in `list_capsules` without redeploying the control plane.

There are two kinds of capsule:

- **Stream-only (Tier 0):** no manifest, no tools. The chat shows a live desktop the human drives, and
  the AI cannot act. This is the simplest capsule to build.
- **Agent-interactive (Tier 1 or 2):** ships a manifest (`capsule.yaml`) that declares typed tools over
  an in-VM agent bridge, so the AI can observe and act alongside the human.

## What a capsule image must do

A capsule is a MicroVM image built from a **build context**, which is a directory containing a
`Dockerfile` and the files it installs. AWS builds the Dockerfile into a MicroVM image server-side;
you do not push a prebuilt image. Capsules are **ARM64**, and AWS builds them on a managed Amazon Linux
2023 base (`aws:microvm-image:al2023-1`), so your Dockerfile installs everything else.

Your image must satisfy two contracts: the **readiness hook** and the **streaming ports**.

### The readiness hook

During the build, AWS boots your image and polls a hook server before it snapshots the image.

- Serve HTTP on **`127.0.0.1:9000`** (loopback only).
- Answer `GET /ready` with **`503` while starting** and **`200` once the capsule is fully up**.
- If `/ready` never returns `200` within the timeout, the build **fails**. This is deliberate: a build
  that cannot prove it is healthy never ships.

For an interactive capsule, hold `/ready` at `503` until an input self-test passes as well (a synthetic
key reaches an X client). The `PAIRPUTER_INPUT_SELFTEST_ENFORCE` flag (default `true`) makes a failed
self-test fail the build, so a capsule with dead keyboard or mouse never ships.

### The streaming ports

The relay connects to fixed loopback ports inside the VM. A capsule produces its stream on these ports:

| Port | Role | Producer |
|---|---|---|
| `6903` | Video | H.264 over a WebSocket, for example ffmpeg capturing the X display with `libx264`. |
| `6902` | Audio | Opus over a WebSocket, for example PulseAudio into ffmpeg with `libopus`. |
| `6904` | Input | Keyboard and mouse injection into the display (XTEST). |
| `6906` | Co-play | The input-arbiter state, which tracks whose turn it is to drive. |
| `6905` | Agent bridge | An HTTP/JSON server exposing the capsule's tools. Interactive capsules only. |
| `9000` | Readiness hook | `/ready`, loopback only (see above). |

A stream-only capsule needs a display, the readiness hook, and the video producer on `6903` (add audio
and input to make it interactive). An interactive capsule adds the input, co-play, and agent-bridge
ports.

## The manifest (capsule.yaml)

An interactive capsule declares its tools in a `capsule.yaml` manifest. The file has a single top-level
`capsule:` key, and the only allowed fields under it are:

| Field | Purpose |
|---|---|
| `id` | Stable capsule id (`^[a-z0-9][a-z0-9._-]{0,127}$`). It namespaces the capsule's tools, so a tool `observe` is exposed as `<id>__observe`. |
| `name`, `description` | Shown by `list_capsules`. |
| `interaction` | The tier switch. `tier1: true` enables the universal input primitives; a capsule with no manifest is Tier 0. |
| `bridge` | The agent bridge's `{port, protocol}`. The protocol must be `http-json`; the default port is `6905`. |
| `lifecycle` | Optional `beforeFreeze` and `afterThaw` HTTP routes the platform calls around suspend and resume. |
| `runtime` | `minimumMemoryMiB`, plus fields that apply only to local Docker runs. |
| `experience` | Human-facing help text, suggested prompts, and display geometry. UI metadata only. |
| `tools` | The typed tools (Tier 2). Each has a bare-verb `name`, a bridge `path`, a `description`, an `inputSchema`, and risk metadata. |
| `permissions` | `iamRole`. The reference capsules use `none`; the VM gets no AWS credentials. |
| `safety` | `sensitivePatterns`, regexes screened at tool-call time. |

A tool whose `riskClass` is high, or whose `effects` reach outside the VM (destructive, credential,
purchase, or delete), **must** set `requiresApproval: true`, or the manifest is rejected. Approval means
the AI must obtain a single-use, human-confirmed token for that exact action before the platform runs
it.

Validate a manifest before you build:

```bash
substrate/validate-capsule-manifest.py capsules/<your-capsule>/capsule.yaml
```

The packaging step embeds the validated manifest into the build context as `capsule.manifest.json`, and
the capsule stack stages it into immutable SSM parameters that the MCP server reads at startup.

## Build the context

Put your capsule under `capsules/<your-capsule>/` with a `Dockerfile` at its root. Everything in the
directory becomes the build context, except entries you list in a `.contextignore` file. Note that
`.gitignore` does **not** exclude files from the context, so use `.contextignore` for build output and
large assets.

Because you build on the minimal Amazon Linux 2023 base, your Dockerfile installs the full stack. Using
the Workbench as the reference, an interactive capsule typically installs:

- **A display server:** an X server such as Xvnc, X libraries, a lightweight window manager, and dbus.
- **A video producer:** ffmpeg with `libx264`, capturing the X display and serving H.264 on `6903`.
- **An audio producer:** PulseAudio into ffmpeg with `libopus`, serving Opus on `6902`.
- **Input:** a service that injects keyboard and mouse events into the display with XTEST on `6904`, plus
  the co-play arbiter on `6906`.
- **The agent bridge:** an HTTP/JSON server on `6905` that implements each tool's `path` from the
  manifest. Interactive capsules only.
- **The readiness hook:** a server on `127.0.0.1:9000` that reports `/ready`.
- **A supervisor** as the container `CMD` that starts all of the above and flips `/ready` to `200` only
  after the stream and input are verified.

A stream-only capsule needs only the display, the video producer, and the readiness hook.

Tips that save a build:

- **Pin and verify third-party binaries.** Fetch them by version with a checksum. A missing or
  unreadable download makes the build fail early with no boot logs.
- **`EXPOSE` is documentation only.** The real contract is which ports your processes actually bind, not
  what you declare.
- **If your engine speaks gRPC, add an HTTP/JSON shim on the bridge port.** The bridge protocol is locked
  to `http-json`, because the MicroVM gateway does not proxy gRPC.
- **The VM has no AWS credentials.** Do not design a capsule that expects them. To move files in or out,
  use the platform's `persistent_storage` and workspace tools.

## Deploy the capsule

With the substrate already running, deploy your capsule as a cartridge:

```bash
substrate/deploy-capsule.sh <your-capsule-dir>
# for example:
substrate/deploy-capsule.sh agent-doom --name "Agent DOOM" --id agent-doom
substrate/deploy-capsule.sh computer-use-desktop --memory-mib 8192
```

The script packages the build context, uploads it, deploys the capsule stack (which builds and tags the
MicroVM image), stages the manifest, and waits for the release to commit. When it finishes, the capsule
is a distinct CloudFormation stack, and its image carries the discovery tags.

The running MCP server discovers the new capsule by tag within its cache interval, so it appears in
`list_capsules` on the next launch. There is one caveat for tools: the server snapshots each capsule's
tool registration at startup, so a **brand-new interactive capsule's tools appear in the tool list only
after the runtime restarts**. If you change an existing capsule's tools, use
`substrate/deploy-capsule-and-rebind.sh`, which deploys the capsule, waits for the release to commit,
and then does a configuration-preserving runtime bounce so the server re-registers the tools.

To remove a capsule, delete its stack:

```bash
aws cloudformation delete-stack --stack-name pairputer-capsule-<id>
```

The stack's reaper terminates any running MicroVMs on the image and deletes the image before the stack
tears down, so deletion never wedges.

## See also

- [Repository README](../README.md): deploy the substrate and connect a chat host.
- [docs/architecture.md](./architecture.md): how the substrate, capsule, and streaming planes fit
  together, including the tools the MCP server and capsule expose.
- [docs/1-click-advanced.md](./1-click-advanced.md): the `BundleReferenceCapsule` and capsule-image
  launch parameters.

## Easter egg: load the Agent DOOM cartridge

The repo ships a second, ready-made capsule you can drop into a running substrate: **Agent DOOM**. It is
a Tier 1 capsule that streams DOOM and adds an in-VM agent bridge, so the AI can play alongside you. It
is the reference for a game or single-application capsule, and it doubles as a working example of every
step on this page.

With the substrate deployed, add it as a cartridge:

```bash
substrate/deploy-capsule.sh agent-doom --name "Agent DOOM" --id agent-doom
```

This builds and tags the `agent-doom` MicroVM image and stages its manifest, exactly like any other
capsule. Because Agent DOOM declares tools, restart the MCP runtime once after the deploy so the server
registers them (or deploy with `substrate/deploy-capsule-and-rebind.sh agent-doom`, which does the
runtime bounce for you).

Open it from a fresh chat:

> Use the pairputer app to open Agent DOOM (play_capsule).

You drive with the keyboard and mouse in the live stream; the AI can also observe the game state and act
through the capsule's tools. To remove it, delete its stack:

```bash
aws cloudformation delete-stack --stack-name pairputer-capsule-agent-doom
```
