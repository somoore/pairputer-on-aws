# Deploy pairputer from source

This directory holds the pairputer substrate: the CloudFormation templates, the container images (MCP
server and streaming relay), and the scripts that build and deploy them. It is the **contributor path**
for people who change pairputer and deploy their own build.

If you only want to *use* pairputer, don't build from source. Launch the 1-click CloudFormation stack
from the [repository README](../README.md) instead - it deploys pairputer's signed public images with no
tools, no clone, and no Docker.

For how the deployed system works (architecture, planes, security model), see
[`../docs/architecture.md`](../docs/architecture.md) and [`../SECURITY.md`](../SECURITY.md).

## What `deploy.sh` does

`substrate/deploy.sh` runs the whole from-source deploy in one command:

1. Builds the ARM64 MCP and relay container images with Docker and pushes them to your private ECR,
   pinned by digest.
2. Packages the bundled capsule's MicroVM build context and uploads it to S3.
3. Packages the nested CloudFormation templates and deploys the root stack (Cognito, the AgentCore MCP
   control plane, the ECS/Fargate relay behind CloudFront and WAF, and the Pairputer Workbench MicroVM
   image, which builds in your account).
4. Creates your super-admin user and, unless you opt out, wires your local Codex config.

The result is the same stack the 1-click launch creates - the only difference is that the images come
from your source build instead of pairputer's signed public images.

## Before you begin

You need:

- A running **Docker** daemon (the build step needs it).
- **AWS credentials** on the standard chain (SSO, `AWS_PROFILE`, `~/.aws/*`, env keys, or a role).
  Nothing is hardcoded; set `AWS_PROFILE` or run `aws sso login` however you normally do.
- To deploy in **`us-east-1`**. AWS only allows CloudFront-scope WAF resources there, and the root stack
  nests that WAF in-region rather than shipping a hidden cross-region helper.

## Deploy

Run the script directly - not with `sh`, which is a POSIX shell that cannot parse this bash script:

```bash
# From the repo root. Default networking builds a dedicated VPC and a cheap fck-nat instance,
# so there are no VPC or DNS prerequisites.
PAIRPUTER_SUPER_ADMIN_EMAIL=you@example.com ./substrate/deploy.sh
```

When it finishes, it prints the stack outputs - including `McpEndpoint`, `ChatGPTClientId`,
`ClaudeClientId`, and `CognitoDomain` - and the one interactive login step for Codex.

## Connect a chat host

Connect ChatGPT and Claude with the guides in [`../docs/chatgpt.md`](../docs/chatgpt.md) and
[`../docs/claude.md`](../docs/claude.md). Each connector covers that product's web, desktop, and mobile
apps, and Codex rides the ChatGPT connector.

For Codex specifically, `deploy.sh` wires `~/.codex/config.toml` for you at the end of the run (it
upserts the `[mcp_servers.pairputer]` block, backing up the file first). Then run the one interactive
step, which opens a browser for the Cognito PKCE login and cannot be headless:

```bash
codex mcp login pairputer
```

Re-run the wiring any time the endpoint or client id changes, for example after a redeploy:

```bash
./substrate/wire-codex.sh          # queries the live stack and upserts config.toml
```

Opt out of the automatic Codex wiring with `PAIRPUTER_SKIP_CODEX_CONFIG=1` (for example, in CI, or when
the deploy account is not your Codex machine).

## Configuration

Every option has a default. The ones you are most likely to set:

```bash
PAIRPUTER_SUPER_ADMIN_EMAIL=you@example.com  # first admin; Cognito emails a temporary password
PAIRPUTER_IMAGE_SOURCE=Private               # Private (default): build from source. Public: use signed public images.
PAIRPUTER_NETWORKING_MODE=CreateVpcFckNat    # default; or CreateVpcNatGateway, or ExistingVpc
PAIRPUTER_RELAY_WARM_SECONDS=-1              # -1 always-warm (default); 0 scale-to-zero; N warm N seconds
PAIRPUTER_BUNDLE_REFERENCE_CAPSULE=true       # bundle the Workbench capsule (default); false for a bare substrate
PAIRPUTER_REFERENCE_CAPSULE=computer-use-desktop  # bundled capsule dir; agent-doom bundles DOOM instead
PAIRPUTER_DEBUG=false                        # true adds verbose capsule input logs and the relay /vmdbg route
```

For `ExistingVpc` networking, also set `PAIRPUTER_VPC_ID` and `PAIRPUTER_PRIVATE_SUBNET_IDS` (the subnets
must be private with a working egress path). `deploy.sh` resolves the VPC CIDR automatically. For the
default `CreateVpcFckNat` mode, `deploy.sh` resolves the current fck-nat ARM64 AMI for you.

The full cost breakdown for every resource this deploys is in
[`../docs/1-click-cost.md`](../docs/1-click-cost.md).

## Add a capsule cartridge

Capsules are separate CloudFormation stacks you deploy after the substrate. The Pairputer Workbench
bundles with the substrate by default; Agent DOOM is an optional cartridge:

```bash
./substrate/deploy-capsule.sh agent-doom
```

The running MCP server discovers the new capsule by image tag within its cache TTL, so no substrate
redeploy is needed. To remove a capsule, delete its stack:

```bash
aws cloudformation delete-stack --stack-name pairputer-capsule-agent-doom
```

After rebuilding a capsule image, each principal must trash its own MicroVM (the widget's Trash button
or the `trash_microvm` tool) before the next launch, or the launch resumes the old image. Per-tenant
mapping means your chat-host user and any test client have different VMs.

## Iterate on the images

- **MCP server and widget** (`mcp-server/`): rebuild with `build-and-push.sh`, then redeploy. Widget
  (`app.html`) changes usually need a chat-host restart, because hosts cache widget HTML aggressively.
- **Relay** (`stateful-relay/`): rebuild with `build-and-push-relay.sh`, then redeploy.
- **Capsule MicroVM image**: any change under `capsules/<capsule>/`, or flipping `PAIRPUTER_DEBUG` or
  `PAIRPUTER_INPUT_SELFTEST_ENFORCE`, rebuilds the image on the next deploy (both flags are baked into
  the image as environment variables).

The MicroVM image always builds in your account. `AWS::Lambda::MicrovmImage` can only build from an S3
context - it cannot import a prebuilt image, and `BaseImageArn` accepts only AWS-managed base images. A
build-time readiness gate runs an input self-test (it injects a key with XTEST and confirms delivery);
with `PAIRPUTER_INPUT_SELFTEST_ENFORCE=true` (the default), a build where input does not work fails
instead of shipping a capsule with silently-dead keyboard and mouse.

## Publish the 1-click launch bucket (maintainers)

`publish-launch.sh` publishes the templates and assets to the public launch bucket. CI runs it
automatically on every push to `main` (see [`../.github/workflows/publish-launch.yml`](../.github/workflows/publish-launch.yml)),
so you rarely run it by hand:

```bash
./substrate/publish-launch.sh
```

It runs `aws cloudformation package` (rewriting nested `TemplateURL`s to absolute launch-bucket URLs),
uploads the packaged templates plus the capsule context zip and brand assets, and attaches a bucket
policy that allows `s3:GetObject` only when the request comes through CloudFormation
(`aws:CalledVia = cloudformation.amazonaws.com`). Any account's CloudFormation can read the templates to
launch, while direct browser, `curl`, and anonymous reads get a `403`. The templates contain no
secrets; every secret is generated inside the deploying account at stack-create time.

That bucket policy is why the console **Review** page may not preview the template (it fetches the
template in your browser, which the policy blocks) - the launch itself still works. Use
`./substrate/publish-launch.sh --public-read` if you want world-readable templates and the browser
preview.

## Remove everything

```bash
./substrate/remove-cf.sh            # delete the stack and all nested stacks
./substrate/remove-cf.sh --all      # also remove the artifact bucket and ECR repos
```

Capsule cartridge stacks are deleted first; each one's reaper terminates leftover MicroVMs and deletes
its image, then the root stack tears down every nested stack in dependency order. `teardown.sh` is a thin
wrapper around `remove-cf.sh`.

## Files

```
cloudformation/
  pairputer.yaml               root stack: parameters, validation rules, nested-stack orchestration
  nested/identity.yaml         Cognito pool, hosted UI, host + M2M clients, regional Cognito WAF, invite email
  nested/security.yaml         relay HMAC secret, ALB origin-header secret, CloudFront signing key + key group
  nested/sessions.yaml         DynamoDB per-tenant MicroVM session table (TTL + relay-active index)
  nested/relay-network.yaml    dedicated VPC, subnets, and egress NAT (fck-nat or NAT Gateway)
  nested/relay.yaml            ECS cluster/service, Fargate task, internal ALB, CloudFront, security groups
  nested/agentcore.yaml        AgentCore MCP runtime + least-privilege controller role
  nested/cloudfront-waf.yaml   CloudFront-scope WebACL (AWS managed rules + per-IP rate limit)
  nested/image-copy.yaml       Private mode: verify-and-copy the signed public images into your ECR
mcp-server/                    FastMCP server (server.py), widget host (app.html), host profiles, Dockerfile
stateful-relay/                Node relay: player HTML, video/audio SSE, POST /input, JWE cache (index.mjs)
lib/aws-env.sh                 shared AWS credential-chain and region resolution (sourced by the scripts)
build-and-push.sh              build and push the ARM64 MCP image, print a digest-pinned URI
build-and-push-relay.sh        build and push the ARM64 relay image
package-capsule-image.sh       package and upload a capsule's MicroVM build context to S3
deploy.sh                      build images, deploy the WAF, package templates, deploy the root stack, wire Codex
deploy-capsule.sh              deploy one capsule cartridge stack (build image, stage manifest, publish release)
deploy-capsule-and-rebind.sh   deploy capsule(s) then bounce the MCP runtime so it re-binds to the new release
wire-codex.sh                  upsert the stack's endpoint and client id into ~/.codex/config.toml
wire-chatgpt.sh                register a ChatGPT connector's callback URL on Cognito
wire-claude.sh                 verify the Claude auth discovery chain and print the connector values
publish-launch.sh              publish templates and assets to the public launch bucket
remove-cf.sh                   delete the stack and all nested stacks (optionally the artifact bucket and ECR)
teardown.sh                    thin wrapper around remove-cf.sh
local-dev.sh                   run a capsule locally in Docker for a fast iteration loop
```
