# pairputer supply-chain security

pairputer is deployed by strangers into their own AWS accounts. The moment the deploy path stops being
"you build it yourself" and becomes "pull artifacts we published," **supply-chain integrity becomes the
platform's core trust guarantee.** This doc is the end-to-end spec: every artifact a deployer trusts, and
exactly how it is verified — with a bias toward *provable in the deployer's own account*, not "trust us."

Design decisions (locked):
- **cosign keyless (Sigstore/OIDC)** signing — no private key to guard; provenance bound to the CI identity.
- **Digest-pin everything.** One `@sha256:` digest threads through sign → publish → deploy. No tags on the
  trust path. In the deployer's stack, **the digest pin *is* the deploy-time integrity guarantee**: a
  `@sha256:` reference is immutable and content-addressed, so what the template names cannot be swapped.
- **Out-of-band verification, not a forced in-stack gate.** `scripts/verify-images.sh` lets anyone (the
  deployer, their security team, an auditor) independently confirm — offline, against a pinned Sigstore
  root — that the pinned digests are cosign-signed by this repo's CI **and** carry a SLSA provenance
  attestation. We deliberately do **not** force a per-deploy verification gate into every 1-click stack:
  that gate would run in the *deployer's* account (their cost, their CodeBuild/Lambda, their Sigstore
  egress, their failure surface) for a guarantee the digest pin already provides. A high-assurance opt-in
  in-stack gate can be added later, off by default. (Note: a container-**Lambda** gate is not even possible —
  Lambda image functions can only pull from private same-account ECR, not `public.ecr.aws`.)
- **Public images are built + signed only in GitHub Actions**, via GitHub OIDC — never from a laptop — so
  their provenance is "built by this repo's CI," not "a person."

## Two image modes: Public (day 0) and Private (day 1)

The `ImageSource` parameter (first in the launch form, default **Public**) selects where the MCP + relay
container images come from:

- **Public** — pairputer's signed, digest-pinned `public.ecr.aws` images. Zero inputs, nothing to build.
  Because the native `AWS::BedrockAgentCore::Runtime` CFN resource's `ContainerUri` schema **rejects
  `public.ecr.aws`** (it only matches private ECR), Public mode creates the AgentCore runtime via a small
  **API-backed custom resource** (`Custom::PairputerAgentCoreRuntime`) — the AgentCore *API* accepts public
  ECR. The custom-resource Lambda is least-privilege: `bedrock-agentcore:{Create,Update,Delete,Get,List}AgentRuntime`
  plus `iam:PassRole` scoped to the one controller role (`iam:PassedToService: bedrock-agentcore`), and logs.
- **Private** — you bring your own images in your account's **private ECR** (`PrivateMcpContainerUri` +
  `PrivateRelayContainerUri`, required in this mode; a CloudFormation `Rule` fails the deploy if either is
  missing). Private mode uses the **native** `AWS::BedrockAgentCore::Runtime` resource (it accepts private
  ECR). Your images are your own supply-chain responsibility; pairputer's signing story covers only the
  public images. (Planned follow-up: Private mode with blank URIs auto-copies the public images into your
  private ECR via CodeBuild.)

## The artifacts a deployer trusts, and how each is verified

| Artifact | Origin | Integrity mechanism |
|---|---|---|
| CloudFormation templates | pairputer launch S3 bucket | CloudFormation-only bucket policy (`aws:CalledVia`); templates carry **no secrets** — every secret is generated in the deployer's account at create time |
| **MCP server image** (`public.ecr.aws/b6x6x7v3/pairputer-mcp@sha256:…`) | GitHub Actions CI | **digest-pinned** (immutable = deploy-time integrity) + **cosign keyless signature + SLSA provenance attestation**, independently verifiable out of band with `scripts/verify-images.sh` (offline, pinned Sigstore root) |
| **Relay image** (`public.ecr.aws/b6x6x7v3/pairputer-stateful-relay@sha256:…`) | GitHub Actions CI | same as MCP image |
| Base images of the two published images (`python:3.12-slim`, `node:20-slim`) | Docker Hub | **digest-pinned** (`@sha256:` arm64) so a rebuild can't silently pull a different base |
| Capsule MicroVM base | AWS-managed | governed by the template's `BaseImageArn` (versioned `aws:microvm-image:al2023-1`) — the AWS-controlled pin for the in-account build; the capsule Dockerfile `FROM` is local-build-only and intentionally not pinned to a public base to avoid conflicting with `BaseImageArn` |
| DOOM MicroVM capsule build context (the zip) † | pairputer launch bucket (for 1-click) | **tree SHA-256** (computed at package time) recorded + checked; the context is WAD-free |
| `DOOM1.WAD` (fetched during the in-account image build) † | third-party mirror | **SHA-256 pinned + verified during build** (see CLAUDE.md wall #5) — already in place |
| Relay HMAC secret, CloudFront keys, origin secret | generated in the deployer's account | never distributed; live only in the deployer's Secrets Manager |

† Reference-capsule artifacts. Present only when the **Bundle reference capsule** parameter is on (the
default). With it off, the substrate deploys capsule-empty and neither the DOOM build context nor the WAD
is fetched — removing those two third-party trust dependencies entirely.

## Signing (CI)

Public images are built, pushed, and signed exclusively by GitHub Actions using GitHub OIDC — one identity
chain, zero stored secrets:

1. **AWS auth without stored keys** — the workflow assumes an IAM role via GitHub OIDC
   (`aws-actions/configure-aws-credentials` with `role-to-assume`); the role's trust policy is scoped to
   `repo:pairputer/pairputer-platform:ref:refs/heads/main` and grants only ECR-public push.
2. **Build + push ARM64**, capturing the **digest** (never rely on the tag downstream).
3. **cosign keyless sign the digest** (workflow has `id-token: write`; cosign uses the GH Actions OIDC
   token — no private key):
   ```bash
   cosign sign --yes public.ecr.aws/<alias>/<repo>@${DIGEST}
   ```
   The signature is stored as an OCI artifact in the same public repo; the signing event (the CI identity
   + the artifact digest) is recorded in the Rekor transparency log.
4. **SLSA build provenance** (how it was built), same identity:
   ```bash
   cosign attest --yes --type slsaprovenance --predicate provenance.json \
     public.ecr.aws/<alias>/<repo>@${DIGEST}
   ```

ECR Public signing/verify is **us-east-1 only**.

## Verification (out of band — the digest pin is the deploy-time guarantee)

There is **no native signature-admission hook** in Bedrock AgentCore or plain ECS/Fargate (confirmed).
Rather than push a per-deploy verification gate into every deployer's account, the deploy-time integrity
guarantee is the **digest pin itself** — AgentCore and the relay task definition are pinned to a specific
`@sha256:` digest, which is immutable and content-addressed and cannot be swapped after the template is read.
The signature + SLSA attestation are then **independently verifiable out of band** with `scripts/verify-images.sh`:

```bash
# offline, against the pinned Sigstore root committed at scripts/sigstore-trusted-root.json
scripts/verify-images.sh          # verify the digests the template pins
# under the hood, per digest:
cosign verify --offline --trusted-root scripts/sigstore-trusted-root.json \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --certificate-identity-regexp 'https://github.com/pairputer/pairputer-platform/.github/workflows/.*@refs/heads/main' \
  public.ecr.aws/<alias>/<repo>@sha256:<digest>
cosign verify-attestation --offline --trusted-root scripts/sigstore-trusted-root.json \
  --type slsaprovenance <same identity flags> <same digest>
```

- **Offline + pinned root => fails closed, not by accident.** The Sigstore trust root is committed in-repo,
  so verification does not depend on Sigstore/Rekor being reachable at verify time — a network outage can't
  make a bad image look good. (cosign **v3** pins the root via the `--trusted-root` **flag**; the old
  `SIGSTORE_ROOT_FILE` env var is silently ignored for the new bundle format.)
- **Why not force it in-stack?** That gate would run in the *deployer's* account — their CodeBuild/Lambda,
  their cost, their egress, their failure surface — for a guarantee the immutable digest pin already gives.
  A high-assurance opt-in in-stack gate can be added later (off by default). A container-**Lambda** gate is
  not possible regardless: Lambda image functions can only pull from private same-account ECR.

## Digest pinning (the baseline that holds even without signing)

Referencing images by `@sha256:<digest>` is immutable — it defeats tag-mutation and TOCTOU-swap outright,
which matters because even ECR tag-immutability can be turned off by whoever controls repo config. Signing
composes on top: **sign the digest, verify the digest, deploy the digest.** As belt-and-suspenders, the
public ECR repos are also set to `IMMUTABLE` tags.

## Runtime auth: Cognito ↔ AgentCore, and token lifetimes

Supply-chain integrity gets the *right* code running; this section covers who is allowed to *call* it.
Everything below is created fresh in the deployer's account — no shared identity, no secret ever leaves it.

**Control plane — chat host → Cognito → AgentCore MCP.** There is one public Cognito app client per
interactive host, plus a confidential smoke-test client, on the deployer's own user pool (`identity.yaml`):

| Client | Flow | Secret | Access token | Refresh token | Scopes |
|---|---|---|---|---|---|
| **Codex** (interactive) | `code` (authorization-code + PKCE) | **none** (public client) | **8 hours** | **30 days** | `openid`, `pairputer-mcp/invoke` |
| **ChatGPT** (interactive) | `code` (authorization-code + PKCE) | **none** (public client) | **8 hours** | **30 days** | standard OIDC scopes, `pairputer-mcp/invoke` |
| **Claude** (interactive) | `code` (authorization-code + PKCE) | **none** (public client) | **8 hours** | **30 days** | standard OIDC scopes, `pairputer-mcp/invoke` |
| **M2M** (smoke tests only) | `client_credentials` | yes (confidential) | **24 hours** | — | `pairputer-mcp/invoke` |

Token revocation is on (Cognito default), and self-sign-up is disabled — the pool is
**admin-created users only** (`AllowAdminCreateUserOnly`), so a leaked callback can't register accounts.
Cognito's hosted-UI login surface has a regional WAF in front of it.

**AgentCore validates the JWT itself.** The MCP runtime is configured with a `CustomJWTAuthorizer`
(`agentcore.yaml`) that points at the pool's OIDC discovery document
(`https://cognito-idp.<region>.amazonaws.com/<poolId>/.well-known/openid-configuration`) and an
`AllowedClients` allow-list of exactly the four client IDs above and an `AllowedScopes` requirement for
`pairputer-mcp/invoke`. AgentCore fetches Cognito's JWKS from that URL and verifies the token's signature,
issuer, expiry, client, and required scope on every call — so an expired, wrong-issuer, wrong-client, or
scope-less token is rejected at the runtime before any tool code runs. Only the `Authorization` header is
allow-listed into the runtime; nothing else is forwarded. The MCP server holds **no static AWS keys** — it
acts through its IAM execution role.

**Defense-in-depth: the container re-verifies the JWT too (2026-07-12 audit).** The tenant model
(`tenant_id = sha256(iss:sub)`) previously trusted the decoded claims because AgentCore had verified
them upstream — safe, but a single point of failure if anything ever let a request reach the runtime
with an attacker-set `Authorization` header. `server.py::_verify_jwt` now INDEPENDENTLY re-verifies the
RS256 signature + `iss` + `exp` against Cognito's JWKS inside the container (`PAIRPUTER_JWT_DISCOVERY_URL`,
JWKS cached + refetched on key rotation), fail-closed, before deriving the tenant. It is a no-op only
when the discovery URL is unset (LOCAL_MODE / a not-yet-migrated deploy), so the tenant model no longer
relies SOLELY on AgentCore being the only ingress.

**The CloudFront signed-URL edge gate is mandatory (2026-07-12 audit).** `CloudFrontKeyGroupId` is a
required stack param (`MinLength: 1`) and the distribution's `TrustedKeyGroups` is unconditional — an
empty value previously (`!If HasCloudFrontKeyGroup`) silently dropped the signed-URL requirement,
collapsing "edge auth before origin" to just the relay `?t=` token. That footgun is closed.

**Data plane — a separate, short-lived grant.** The Cognito token authorizes the *control plane*; it does
**not** reach the relay. When a session opens, the MCP server mints its own **HMAC-signed relay token with
a 15-minute TTL** (`SESSION_TOKEN_TTL_SECONDS`), scoped to specific channels (video/audio/input) and
bound to the DynamoDB session's id + version. The matching CloudFront signed-URL policy carries the **same
`exp`**. The relay re-verifies the HMAC, the channel, and **DynamoDB session freshness** on every request —
so a token minted before a Freeze (which rotates the session version) is dead even inside its 15-minute
window. A Codex thread left open past the TTL recovers by calling the authenticated MCP channel again
(`pairputer_session`) to mint a fresh relay token — the long-lived Cognito refresh token is what makes that
seamless without re-login. The MicroVM's own JWE (relay→VM) is minted server-side and **never reaches the
browser**.

Net: a long-lived *identity* (Cognito, 8h/30d, revocable) gates the control plane; a short-lived,
session-bound, channel-scoped *capability* (15-min HMAC + matching CloudFront policy) gates the data plane;
and the two are independent, so compromising one grant doesn't hand over the other.

## What is explicitly out of scope / known limits

- **No AgentCore/ECS native admission control** for signatures — the deploy-time guarantee is the immutable
  digest pin; signature/provenance are verified out of band (`scripts/verify-images.sh`), not enforced by an
  in-stack admission gate (see above for why, and the opt-in path if you want one).
- **The DOOM MicroVM image itself cannot be pre-signed by us** — `AWS::Lambda::MicrovmImage` builds
  **in-account** from the S3 context (no ECR/prebuilt import). Its integrity rests on the tree-hash of the
  build context + the SHA-256-pinned WAD fetched during the build, both in the deployer's own account.
- Long-lived AWS credentials are never stored in GitHub (OIDC only) and never distributed to deployers.
