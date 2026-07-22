# 1-click launch parameters

The 1-click CloudFormation launch has one required input, your email address, and everything else
defaults to a working deployment: pairputer's signed public images and the bundled Pairputer Workbench
capsule. This page documents every parameter you can change, what it does, and its options.

You do not need to read this to deploy. Launch with just your email from the
[repository README](../README.md), and come back here only when you want to customize something.

The parameters below are grouped the same way the CloudFormation console groups them, so the section
headings match the form you see.

## Start here

These are the only two parameters most people touch.

### SuperAdminEmail

- **Type:** string. **Default:** empty.
- The email address for your first super-admin. Cognito sends a one-time temporary password to this
  address, and you set your own permanent password on first login. No password is ever stored in the
  template.
- Leave it blank to skip auto-creating the admin. You then create one yourself with the
  `CreateSuperAdmin*` commands that appear in the stack outputs.

### ImageSource

- **Type:** string. **Default:** `Public`. **Values:** `Public`, `Private`.
- `Public`: deploy pairputer's signed, digest-pinned public-ECR images. Nothing to build, no Docker.
- `Private`: run the images from your own account's private ECR. See
  [Container images](#container-images) for the two ways to do this.

## Container images

These apply only when `ImageSource` is `Private`. In `Public` mode they are ignored, and the two
`ContainerUri` defaults already point at the correct signed public images.

### PrivateMcpContainerUri

- **Type:** string. **Default:** empty.
- Your MCP server image in private ECR (`<account>.dkr.ecr...`, by `@sha256:` digest or `:tag`).
- Leave it blank to have an in-stack CodeBuild job cosign-verify pairputer's signed public MCP image and
  copy it into your private ECR.

### PrivateRelayContainerUri

- **Type:** string. **Default:** empty.
- Your streaming-relay image in private ECR. Leave it blank to verify-and-copy pairputer's signed public
  relay image the same way.

### ContainerUri

- **Type:** string. **Default:** the signed public MCP image, pinned by digest.
- The MCP image used in `Public` mode. Change it only to pin a different published digest. Verify any
  value out of band with `scripts/verify-images.sh`.

### RelayContainerUri

- **Type:** string. **Default:** the signed public relay image, pinned by digest.
- The relay image used in `Public` mode. Same guidance as `ContainerUri`.

## Network

These control how the streaming relay gets the private subnets its internal load balancer needs.
CloudFront VPC origins require the load balancer to be internal, so the relay always runs in private
subnets with an egress path.

### NetworkingMode

- **Type:** string. **Default:** `CreateVpcFckNat`. **Values:** `CreateVpcFckNat`,
  `CreateVpcNatGateway`, `ExistingVpc`.
- `CreateVpcFckNat`: create a dedicated VPC with a small fck-nat instance for egress (about $3 per
  month). This is the default.
- `CreateVpcNatGateway`: create a dedicated VPC with a managed NAT Gateway (about $32 per month fixed,
  plus data-processing charges).
- `ExistingVpc`: deploy into a VPC you already have. You must also set `VpcId` and `PrivateSubnetIds`,
  and those subnets must already have working egress.

### VpcId

- **Type:** string. **Default:** empty.
- `ExistingVpc` mode only. The VPC to deploy the relay into.

### PrivateSubnetIds

- **Type:** comma-delimited list. **Default:** empty.
- `ExistingVpc` mode only. The private subnets, with egress, for the internal load balancer and the
  Fargate tasks.

### VpcCidr

- **Type:** string. **Default:** empty.
- `ExistingVpc` mode only. Leave it blank; the stack resolves your VPC's CIDR automatically. Set it (for
  example `10.0.0.0/16`) only as an advanced override.

### NewVpcCidr

- **Type:** string. **Default:** `10.71.0.0/16`.
- Used by the `Create*` modes only. The CIDR for the dedicated VPC the stack creates. Change it if the
  default range collides with a network you plan to peer with.

### FckNatAmiId

- **Type:** string. **Default:** empty.
- `CreateVpcFckNat` mode only. Leave it blank; the stack resolves the current fck-nat ARM64 AMI for your
  region at deploy time. Set it only to pin a specific AMI as an advanced override.

## Bundled capsule (Pairputer Workbench)

These control the capsule that ships with the substrate.

### BundleReferenceCapsule

- **Type:** string. **Default:** `true`. **Values:** `true`, `false`.
- `true`: bundle the Pairputer Workbench, so the deployment is useful out of the box. The stack builds
  the MicroVM image and registers it with the MCP server.
- `false`: deploy the bare substrate with no capsule. Capsule tools report "no capsules deployed" until
  you register one with `deploy-capsule.sh <capsule-dir>`.

### CapsuleImageArnOverride

- **Type:** string. **Default:** empty.
- An ARN for a prebuilt capsule MicroVM image. Leave it empty to let the stack build the image. To use
  this, you must also set `AllowCapsuleImageArnOverride` to `true`.

### AllowCapsuleImageArnOverride

- **Type:** string. **Default:** `false`. **Values:** `true`, `false`.
- The explicit opt-in required before `CapsuleImageArnOverride` can bypass the stack-managed image build.

### CapsuleCodeArtifactUri

- **Type:** string. **Default:** the public Pairputer Workbench build context.
- The S3 URI of the capsule's MicroVM build-context zip, used only when the stack builds the image.
  Required unless `CapsuleImageArnOverride` is set.

### CapsuleCodeArtifactBucket

- **Type:** string. **Default:** `pairputer-launch`.
- The S3 bucket that holds `CapsuleCodeArtifactUri`. Required unless `CapsuleImageArnOverride` is set.

### CapsuleImageName

- **Type:** string. **Default:** `pairputer-workbench`.
- The account-local name for the Lambda MicroVM image the stack creates.

### CapsuleBaseImageArn

- **Type:** string. **Default:** empty.
- An optional managed base-image ARN. Leave it empty to use the regional `al2023-1` base image.

### CapsuleBaseImageVersion

- **Type:** string. **Default:** `0`.
- The managed base-image version used by the MicroVM image resource.

### CapsuleImageMinimumMemoryMiB

- **Type:** number. **Default:** `8192`. **Range:** 2048 to 32768.
- The minimum memory for each capsule MicroVM. The Workbench needs 8192. Lower it only for a smaller
  capsule that you know fits in less.

## Runtime

These tune the MCP runtime, OAuth, and the relay's warm behavior.

### RuntimeName

- **Type:** string. **Default:** `pairputer_mcp_stateful`.
- The AgentCore runtime name. Must match `[a-zA-Z][a-zA-Z0-9_]{0,47}`.

### CodexCallbackUrl

- **Type:** string. **Default:** `http://localhost:5555/callback`.
- The exact OAuth callback URL that Codex uses. Change it only if your Codex client is configured with a
  different callback.

### CognitoDomainPrefix

- **Type:** string. **Default:** empty.
- The Cognito hosted-UI domain prefix. Cognito domains are globally unique across all AWS accounts, so
  leave it blank and the stack derives a unique prefix from your account id (`pairputer-<account-id>`).
  Set it only if you want a custom prefix and know it is free.

### RelayWarmSeconds

- **Type:** number. **Default:** `-1`.
- The relay's warm policy:
  - `-1`: always on. Fargate stays at one task for instant resume. This is the multi-tenant-safe
    default.
  - `0`: scale the relay to zero when it is genuinely idle (about $15 per month down to about $0 at
    idle, at the cost of a cold start on the next connect).
  - `N` greater than 0: stay warm for `N` seconds after the last session, then scale down.
- Scale-to-zero fires only on a successful read of exactly zero active sessions. A stale or failed read
  leaves the relay warm, so a live session is never killed.

### PairputerDebug

- **Type:** string. **Default:** `false`. **Values:** `true`, `false`.
- Enables verbose capsule input diagnostics and the relay's `/vmdbg` log-readback route. Leave it
  `false` in production.

### InputSelftestEnforce

- **Type:** string. **Default:** `true`. **Values:** `true`, `false`.
- When `true`, the capsule image build fails if its input self-test does not pass, so a build with
  broken keyboard or mouse never ships. Leave it `true` unless you are debugging the build itself.

## Security

These control the edge web application firewall (WAF) in front of the streaming distribution.

### EnableCloudFrontWaf

- **Type:** string. **Default:** `true`. **Values:** `true`, `false`.
- Creates and attaches the nested CloudFront-scope WAF when you deploy in `us-east-1` and `WebAclArn` is
  empty. CloudFront-scope WAF resources can only be created in `us-east-1`.

### WebAclArn

- **Type:** string. **Default:** empty.
- An existing CloudFront-scoped WAFv2 WebACL ARN to use instead of the nested one. Leave it empty to
  create the nested WAF.

### CloudFrontWafRateLimitPerFiveMinutes

- **Type:** number. **Default:** `2000000`. **Range:** 10000 to 2000000000.
- A coarse per-source-IP request ceiling over five minutes. Per-session input limits are enforced
  separately in the relay, so this ceiling should be high enough to tolerate shared NATs.

## Scaling and durable storage

These parameters are not in a named console section. They control relay scaling, durable workspace
storage, and the capsule capability manifest.

### RelayMaxCount

- **Type:** number. **Default:** `1000`. **Range:** 1 to 1000.
- The maximum number of ECS relay tasks for horizontal scaling of concurrent desktop streams.

### TenantStorageBucket

- **Type:** string. **Default:** empty.
- An S3 bucket for durable per-tenant workspace storage, which backs the Workbench's
  `workspace/persistent/` folder. The control plane mirrors that folder to
  `tenant-storage/<tenantId>/<imageId>/` at freeze and trash, and restores it into fresh VMs. The
  MicroVM never receives credentials.
- Empty disables the feature, so `persistent/` does not survive a trashed VM. Set it to a bucket name to
  turn durable storage on.

### CapsuleManifestJson

- **Type:** string. **Default:** empty.
- An optional capability manifest for the bundled capsule (its `capsule.yaml` as JSON). When set, the
  MCP server registers the agent-interaction tools the capsule declares, so the AI can observe and act
  in the live capsule alongside the human.
- Leave it empty for a stream-only capsule.

## Reference capsule identity

These name the bundled capsule in the registry. The defaults describe the Pairputer Workbench, and you
rarely change them by hand.

### ReferenceCapsuleId

- **Type:** string. **Default:** `computer-use-desktop`.
- The registry id for the bundled capsule, which becomes the MCP server's default capsule.

### ReferenceCapsuleName

- **Type:** string. **Default:** `Pairputer Workbench`.
- The display name for the bundled capsule. It becomes a MicroVM image tag, so only letters, digits,
  spaces, and `_.:/=+-@` are allowed. Commas are not.

### ReferenceCapsuleDescription

- **Type:** string. **Default:** a one-line Workbench description.
- A one-line description shown by `list_capsules`. It becomes a MicroVM image tag, so the same character
  rules as `ReferenceCapsuleName` apply.

## Admin creation

### CreateSuperAdminUser

- **Type:** string. **Default:** `true`. **Values:** `true`, `false`.
- When `true` and `SuperAdminEmail` is set, the stack creates the super-admin user and Cognito emails
  the branded temporary-password invite, so a console 1-click deploy is ready to log in with no
  command-line step.
- Set it to `false` to skip in-stack creation and create the admin yourself with the stack-output
  commands.

## See also

- [Repository README](../README.md): the 1-click launch button and quick start.
- [docs/1-click-cost.md](./1-click-cost.md): every resource the stack creates and its cost.
- [docs/architecture.md](./architecture.md): how the planes fit together.
- [SECURITY.md](../SECURITY.md): the supply-chain and trust model, including the two image modes.
