# pairputer — production DOOM-in-Codex, in your own AWS account

Real DOOM running in an **AWS Lambda MicroVM**, streamed inline in an **OpenAI Codex** thread with
video, audio, keyboard, mouse, and a working **Freeze/Thaw** that suspends the MicroVM (and its
compute billing). Everything runs server-side in **your** AWS account — the only thing on your laptop
is the Codex app. No static AWS credentials are distributed anywhere.

- Control plane: **Bedrock AgentCore** hosts the MCP server (IAM execution role, no static keys).
- Auth: **Cognito OAuth** (authorization-code + PKCE; tokens live in the OS keyring).
- Data plane: a **stateful ECS/Fargate relay** behind **CloudFront + WAF + ALB**.
- Game: a **DOOM MicroVM** the relay reaches over a server-side auth token; the browser never sees it.

The full technical record — architecture diagram, control/data/lifecycle flows, security model, and the
debugging history — is in [`../CLAUDE.md`](../CLAUDE.md). This README is the deploy guide.

## Deploy it (recommended: the CLI)

One command builds the two container images, packages the DOOM MicroVM build context, deploys the
CloudFront-scope WAF, creates the whole nested stack in your account, and wires your Codex config:

```bash
# from the repo root — default networking builds a dedicated VPC + a cheap fck-nat instance,
# so there are NO VPC/DNS prerequisites.
PAIRPUTER_SUPER_ADMIN_EMAIL=you@example.com \
substrate/deploy.sh
```

Credentials use the standard AWS chain (SSO, `AWS_PROFILE`, `~/.aws/*`, env keys, roles) — set
`AWS_PROFILE`/run `aws sso login` however you normally do; nothing is hardcoded. Pick a region with
`PAIRPUTER_AWS_REGION`/`AWS_REGION`, and **use `us-east-1`** — AWS requires CloudFront-scope WAF
resources there, and the root stack nests that WAF in-region rather than shipping a hidden cross-region
helper.

**Relay networking (`PAIRPUTER_NETWORKING_MODE`).** The relay ALB is internal (CloudFront reaches it
privately through a VPC origin — no cert or DNS needed), which requires private subnets. Choose one:

- `CreateVpcFckNat` *(default)* — the stack builds a dedicated VPC + a fck-nat `t4g.nano` (~$3/mo). No
  prerequisites. `deploy.sh` resolves the public fck-nat AMI for you.
- `CreateVpcNatGateway` — dedicated VPC + a managed NAT Gateway (~$32/mo).
- `ExistingVpc` — bring your own: also set `PAIRPUTER_VPC_ID` and `PAIRPUTER_PRIVATE_SUBNET_IDS` (subnets
  must be private with a working egress path). `deploy.sh` resolves the VPC CIDR automatically.

The CLI is the recommended path because it builds the ARM64 container images (MCP + relay) locally with
Docker and pins them by digest — a one-click console launch can't run Docker.

## Or launch from the console (1-click)

The stack template is published, so you can create it straight from the CloudFormation console — no
clone, no local packaging:

[![Launch Stack](https://cdn.rawgit.com/buildkite/cloudformation-launch-stack-button-svg/master/launch-stack.svg)](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://pairputer-launch.s3.amazonaws.com/templates/pairputer.yaml&stackName=pairputer)

**Heads up — you still need the two container image URIs.** `ContainerUri` (MCP server) and
`RelayContainerUri` (relay) are required parameters and currently must be ARM64 images in **your own
account's ECR**, so today the console launch is a convenience for people who've already built and pushed
those images (`substrate/build-and-push.sh` + `substrate/build-and-push-relay.sh` print digest-pinned URIs to paste
in). A true zero-Docker launch needs those images in **public ECR** — that's a planned follow-up. If you
haven't built images, use the CLI path above, which builds and wires everything for you.

Everything else has a sensible default. The DOOM MicroVM image builds in your account (AWS only lets
MicroVM images build from a context, not import prebuilt) and the stack self-tests input before that
image goes live. After creation, wire Codex with `substrate/wire-codex.sh` (it reads the stack outputs).

Maintainers: publish/refresh the launch bucket + button with `substrate/publish-launch.sh` (see "Hosting the
1-click launch" below).

When it finishes it prints stack outputs including `McpEndpoint`, `CodexClientId`, `CognitoDomain`, and
CLI commands to create your first admin user. Point Codex at the `McpEndpoint` (see below).

### What gets created

- **Cognito** user pool + hosted UI (admin-created users only; a public PKCE client for Codex; an M2M
  client for smoke tests), plus a regional WAF on the login surface.
- **Secrets Manager**: the relay HMAC secret, the ALB origin-header secret, and a generated CloudFront
  signing key (private key never leaves Secrets Manager).
- **DynamoDB** session table mapping each Cognito principal to its own MicroVM.
- **AgentCore Runtime** running the FastMCP server — capsule-agnostic tools (`list_capsules`, `play_capsule`,
  `freeze`, `thaw`, `trash_microvm`, `pairputer_session`, `capsule_state`; `play_doom`/`doom_state`/`list_images`
  remain as deprecated aliases). `image_id` defaults to the sole/first deployed capsule. Tools return a clean
  status line to the chat (`[Capsule] — Running/Frozen/…`) with the full session payload delivered to the
  widget via structured content (never dumped as raw JSON). Capsules may advertise only a slim core of
  their tool catalog to keep per-turn context cost down — hidden tools stay callable via `capsule_invoke`
  and discoverable via `capsule_metadata` with identical gating (see
  [`docs/mcp-tool-efficiency.md`](../docs/mcp-tool-efficiency.md)). Least-privilege controller role.
- **DOOM MicroVM image** built in-account from the WAD-free context (see "Image build" below).
- **Private relay networking** (VPC + private subnets + egress NAT) per `NetworkingMode`.
- **Internal ECS/Fargate relay** + internal ALB + **CloudFront** (VPC origin) + CloudFront-scope WAF.

### Useful knobs

```bash
PAIRPUTER_NETWORKING_MODE=CreateVpcFckNat # default; or CreateVpcNatGateway, or ExistingVpc
PAIRPUTER_VPC_ID=vpc-...                 # ExistingVpc mode only
PAIRPUTER_PRIVATE_SUBNET_IDS=subnet-a,... # ExistingVpc mode only (private subnets w/ egress)
PAIRPUTER_SUPER_ADMIN_EMAIL=you@ex.com   # first admin; Cognito emails a temp password (no password in CFN)
PAIRPUTER_ADMIN_PASSWORD_PROMPT=1        # if the invite email doesn't arrive: set the admin password at a
                                       #   local hidden prompt instead (COGNITO_DEFAULT email is best-effort)
PAIRPUTER_CODEX_CALLBACK_URL=...         # see "Wire Codex" if you hit redirect_mismatch
PAIRPUTER_RELAY_WARM_SECONDS=-1          # shared multi-tenant relay is always on; scale-to-zero is fail-closed
PAIRPUTER_INPUT_SELFTEST_ENFORCE=true    # fail the image build if input can't be verified (recommended)
PAIRPUTER_DEBUG=false                    # true = verbose capsule input logs + relay /vmdbg readback
```

## Wire Codex

`deploy.sh` does this for you at the end: it reads `McpEndpoint` + `CodexClientId` from the stack and
**upserts** the `[mcp_servers.pairputer]` block into `~/.codex/config.toml` (backing the file up to
`config.toml.bak` first; a no-op if nothing changed). Then it prints the one interactive step:

```bash
codex mcp login pairputer      # browser PKCE login; token lands in the OS keyring
```

That's the only manual step — the login opens a browser for Cognito and can't be headless. In a Codex
thread, `play_capsule` (or the `play_doom` alias) then renders the inline widget and the game starts.

Re-run the wiring anytime the endpoint or client id changes (e.g. after a redeploy in a new region):

```bash
substrate/wire-codex.sh                   # standalone; queries the live stack and upserts config.toml
```

Opt out of the automatic wiring with `PAIRPUTER_SKIP_CODEX_CONFIG=1 substrate/deploy.sh` (e.g. in CI or when the
deploy account isn't your Codex machine). Codex talks to AgentCore over OAuth with a **static public
PKCE client** (no secret) because Cognito doesn't support Dynamic Client Registration; the stack creates
that client and the wiring uses it. The block it writes:

```toml
[mcp_servers.pairputer]
url = "<McpEndpoint>"
scopes = ["openid", "pairputer-mcp/invoke"]
[mcp_servers.pairputer.oauth]
client_id = "<CodexClientId>"      # public client, no secret
```

If login fails with Cognito `redirect_mismatch`, read the exact `redirect_uri` from the failed login URL,
redeploy with `PAIRPUTER_CODEX_CALLBACK_URL=<that exact URL>`, and run `codex mcp login pairputer`
again. If Codex later says the connection expired, run the same login command. (The callback URL is a
one-time bootstrap: Codex derives `http://localhost:5555/callback/<hash>` from the client, discoverable
only by attempting login once.)

## Insert a capsule cartridge

Capsules are separate CloudFormation stacks deployed AFTER the substrate ([`../docs/capsule-architecture.md`](../docs/capsule-architecture.md)).
The bundled DOOM capsule ships with the substrate; the agent-driven one is a cartridge:

```bash
substrate/deploy-capsule.sh agent-doom    # snapshot -> immutable manifest -> image -> atomic release pointer
```

The running MCP discovers it by tag within its cache TTL — no substrate redeploy. In a Codex thread:
`open agent-doom`, then either play, idle ~20s for the autopilot to take over, or say "fight demons".

Two operational notes:
- Capsule publication is release-atomic: the deploy stages an immutable, digest-addressed manifest; the
  capsule stack then publishes an immutable release pinning that manifest to one ACTIVE image version and
  advances `/pairputer/capsules/<id>/current` last. A failed build/check never advertises a partial release.
- **After any capsule image rebuild, each principal must `trash_microvm` its own VM** (widget Trash
  button) or its next `play_capsule` RESUMES the old image. Per-tenant mapping means your Codex user and
  any M2M test client have *different* VMs.
- Remove a capsule by deleting its stack: `aws cloudformation delete-stack --stack-name pairputer-capsule-agent-doom`.

## Image build (why it's build-in-account, and how it's kept correct)

`AWS::Lambda::MicrovmImage` can only be **built from an S3 context** — it cannot import a prebuilt image
from ECR, and `BaseImageArn` accepts only AWS-managed base images. So there is no "pull a golden DOOM
image" option; every deployer's stack builds the image in their own account from the WAD-free context
(`package-doom-image.sh` uploads it; the shareware `DOOM1.WAD` is fetched from a pinned URL and
SHA-256-verified during the AWS-managed build). The AgentCore/relay **container** images (which *can* be
shared) are built and pushed by `build-and-push.sh` / `build-and-push-relay.sh`.

Because in-account builds are non-deterministic in *timing*, the image's readiness gate runs an
**input self-test**: it injects a key via XTEST and confirms delivery to a control window before the
image is marked ready. With `InputSelftestEnforce=true` (default) a build where input doesn't work
**fails** instead of shipping — so a deployer never gets a MicroVM with silently-dead keyboard/mouse.
(This exists because an earlier startup race — `input_ws.py` connecting to Xvnc before it was ready and
then staying dead — produced exactly that symptom; see "Latest walls and fixes" #11 in `CLAUDE.md`.)

Supplying an external prebuilt MicroVM image ARN is intentionally gated: `deploy.sh` refuses
`PAIRPUTER_DOOM_IMAGE_ARN_OVERRIDE` unless `PAIRPUTER_ALLOW_EXTERNAL_DOOM_IMAGE=true`, and the root template
validates that the override has a latest active image version before AgentCore consumes it.

## Hosting the 1-click launch (maintainers)

`substrate/publish-launch.sh` publishes the templates to a public launch bucket and prints the console launch
URL + README button markdown:

```bash
substrate/publish-launch.sh          # bucket pairputer-launch-<account>, CloudFormation-only access
```

It runs `aws cloudformation package` (so the root's nested `TemplateURL`s become absolute URLs in the
launch bucket), uploads the packaged root + nested templates, and attaches a bucket policy that allows
`s3:GetObject` **only when the request is made via CloudFormation** (`aws:CalledVia =
cloudformation.amazonaws.com`). This lets any account's CloudFormation read the templates to launch
while denying direct browser/`curl`/anonymous reads — verified: anonymous GET returns `403`, and a real
`create-stack --template-url` (root + nested) succeeds. The templates contain **no secrets**; every
secret is generated inside the deploying account at stack-create time.

Trade-off: because non-CloudFormation reads are blocked, the console **Review** page (which fetches the
template in your browser to render the parameter form) may not preview it — the launch itself still
works. Use `substrate/publish-launch.sh --public-read` if you want world-readable templates and the browser
preview instead.

## Iterate

- **MCP server / widget** (`mcp-server/`): rebuild with `build-and-push.sh`, redeploy with the new
  `ContainerUri`. Widget (`app.html`) changes usually need a Codex app restart — Codex caches widget
  HTML aggressively.
- **Relay** (`stateful-relay/`): rebuild with `build-and-push-relay.sh`, redeploy with the new
  `RelayContainerUri`.
- **MicroVM image** (`microvm-image/`): any change here, or flipping `PAIRPUTER_DEBUG` /
  `PAIRPUTER_INPUT_SELFTEST_ENFORCE`, rebuilds the image on the next `deploy.sh` (both flags are baked in
  as image env vars).

`substrate/teardown.sh` deletes the stack (add `--delete-ecr` to also remove the ECR repos). Terminate any
running MicroVM first via the widget's Trash button or the `trash_microvm` MCP tool.

## Files

```
cloudformation/
  pairputer.yaml              root stack: params, validation Rules, nested-stack orchestration
  nested/security.yaml         relay HMAC secret, origin-header secret, CloudFront signing key + key group
  nested/identity.yaml         Cognito pool, hosted UI, PKCE + M2M clients, regional Cognito WAF
  nested/sessions.yaml         DynamoDB per-user MicroVM session table (TTL + relay-active GSIs)
  nested/relay.yaml            ECS cluster/service, Fargate task, ALB, CloudFront, security groups
  nested/agentcore.yaml        AgentCore runtime + least-priv controller role
  nested/microvm-image.yaml    AWS::Lambda::MicrovmImage build from the S3 context
  nested/cloudfront-waf.yaml   CloudFront-scope WebACL (managed rules + per-IP rate limit)
mcp-server/                    FastMCP server (server.py), widget host (app.html), Dockerfile
stateful-relay/                Node relay: player HTML, video/audio SSE, POST /input, JWE cache (index.mjs)
microvm-image/                 WAD-free DOOM capsule: Dockerfile + rootfs/opt/capsule/*
video-relay/                   earlier Lambda response-streaming relay (fallback/history; not deployed)
lib/aws-env.sh                 shared AWS credential-chain + region resolution (sourced by the scripts)
build-and-push.sh              build/push the ARM64 MCP image, print a digest-pinned URI
build-and-push-relay.sh        build/push the ARM64 relay image
package-doom-image.sh          package + upload the WAD-free DOOM build context to S3
deploy.sh                      build missing images, deploy WAF, package nested templates, deploy root, wire Codex
wire-codex.sh                  upsert the stack's endpoint + client id into ~/.codex/config.toml
publish-launch.sh              publish templates to a public launch bucket + print the 1-click launch URL
remove-cf.sh                   delete the stack + all nested stacks (optionally artifact bucket + ECR)
teardown.sh                    thin wrapper around remove-cf.sh
```
