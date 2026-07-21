# 1-click deployment: resources, IAM, and cost

This page lists every AWS resource the 1-click CloudFormation launch creates in your account, every
IAM role and what it can touch, and daily, weekly, and monthly cost estimates. Each resource links to
the exact CloudFormation code that creates it.

In short: the always-on substrate idles at roughly **$1.90/day, $13/week, or $55-60/month**. Capsules
bill only while running - the Workbench adds about **$0.60 per active hour** (compute plus streaming)
and about **$0 while frozen**. Scale-to-zero mode drops the idle baseline to about **$40-45/month**.

---

## Before you read this page

- **Region & date:** all prices are `us-east-1`, on-demand, as of **July 2026**. Verify current numbers
  with the [AWS Pricing Calculator](https://calculator.aws/) - AWS changes prices, this page doesn't
  auto-update.
- **Source links** point at the exact template block on `main`. Line numbers drift as files evolve; the
  resource names don't, so search the file for the logical id if a link lands slightly off.
- **Verify the images first:** [`scripts/verify-images.sh`](../scripts/verify-images.sh) proves the
  container digests the template pins were signed by pairputer CI with SLSA provenance - offline,
  fail-closed.

---

## The stack map

One root stack, up to seven nested stacks (two are conditional):

| Nested stack | What it is | Created when |
|---|---|---|
| [`IdentityStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml) | Cognito OAuth (user pool, 4 app clients, hosted domain, your admin user) | always |
| [`SecurityStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/security.yaml) | Secrets + CloudFront signing key pair | always |
| [`SessionsStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/sessions.yaml) | DynamoDB session table | always |
| [`RelayNetworkStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay-network.yaml) | Dedicated VPC + egress NAT | always |
| [`RelayStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml) | ECS/Fargate streaming relay + internal ALB + CloudFront | always |
| [`CapsuleImageStack`](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml) | Builds + registers the Pairputer Workbench MicroVM image | `BundleReferenceCapsule=true` (default) |
| [`CloudFrontWafStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/cloudfront-waf.yaml) | CLOUDFRONT-scope WAF on the streaming front door | `us-east-1` + `EnableCloudFrontWaf=true` (default) |
| [`ImageCopyStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/image-copy.yaml) | ECR repos + CodeBuild that verify-and-copy our signed images into your private ECR | **Private image mode only** - not created by the default 1-click |
| [`AgentCoreStack`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/agentcore.yaml) | The MCP control plane on Bedrock AgentCore | always |

---

## Complete resource inventory

### Root stack: [`pairputer.yaml`](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/pairputer.yaml)

| Resource | Type | Purpose | Source |
|---|---|---|---|
| 7 nested stacks | `AWS::CloudFormation::Stack` | listed above | [pairputer.yaml](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/pairputer.yaml#L440) |
| `FckNatAmiResolver` | Lambda + `Custom::` | resolves the current fck-nat ARM64 AMI for the region at deploy time | [#L497](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/pairputer.yaml#L497) |
| `VpcCidrResolver` | Lambda + `Custom::` | resolves your VPC's CIDR in `ExistingVpc` mode | [#L551](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/pairputer.yaml#L551) |
| `CapsuleImageOverrideValidation` | Lambda + `Custom::` | validates a prebuilt image ARN override (only if you use one) | [#L607](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/pairputer.yaml#L607) |

Deploy-time helper Lambdas run for seconds during stack create/update - their runtime cost rounds to $0.

### IdentityStack: Cognito

| Resource | Type | Purpose | Source |
|---|---|---|---|
| User pool | `AWS::Cognito::UserPool` | your users; MFA-capable; branded invite email | [identity.yaml#L31](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml#L31) |
| Resource server | `AWS::Cognito::UserPoolResourceServer` | the `pairputer-mcp/invoke` OAuth scope | [#L100](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml#L100) |
| `SuperAdmins` group | `AWS::Cognito::UserPoolGroup` | admin group | [#L110](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml#L110) |
| Your admin user + group attachment | `AWS::Cognito::UserPoolUser` (+`…ToGroupAttachment`) | created from the email you enter; Cognito sends the invite | [#L124](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml#L124) |
| Hosted domain | `AWS::Cognito::UserPoolDomain` | `pairputer-<your-account-id>.auth.us-east-1.amazoncognito.com` | [#L147](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml#L147) |
| Cognito WAF + association | `AWS::WAFv2::WebACL` (REGIONAL) | brute-force / abuse protection on the login endpoints | [#L154](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml#L154) |
| 4 app clients | `AWS::Cognito::UserPoolClient` | Codex, ChatGPT, Claude (authorization-code) + one machine-to-machine client | [#L210](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/identity.yaml#L210) |

### SecurityStack: secrets and signing

| Resource | Type | Purpose | Source |
|---|---|---|---|
| 3 secrets | `AWS::SecretsManager::Secret` | relay session HMAC, relay origin header, CloudFront signing private key | [security.yaml#L7](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/security.yaml#L7) |
| Signing-key generator | Lambda + `Custom::` | generates the RSA key pair **inside your account** - nothing pairputer-side ever sees it | [#L53](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/security.yaml#L53) |
| CloudFront public key + key group | `AWS::CloudFront::PublicKey` / `KeyGroup` | the mandatory signed-URL gate on the streaming distribution | [#L72](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/security.yaml#L72) |

### SessionsStack: state

| Resource | Type | Purpose | Source |
|---|---|---|---|
| Session table | `AWS::DynamoDB::Table` (on-demand) | per-tenant capsule sessions, leases, relay-activity index | [sessions.yaml#L7](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/sessions.yaml#L7) |

### RelayNetworkStack: networking (default `CreateVpcFckNat` mode)

| Resource | Type | Purpose | Source |
|---|---|---|---|
| VPC + IGW + 4 subnets + route tables | `AWS::EC2::*` | dedicated VPC (2 public + 2 private subnets) | [relay-network.yaml#L48](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay-network.yaml#L48) |
| fck-nat instance (`t4g.nano`) + ENI + EIP + SG + role | `AWS::EC2::Instance` and related resources | private-subnet egress for ~$3/mo instead of a $32/mo NAT Gateway | [#L215](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay-network.yaml#L215) |
| NAT Gateway + EIP | `AWS::EC2::NatGateway` | **only** in `CreateVpcNatGateway` mode (not the default) | [#L144](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay-network.yaml#L144) |

### RelayStack: the streaming data plane

| Resource | Type | Purpose | Source |
|---|---|---|---|
| ECS cluster + service + task definition | `AWS::ECS::*` | the stateful relay: **1 × Fargate task, ARM64, 0.5 vCPU / 1 GB** | [relay.yaml#L237](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L237) |
| Auto-scaling target + 2 policies | `AWS::ApplicationAutoScaling::*` | scales relay tasks with active sessions (max `RelayMaxCount`) | [#L306](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L306) |
| Internal ALB + target group + listener + rule | `AWS::ElasticLoadBalancingV2::*` | private front door for the relay (CloudFront VPC origin requirement) | [#L177](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L177) |
| CloudFront distribution + VPC origin | `AWS::CloudFront::Distribution` | the public streaming endpoint; **signed URLs required** (key group is mandatory) | [#L456](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L456) |
| 2 security groups + origin-SG custom resource | `AWS::EC2::SecurityGroup` | ALB accepts only CloudFront's managed prefix list; tasks accept only the ALB | [#L151](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L151) |
| Relay log group (14-day retention) | `AWS::Logs::LogGroup` | relay + shipped capsule-runtime logs | [#L134](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L134) |

### CloudFrontWafStack

| Resource | Type | Purpose | Source |
|---|---|---|---|
| WebACL (CLOUDFRONT scope) | `AWS::WAFv2::WebACL` | AWS managed rule groups + a per-IP rate ceiling in front of the streaming distribution | [cloudfront-waf.yaml#L15](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/cloudfront-waf.yaml#L15) |

### CapsuleImageStack: the bundled Pairputer Workbench

| Resource | Type | Purpose | Source |
|---|---|---|---|
| Workbench MicroVM image (`pairputer-workbench`, 8 GB min) | `AWS::Lambda::MicrovmImage` | built **in your account** from the public, integrity-hashed build context | [capsule-stack.yaml#L320](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L320) |
| Manifest stager | Lambda + `Custom::` | reads `capsule.manifest.json` from the context zip, stages it into immutable chunked SSM parameters | [#L215](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L215) |
| Release publisher | Lambda + `Custom::` | atomically commits the immutable release record + `/current` pointer in SSM | [#L426](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L426) |
| MicroVM reaper | Lambda + `Custom::` | on stack delete: terminates VMs and deletes the image so teardown never wedges | [#L570](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L570) |
| Image build log group (14-day) | `AWS::Logs::LogGroup` | MicroVM image build logs | [#L140](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L140) |
| SSM parameters (created at runtime) | SSM Parameter Store | capsule manifest chunks + immutable releases + `/current` pointer under `/pairputer/capsules/…` | [#L215](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L215) |

### AgentCoreStack: the MCP control plane

| Resource | Type | Purpose | Source |
|---|---|---|---|
| AgentCore runtime | `AWS::BedrockAgentCore::Runtime` (or API-backed custom resource in Public image mode) | the MCP server every chat host connects to | [agentcore.yaml#L267](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/agentcore.yaml#L267) |
| Runtime manager | Lambda + `Custom::` | drives AgentCore create/update for public-ECR images (the native CFN resource only accepts private ECR) | [#L369](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/agentcore.yaml#L369) |
| Callback registrar | Lambda + custom resource | registers the exact OAuth callback URL on the Cognito clients | [#L453](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/agentcore.yaml#L453) |

---

## IAM: every role and what it can touch

Summaries below; **the linked policy blocks are authoritative**. Design rule throughout: each role is
single-purpose, and anything touching MicroVMs is **tag-scoped to `pairputer:capsule=true` images** - none of these roles can touch MicroVM images created outside pairputer.

| Role | Used by | Permissions (summary) | Source |
|---|---|---|---|
| `ControllerRole` | the MCP runtime | Run/Get/Suspend/Resume/Terminate/auth-token **on pairputer-tagged MicroVM images only** (tag condition); tag-based capsule discovery; read `/pairputer/capsules/*` SSM; session-table CRUD; read the relay + signing secrets | [agentcore.yaml#L91](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/agentcore.yaml#L91) |
| `CustomRuntimeRole` | runtime-manager Lambda | `bedrock-agentcore-control` create/update/delete of **this stack's** runtime; logs | [#L321](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/agentcore.yaml#L321) |
| `CallbackRegistrarRole` | callback Lambda | update **this pool's** Cognito app-client callback URLs; logs | [#L436](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/agentcore.yaml#L436) |
| `RelayExecutionRole` | ECS agent | pull the relay image, write container logs (standard ECS execution) | [relay.yaml#L59](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L59) |
| `RelayTaskRole` | the relay container | session-table read/write; MicroVM data-plane auth tokens **tag-scoped**; ship capsule runtime logs; read relay secrets | [#L71](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L71) |
| `FckNatRole` + instance profile | fck-nat EC2 | the standard fck-nat ENI attach/route management | [relay-network.yaml#L179](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay-network.yaml#L179) |
| `AlbFromCloudFrontOriginSgRole` | deploy-time Lambda | maintain the SG rule pinning the ALB to CloudFront's origin prefix list | [relay.yaml#L368](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/relay.yaml#L368) |
| `SigningKeyCustomResourceRole` | deploy-time Lambda | create/rotate the CloudFront signing secret **in your Secrets Manager** | [security.yaml#L30](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/nested/security.yaml#L30) |
| `MicrovmImageBuildRole` | AWS image builder | `s3:GetObject` on the build-context bucket; build logs | [capsule-stack.yaml#L146](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L146) |
| `ManifestStagerRole` | stager Lambda | `s3:GetObject` on the context zip; SSM get/put **scoped to `/pairputer/capsules/<this-capsule>/*`** | [#L188](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L188) |
| `CapsuleReleasePublisherRole` | publisher Lambda | SSM get/put **scoped to `/pairputer/capsules/<this-capsule>/*`** | [#L402](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L402) |
| `MicrovmReaperRole` | teardown Lambda | list/terminate MicroVMs + delete image **scoped to this stack's image ARN** | [#L539](https://github.com/somoore/pairputer/blob/main/capsules/nested/capsule-stack.yaml#L539) |
| Root-stack resolver/validator roles | deploy-time Lambdas | read-only describe calls (AMI lookup, VPC CIDR, image-state validation); logs | [pairputer.yaml#L472](https://github.com/somoore/pairputer/blob/main/substrate/cloudformation/pairputer.yaml#L472) |

**What is *not* here:** no static access keys anywhere; no role assumable from outside your account; the
MicroVM itself runs with `iamRole: none` - the capsule VM has **zero** AWS API access. Full model:
[`SECURITY.md`](../SECURITY.md).

---

## Cost model

### Assumptions (read these)

- `us-east-1`, on-demand, July 2026 public pricing. Estimates, not quotes.
- **MicroVM pricing** is modeled on AWS Lambda duration pricing (**$0.0000166667 per GB-second**),
  since `AWS::Lambda::MicrovmImage` VMs bill Lambda-style for the memory you provision, only while the
  VM is running. **A Frozen (suspended) capsule ≈ $0 compute.** Confirm the current MicroVM rate on the
  AWS Lambda pricing page before budgeting real money on it.
- Demo-scale traffic (a handful of users). At real multi-tenant load, the usage-based lines
  (CloudFront, WAF requests, DynamoDB, Fargate autoscaling) grow with use.

### The always-on substrate (default `RelayWarmSeconds=-1`)

These run 24/7 whether or not anyone is connected:

| Component | Sizing | $/day | $/week | $/month |
|---|---|---:|---:|---:|
| Fargate relay task (ARM64, 0.5 vCPU / 1 GB) | 1 always-warm task | $0.47 | $3.32 | $14.42 |
| Internal ALB | 1 ALB (+minimal LCU) | $0.54 | $3.78 | $16.43 |
| fck-nat egress (`t4g.nano` + 8 GB EBS) | 1 instance | $0.12 | $0.86 | $3.71 |
| WAF (CloudFront ACL + Cognito ACL, managed rules) | 2 WebACLs | $0.60 | $4.20 | ~$18 |
| Secrets Manager | 3 secrets | $0.04 | $0.28 | $1.20 |
| DynamoDB (on-demand), S3, SSM, CloudWatch (14-day retention) | demo scale | ~$0.07 | ~$0.50 | ~$2 |
| Bedrock AgentCore runtime | consumption-billed, mostly idle pings | ~$0.06 | ~$0.45 | ~$2 |
| Cognito | < 10k MAU | $0 | $0 | $0 |
| **Substrate total (idle)** | | **≈ $1.90** | **≈ $13.40** | **≈ $55-60** |

**Cheaper idle options:**

| Change | New monthly baseline | Tradeoff |
|---|---:|---|
| `RelayWarmSeconds=0` (relay scales to zero when idle) | **≈ $40-45** | a cold-start pause on the next connect |
| `CreateVpcNatGateway` instead of fck-nat | +$33 + $0.045/GB | managed NAT, no EC2 instance to trust |
| `EnableCloudFrontWaf=false` and/or trimming the Cognito WAF | −$10-18 | you lose the abuse ceiling - not recommended |

### Capsules - you pay only while they run

Compute (at the modeled rate) + CloudFront streaming (~1-1.5 GB/hr of H.264 at $0.085/GB):

| Capsule | Memory | Compute $/hr | + streaming $/hr | ≈ total per **active** hour | Frozen |
|---|---|---:|---:|---:|---:|
| **Pairputer Workbench** (bundled) | 8 GB | $0.48 | ~$0.12 | **≈ $0.60** | ≈ $0 |
| Workbench 16 GB tier | 16 GB | $0.96 | ~$0.12 | ≈ $1.08 | ≈ $0 |
| Agent DOOM (optional cartridge) | 2 GB | $0.12 | ~$0.12 | ≈ $0.24 | ≈ $0 |

One-time / storage: the Workbench image build runs once at deploy (~15 min, negligible); the stored
MicroVM image adds roughly **$0.25-0.50/month**.

### What a month actually looks like

Substrate (always-on) + Workbench usage:

| Usage pattern | Capsule hours/mo | Capsule cost | **Total ≈ $/month** |
|---|---:|---:|---:|
| Kick the tires (30 min/day) | 15 | $9 | **≈ $65-70** |
| Daily pairing (2 h/day) | 60 | $36 | **≈ $90-95** |
| Heavy use (6 h/day) | 180 | $108 | **≈ $165-170** |
| Deployed but unused (scale-to-zero) | 0 | $0 | **≈ $40-45** |

### Cost controls built in

- **Freeze/Thaw** - the widget's ❄ Freeze suspends the VM (billing ≈ $0) and 🔥 Thaw resumes it in
  seconds. Idle auto-freeze policies are configurable per session.
- **Trash** - terminates the VM entirely; the durable per-tenant workspace survives in S3.
- **`RelayWarmSeconds`** - `-1` always-warm (instant resume) / `0` scale-to-zero / `N` seconds warm.
- **Tear it all down** - `substrate/remove-cf.sh` deletes every stack, terminates every MicroVM, and
  deletes the images. Nothing keeps billing.

---

*Estimates prepared July 2026. The templates are the source of truth for resources; the AWS Pricing
Calculator is the source of truth for prices. Found a drift? Open an issue.*
