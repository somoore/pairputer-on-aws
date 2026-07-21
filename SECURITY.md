# pairputer - Security

pairputer is a technical demo that people deploy into their own AWS accounts. Because the default
deploy pulls container images that the project publishes, rather than images you build yourself,
supply-chain integrity is the trust boundary that matters most. This document describes every artifact
a deploy trusts and how you verify each one, then covers the runtime authentication model.

The guiding principle is *provable in your own account*, not "trust the project." You can independently
verify the published images offline, against a Sigstore root committed in this repo, before you deploy.

## Report a security issue

pairputer is a demo, not a supported product. There is no security SLA, no coordinated-disclosure
timeline, and no bug-bounty program. If you find a security problem,
[open a GitHub issue](https://github.com/somoore/pairputer-on-aws/issues). Describe the problem and how
to reproduce it. For a sensitive report, open a minimal issue asking for a private channel rather than
posting details publicly.

## Design decisions

These choices are fixed:

- **Keyless signing with cosign (Sigstore + OIDC).** There is no private signing key to protect.
  Provenance is bound to the CI identity that built the image.
- **Digest-pin everything.** One `@sha256:` digest threads through sign, publish, and deploy. Tags never
  appear on the trust path. In your stack, the digest pin *is* the deploy-time integrity guarantee: a
  `@sha256:` reference is immutable and content-addressed, so the template cannot name one image and
  receive another.
- **Verify out of band; don't force an in-stack gate.** `scripts/verify-images.sh` lets you, your
  security team, or an auditor confirm offline that the pinned digests are cosign-signed by this repo's
  CI and carry a SLSA provenance attestation. The 1-click stack does not force a per-deploy verification
  gate, because that gate would run in your account (your cost, your CodeBuild or Lambda, your Sigstore
  egress, your failure surface) for a guarantee the digest pin already provides. See
  [Verify the images](#verify-the-images) for why, and for the opt-in path if you want in-stack
  enforcement.
- **Build and sign public images only in CI.** GitHub Actions builds and signs the public images through
  GitHub OIDC, never from a laptop. Their provenance is "built by this repo's CI," not "built by a
  person."

## Two image modes

The `ImageSource` parameter is the first field in the launch form. It selects where the MCP server and
relay container images come from. The default is **Public**.

### Public mode

Public mode uses the project's signed, digest-pinned images from `public.ecr.aws`. It takes no inputs
and builds nothing.

The native `AWS::BedrockAgentCore::Runtime` CloudFormation resource rejects `public.ecr.aws` URIs in its
`ContainerUri` schema, because that schema only matches private ECR. Public mode therefore creates the
AgentCore runtime through a small API-backed custom resource (`Custom::PairputerAgentCoreRuntime`); the
AgentCore *API* accepts public ECR even though the CloudFormation resource does not. The custom-resource
Lambda is least-privilege: it holds `bedrock-agentcore:{Create,Update,Delete,Get,List}AgentRuntime`,
`iam:PassRole` scoped to the single controller role (`iam:PassedToService: bedrock-agentcore`), and log
permissions.

### Private mode

Private mode uses private ECR in your own account. You have two options:

- **Bring your own images.** Set `PrivateMcpContainerUri` and `PrivateRelayContainerUri`. These images
  are then your own supply-chain responsibility.
- **Let the stack copy the project's images.** Leave either URI blank, and an in-stack CodeBuild job
  (`ImageCopyStack`) cosign-verifies the project's signed public digest first (offline, against the
  pinned Sigstore root) and only then uses `crane` to copy it into a private ECR repo in your account.

Private mode uses the native `AWS::BedrockAgentCore::Runtime` resource, which accepts private ECR.

## Artifacts a deploy trusts

Every artifact and its integrity mechanism:

| Artifact | Origin | Integrity mechanism |
|---|---|---|
| CloudFormation templates | Project launch S3 bucket | Bucket policy allows reads only through CloudFormation (`aws:CalledVia`). Templates carry no secrets; every secret is generated in your account at create time. |
| MCP server image | GitHub Actions CI | Digest-pinned (immutable, so the pin is the deploy-time integrity guarantee), plus a cosign keyless signature and a SLSA provenance attestation. Independently verifiable out of band with `scripts/verify-images.sh` (offline, pinned Sigstore root). |
| Relay image | GitHub Actions CI | Same as the MCP server image. |
| Base images of the two published images (`python:3.12-slim-bookworm`, `node:20-bookworm-slim`) | Docker Hub | Digest-pinned (`@sha256:` arm64), so a rebuild cannot silently pull a different base. |
| Capsule MicroVM base | AWS-managed | Governed by the template's `BaseImageArn` (versioned `aws:microvm-image:al2023-1`), the AWS-controlled pin for the in-account build. The capsule Dockerfile `FROM` is for local builds only and is intentionally not pinned to a public base, to avoid conflicting with `BaseImageArn`. |
| Bundled capsule (Pairputer Workbench) build-context zip † | Project launch bucket (1-click only) | The tree SHA-256 is embedded in the filename (content-addressed). The in-stack manifest stager reads the validated `capsule.manifest.json` from that exact zip and stages an immutable, digest-chained SSM release: manifest digest, then release record, then an atomic `/current` pointer. |
| Third-party binaries the Workbench build fetches (gh, ripgrep, uv, code-server, FFmpeg, Chromium, Homebrew) † | GitHub releases and public mirrors | Version-pinned and SHA-256-pinned in the Dockerfile, verified during the in-account build. |
| Relay HMAC secret, CloudFront keys, origin secret | Generated in your account | Never distributed. They live only in your Secrets Manager. |

† These are reference-capsule artifacts. They exist only when the **Bundle reference capsule** parameter
is on, which is the default. With it off, the substrate deploys with no capsule, and neither the build
context nor its third-party binaries are fetched. The optional Agent DOOM cartridge
(`deploy-capsule.sh agent-doom`) follows the same rules: a tree-hashed context, and a `DOOM1.WAD` that
is SHA-256-pinned and verified during the in-account build.

## Which images "the project's images" means

Public mode pulls the images this project publishes from `public.ecr.aws/b6x6x7v3/pairputer-mcp` and
`public.ecr.aws/b6x6x7v3/pairputer-stateful-relay`. The `b6x6x7v3` segment is the ECR Public *registry
alias*, which AWS assigns to the registry that owns those repos. An alias is not a secret; it appears in
every pull URL.

If you fork this repo and republish the images from your own CI and AWS account, you get a different
alias. In that case, what you trust is not the alias string but two things that do not change:

- The **digests** pinned in `substrate/cloudformation/pairputer.yaml`.
- The **signer identity** the verification checks (`somoore/pairputer-on-aws` through GitHub OIDC), which
  you set to your own repo when you republish (`PAIRPUTER_SIGNER_IDENTITY_REGEXP` in
  `scripts/verify-images.sh`).

## How images are signed

GitHub Actions builds, pushes, and signs the public images, using GitHub OIDC as a single identity chain
with no stored secrets:

1. **Get AWS access without stored keys.** The workflow assumes an IAM role through GitHub OIDC
   (`aws-actions/configure-aws-credentials` with `role-to-assume`). The role's trust policy is scoped to
   `repo:somoore/pairputer-on-aws:ref:refs/heads/main` and grants only ECR Public push.
2. **Build and push for ARM64,** capturing the digest. Nothing downstream relies on the tag.
3. **Sign the digest with cosign, keyless.** The workflow has `id-token: write`, and cosign uses the
   GitHub Actions OIDC token, so there is no private key:

   ```bash
   cosign sign --yes public.ecr.aws/<alias>/<repo>@${DIGEST}
   ```

   The signature is stored as an OCI artifact in the same public repo. The signing event, meaning the CI
   identity plus the artifact digest, is recorded in the Rekor transparency log.
4. **Attach SLSA build provenance** with the same identity:

   ```bash
   cosign attest --yes --type slsaprovenance --predicate provenance.json \
     public.ecr.aws/<alias>/<repo>@${DIGEST}
   ```

Publishing to ECR Public (push and sign) runs in `us-east-1` only. Verification runs from anywhere.

## Verify the images

There is no native signature-admission hook in Bedrock AgentCore or in plain ECS/Fargate. Instead of
forcing a per-deploy verification gate into your account, the deploy-time integrity guarantee is the
digest pin itself: AgentCore and the relay task definition are pinned to a specific `@sha256:` digest,
which is immutable and content-addressed and cannot be swapped after the template is read.

You verify the signature and SLSA attestation out of band with `scripts/verify-images.sh`:

```bash
# Offline, against the pinned Sigstore root at scripts/sigstore-trusted-root.json.
scripts/verify-images.sh          # verifies the digests the template pins
```

Under the hood, for each digest:

```bash
cosign verify --offline --trusted-root scripts/sigstore-trusted-root.json \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --certificate-identity-regexp 'https://github.com/somoore/pairputer-on-aws/.github/workflows/.*@refs/heads/main' \
  public.ecr.aws/<alias>/<repo>@sha256:<digest>

cosign verify-attestation --offline --trusted-root scripts/sigstore-trusted-root.json \
  --type slsaprovenance <same identity flags> <same digest>
```

Two properties make this reliable:

- **Offline plus a pinned root means it fails closed.** The Sigstore trust root is committed in the repo,
  so verification does not depend on Sigstore or Rekor being reachable at verify time. A network outage
  cannot make a bad image look good. cosign v3 pins the root through the `--trusted-root` flag; the older
  `SIGSTORE_ROOT_FILE` environment variable is silently ignored for the new bundle format.
- **The in-stack gate is opt-in, not forced.** A forced gate would run in your account (your CodeBuild or
  Lambda, your cost, your egress, your failure surface) for a guarantee the immutable digest pin already
  gives. A high-assurance in-stack gate can be added later, off by default. A container-Lambda gate is
  not possible in any case: Lambda image functions can pull only from private, same-account ECR, not from
  `public.ecr.aws`.

## Digest pinning

Referencing an image by `@sha256:<digest>` is immutable. It defeats tag mutation and TOCTOU swaps
outright, which matters because even ECR tag-immutability can be turned off by whoever controls the repo
configuration. Signing composes on top: sign the digest, verify the digest, deploy the digest. As
belt-and-suspenders, the public ECR repos are also set to immutable tags.

## Runtime authentication

Supply-chain integrity ensures the right code runs. This section covers who is allowed to call it.
Everything here is created fresh in your account. There is no shared identity, and no secret ever leaves
your account.

### Control plane: chat host to Cognito to AgentCore

Your user pool has one public Cognito app client per interactive host, plus a confidential smoke-test
client (`identity.yaml`):

| Client | Flow | Secret | Access token | Refresh token | Scopes |
|---|---|---|---|---|---|
| Codex (interactive) | authorization-code + PKCE | None (public client) | 8 hours | 30 days | `openid`, `pairputer-mcp/invoke` |
| ChatGPT (interactive) | authorization-code + PKCE | None (public client) | 8 hours | 30 days | Standard OIDC scopes, `pairputer-mcp/invoke` |
| Claude (interactive) | authorization-code + PKCE | None (public client) | 8 hours | 30 days | Standard OIDC scopes, `pairputer-mcp/invoke` |
| M2M (smoke tests only) | client_credentials | Yes (confidential) | 24 hours | None | `pairputer-mcp/invoke` |

Token revocation is on (the Cognito default), and self-sign-up is disabled. The pool is admin-created
users only (`AllowAdminCreateUserOnly`), so a leaked callback cannot register accounts. A regional WAF
sits in front of the Cognito hosted-UI login surface.

### AgentCore verifies the JWT

The MCP runtime is configured with a `CustomJWTAuthorizer` (`agentcore.yaml`) that points at the pool's
OIDC discovery document
(`https://cognito-idp.<region>.amazonaws.com/<poolId>/.well-known/openid-configuration`). It has an
`AllowedClients` allow-list of exactly the four client IDs above and an `AllowedScopes` requirement for
`pairputer-mcp/invoke`. AgentCore fetches Cognito's JWKS from that URL and verifies the token's
signature, issuer, expiry, client, and required scope on every call. An expired, wrong-issuer,
wrong-client, or scope-less token is rejected at the runtime before any tool code runs. Only the
`Authorization` header is allow-listed into the runtime; nothing else is forwarded. The MCP server holds
no static AWS keys; it acts through its IAM execution role.

### The container re-verifies the JWT

The tenant model derives `tenant_id = sha256(iss:sub)`. It previously trusted the decoded claims because
AgentCore had verified them upstream. That is safe, but it is a single point of failure if anything ever
lets a request reach the runtime with an attacker-set `Authorization` header.

`server.py::_verify_jwt` now independently re-verifies the RS256 signature, `iss`, and `exp` against
Cognito's JWKS inside the container (`PAIRPUTER_JWT_DISCOVERY_URL`; the JWKS is cached and refetched on
key rotation). It fails closed, and it runs before deriving the tenant. It is a no-op only when the
discovery URL is unset (local mode, or a not-yet-migrated deploy), so the tenant model no longer relies
solely on AgentCore being the only ingress.

### The CloudFront signed-URL gate is mandatory

`CloudFrontKeyGroupId` is a required stack parameter (`MinLength: 1`), and the distribution's
`TrustedKeyGroups` is unconditional. An empty value used to silently drop the signed-URL requirement,
collapsing "edge auth before origin" down to just the relay `?t=` token. That gap is closed.

### Data plane: a separate, short-lived grant

The Cognito token authorizes the control plane. It does not reach the relay.

When a session opens, the MCP server mints its own HMAC-signed relay token with a 15-minute TTL
(`SESSION_TOKEN_TTL_SECONDS`), scoped to specific channels (video, audio, input) and bound to the
DynamoDB session's ID and version. The matching CloudFront signed-URL policy carries the same `exp`. The
relay re-verifies the HMAC, the channel, and DynamoDB session freshness on every request, so a token
minted before a Freeze (which rotates the session version) is dead even inside its 15-minute window.

A chat thread left open past the TTL recovers by calling the authenticated MCP channel again
(`pairputer_session`) to mint a fresh relay token. The long-lived Cognito refresh token is what makes
that seamless without re-login. The MicroVM's own JWE (relay to VM) is minted server-side and never
reaches the browser.

The result: a long-lived identity (Cognito, 8h/30d, revocable) gates the control plane; a short-lived,
session-bound, channel-scoped capability (15-minute HMAC plus a matching CloudFront policy) gates the
data plane; and the two are independent, so compromising one grant does not hand over the other.

## Out of scope and known limits

- **No native admission control for signatures** in AgentCore or ECS. The deploy-time guarantee is the
  immutable digest pin; signature and provenance are verified out of band
  (`scripts/verify-images.sh`), not enforced by an in-stack admission gate. See
  [Verify the images](#verify-the-images) for the reasoning and the opt-in path.
- **The capsule MicroVM image cannot be pre-signed by the project.** `AWS::Lambda::MicrovmImage` builds
  in your account from the S3 context; it cannot import an ECR or prebuilt image. Its integrity rests on
  the content-addressed tree hash of the build context and the SHA-256-pinned third-party artifacts
  fetched during the build, both in your own account.
- **Long-lived AWS credentials** are never stored in GitHub (OIDC only) and never distributed.
