# ci/ — pairputer build & release infrastructure (operator-only)

**This directory is NOT part of the deployable platform.** It is the infrastructure *we* (the pairputer
maintainers) use to build, sign, and publish the public artifacts that end users deploy. It is never
handed to a deployer, never packaged into the launch bucket, and never referenced by
`substrate/cloudformation/`.

Clean separation of concerns:

| Directory | Audience | What it is |
|---|---|---|
| `substrate/cloudformation/` | **end users** | the deployable stack (Launch button / `deploy.sh`) |
| `capsules/` | end users / capsule authors | reference capsules |
| **`ci/`** | **pairputer maintainers only** | how we publish the signed public images users pull |

## Contents

- **`pairputer-ci.yaml`** — deploy ONCE in the account that owns the public images. Creates:
  - the **GitHub Actions OIDC provider** (so CI authenticates to AWS with no stored keys),
  - a **least-privilege push role** assumable ONLY by `pairputer/pairputer-platform` on `refs/heads/main` (scoped to
    `sts:AssumeRoleWithWebIdentity` with `sub`/`aud` conditions; permissions limited to `ecr-public` push
    on exactly the two named repos),
  - the two **public ECR repos** (`pairputer-mcp`, `pairputer-stateful-relay`), immutable tags.
- **`.github/workflows/`** (at repo root) — the workflow that builds ARM64, pushes, and cosign-keyless
  signs + attests. It assumes the push role above via OIDC.

## Deploy (maintainer, once)

```bash
aws cloudformation deploy \
  --template-file ci/pairputer-ci.yaml \
  --stack-name pairputer-ci \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
# then: PushRoleArn output -> the workflow's role-to-assume; note the public ECR registry alias.
```

See `SECURITY.md` for the full trust model this infra implements.
