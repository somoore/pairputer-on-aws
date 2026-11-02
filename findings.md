# Security findings — pairputer-on-aws

**Scanned:** `somoore/pairputer-on-aws` @ `1085d27` (301 tracked files), 2 November 2026.
**Tooling:** gitleaks, semgrep, trivy (config + deps), bandit, hadolint, shellcheck, cfn-lint,
PyYAML template parse, pytest (1002 tests).
**Review roles:** PM/risk, AppSec, Pentester, Devil's Advocate (adjudicator), QA.
**Scope note:** this repository is deployed to AWS; no live account was used for remediation.
"FIXED" below means the repository change is present and the repo's own gates pass. Items whose
verification needs a deploy are marked with the static gate that stands in for it.

Adjudication follows the Devil's Advocate (`04-devil.md`) on verdicts and the Pentester
(`03-pentest.md`) on reachability. 280 of the 396 scanner rows are false positives (test fixtures,
RFC-mandated cryptography, argv-list subprocess, loopback/constant URLs, fixed-enum SQL
placeholders, deliberate demo posture); they are grouped below as `FP-*` with the reason.

## Findings

| ID | Severity | Status | Location | Issue | Fix | Verification |
|----|----------|--------|----------|-------|-----|--------------|
| C-01 / PA-01 / QA-04 / I-05 | MEDIUM | FIXED | `.github/workflows/publish-launch.yml:45,48`; `.github/workflows/ci.yml:21,23` | Privileged OIDC job used mutable action tags; a repointed tag minted the launch-push role. | Pinned `actions/checkout` and `aws-actions/configure-aws-credentials` to the same 40-hex SHAs as `publish-images.yml`; pinned `ci.yml` actions too. | `pytest tests/test_ci_supply_chain.py` (all workflow `uses:` match `@[0-9a-f]{40}`); `git diff` review. |
| PA-02 | MEDIUM | FIXED | `ci/pairputer-ci.yaml:95-101` | OIDC trust used `StringLike ...:ref:refs/heads/main*`, so `main-fix` could mint the push role and rewrite the public launch template. | Single `StringEquals` block on the exact `sub` (`...:ref:${GitHubRef}`) plus the existing numeric owner/repo ids; no wildcard. | `pytest tests/test_ci_supply_chain.py` (exact ref, no `StringLike`); `cfn-lint` exit 0. Post-deploy check: a `main-fix` OIDC token must fail `sts:AssumeRoleWithWebIdentity`. |
| PA-04 | MEDIUM | FIXED | `substrate/cloudformation/nested/image-copy.yaml:124-148` | CodeBuild installed `crane` from `releases/latest` and `cosign` with no checksum, then ran both under the ECR push role. | Pinned crane `0.22.1` + cosign `3.1.1`, committed SHA-256 for each, `sha256sum -c -` before `install`. | `grep releases/latest` → none; `pytest tests/test_ci_supply_chain.py`; `cfn-lint` exit 0. |
| PA-03 | LOW | FIXED | `capsules/nested/capsule-stack.yaml:270-300`; `substrate/deploy-capsule.sh:122-132` | The recorded `contextSha256` was copied from the parameter/filename and never checked against the downloaded zip. | Stager recomputes the packager's tree hash from the zip entries (`context_tree_sha256`) and fails the deploy on mismatch; CLI now records that same content address (parsed from the uploaded object name). | `pytest tests/test_manifest_stager_parity.py` (synthetic zip matches packager algorithm; template contains the compare); stager inline code compiles. |
| PA-05 | LOW | FIXED | `substrate/stateful-relay/index.mjs:729`; `substrate/mcp-server/server.py:2475-2491` | `CreateMicrovmAuthToken` used `allPorts`, unlocking VNC 5901, CDP 9222 and gRPC 50051. | Relay mints only `6902,6903,6904,6906,9000`; control plane mints only the manifest bridge port + `6906` (per-call cache key). | `pytest tests/test_ci_supply_chain.py` (no `allPorts`; port list asserts 5901/9222/50051 absent); `node --check`. |
| PA-06 / PT-02 | LOW | FIXED (Workbench) | `capsules/computer-use-desktop/rootfs/opt/capsule/input_ws.py:594-630` | Any connection without `t:"auth"` was treated as `actor="human"`, so in-guest app/terminal code could bump `humanEpoch` and seize input. | Requires the per-MicroVM viewer capability (`mode:"viewer"`, root-provisioned `viewer-input.key`) before `actor="human"`; unauthenticated input gets `accepted:false, reason:"unauthenticated"`. The relay presents the same capability read from the session row. Local-dev has an explicit, documented opt-out. | `pytest tests/test_computer_use_runtime.py -k input_ws` (behavioral: no-auth/wrong-key refused, correct viewer key accepted, agent key never claims human, Origin socket closed). |
| N-02 / PA-07 / P-1 | LOW | FIXED | `capsules/agent-doom/rootfs/opt/capsule/agent_bridge.py:425-445`; `.../start.sh:91-110` | DOOM bridge accepted every route with no capability and no `Origin` check. | Requires `X-Pairputer-Bridge-Capability` (root-only `bridge-ingress.key`, installed from the Run hook); rejects browser `Origin`; autopilot presents the capability. | `pytest tests/test_agent_capsule.py::TestCoordination::test_doom_agent_bridge_requires_capability_and_rejects_origin`; `python -m py_compile`; inline hook code compiles. |
| PT-01 | LOW | FIXED | `substrate/stateful-relay/index.mjs:434-447` | `retiredViewerIds` only grew; the 129th distinct viewer got a permanent 429 (self-lockout / token-holder DoS). | Bounded retirement set with oldest-first eviction (`retireViewerId`); the 400 ms handoff semantics are unchanged. | `pytest tests/test_video_stream_limit.py` (Node harness: 128 retired ids, 129th admitted, oldest evicted, set stays bounded). |
| QA-01 | MEDIUM | FIXED | `.github/workflows/ci.yml:27-33` | CI installed only pytest+pyyaml, so `tests/test_jwt_verification.py` skipped via `importorskip`; the JWT verifier had zero CI coverage. | Install `cryptography` (pinned in `requirements.in`) and run pytest with `-rs`. | `pytest tests/test_jwt_verification.py -q -rs` → 8 passed, 0 skipped in CI-shaped run; full suite 1002 passed. |
| QA-02 | LOW | FIXED | `substrate/cloudformation/pairputer.yaml:397-400` | Duplicate `ContainerUri` / `RelayContainerUri` keys in `ParameterLabels`; PyYAML last-wins hid them. | Removed the duplicate labels; added a cfn-lint (errors) CI step. | `uvx cfn-lint --non-zero-exit-code error ...` → exit 0, zero `E` rows (was 3 `E0000`). |
| QA-03 | LOW | FIXED | `.github/workflows/ci.yml:35-43` | Four shell harnesses + `test_recovery_image_id.py` passed locally but were never run by CI. | Added a CI step running all five. | All five exit 0 locally. |
| QA-05 | LOW | FIXED | `.github/workflows/ci.yml` shellcheck step | CI shellchecked 6 of 31 tracked scripts. | `git ls-files '*.sh' \| xargs shellcheck -S error`. | `git ls-files '*.sh' \| xargs shellcheck -S error` → exit 0 (31 scripts). |
| C-02 / PA-16 | LOW | FIXED | `substrate/cloudformation/nested/image-copy.yaml:49,58` | Private ECR repos did not scan copied images. | Added `ImageScanningConfiguration: {ScanOnPush: true}` to both repos. | `cfn-lint` exit 0; `aws ecr describe-repositories` → `scanOnPush: true` (post-deploy). |
| C-03 / PA-13 | MEDIUM | FIXED | `substrate/cloudformation/nested/relay-network.yaml:214-225` | fck-nat instance (default networking mode, public subnet, broad ENI/EIP role) had IMDSv1 reachable. | `MetadataOptions: {HttpEndpoint: enabled, HttpTokens: required}`. | `cfn-lint` exit 0; post-deploy `aws ec2 describe-instances ... MetadataOptions.HttpTokens == required`. |
| C-05 | LOW | FIXED | `substrate/cloudformation/nested/relay.yaml:184-191` | ALB forwarded malformed/duplicate headers to the relay. | `routing.http.drop_invalid_header_fields.enabled=true`. | `cfn-lint` exit 0; `pytest tests/test_relay_origin_tls.py`; post-deploy `describe-load-balancer-attributes`. |
| C-07 / PA-12 (TLS) | LOW | FIXED | `substrate/cloudformation/nested/relay.yaml:463-469` | CloudFront default certificate floor was TLSv1.0. | `ViewerCertificate: {CloudFrontDefaultCertificate: true, MinimumProtocolVersion: TLSv1.2_2021}`. | `cfn-lint` exit 0; `pytest tests/test_workbench_security_topology.py`; post-deploy `get-distribution-config`. |
| C-04 | LOW | DEFERRED | `relay-network.yaml:214` | fck-nat root volume unencrypted. | Needs the AMI's root device name/snapshot to add a correct encrypted `BlockDeviceMappings` entry; guessing creates an extra volume instead of encrypting root. | Post-deploy `aws ec2 describe-volumes` check documented. |
| C-06 / PA-08 / PT-04 | LOW | DEFERRED | `substrate/mcp-server/Dockerfile:20`; `substrate/stateful-relay/Dockerfile:14` | Both shipped images run as root. | Adding `USER` changes the image digest; the publish workflow must rebuild and every launch template must re-pin the digest. Tracked for the next image release. | `docker run --rm <image> id -u` must be non-zero; grep test deferred with the image change. |
| N-01 / PA-09 | LOW | DEFERRED | `substrate/mcp-server/server.py:511-519` | In-container JWT verifier checks signature/iss/exp/token_use but not `client_id`/`scope`. | Requires enumerating all four app-client ids and a fail-closed config; a wrong env value logs everyone out. Needs a planned config change, not a hot patch. | Existing `pytest tests/test_jwt_verification.py` stays green; new foreign-client cases to land with the config. |
| N-03 / QA-07 / QA-08 | LOW | DEFERRED | `.github/workflows/ci.yml` | No SAST/dependency/secret gate (baseline was hand-produced). | Adding gitleaks/semgrep/pip-audit needs fixture allowlists and another pinned action; deliberately not added in this pass to avoid an unproven blocking gate. | gitleaks run manually (see below) — only fixture placeholders. |
| N-05 / PA-11 / PT-08 | LOW | DEFERRED | `substrate/deploy.sh:701,705-720` | Super-admin password and Cognito password passed via CLI argv. | Operator-local process-table exposure; scripted path only. Move to `file://` input in a follow-up (interactive deploy unaffected). | Manual review; not reachable by a remote party. |
| PT-05 | LOW | DEFERRED | `substrate/stateful-relay/index.mjs:1583-1605` | Player `message` listener has no `ev.origin`/`ev.source` check. | Needs the complete chat-host/iframe origin allowlist or widget controls break; coordinate with the widget release. | `tests/widget_runtime.cjs` regression to add with the allowlist. |
| PT-11 | INFO | DEFERRED | `capsules/computer-use-desktop/rootfs/opt/capsule/skills/code_skills.py:69` | Gates on `"process.shell"` while the canonical enum is `"process_shell"`; shell is therefore always denied (fail-closed). | Renaming enables a shell surface that has never run — a product decision, not a security fix. | N/A (dead feature, no bypass). |
| QA-06 | MEDIUM (governance) | DEFERRED | GitHub branch protection (repo settings) | Reported `main` has no required checks, so a red/skipped run does not block merge. | Org setting, not code; cannot be changed from the working tree. | `gh api repos/.../branches/main/protection` — owner action. |
| QA-09 | LOW | DEFERRED | `substrate/stateful-relay/index.mjs` | Relay auth/limit behaviour is only string-asserted; no importable seam. | Extract `createRelay({deps})` and add `node --test` cases as a separate refactor. | String tests kept as backstop. |
| PA-14 | LOW | DEFERRED | `capsules/agent-doom/Dockerfile:49-50,129-131` | `grpcio`/`protobuf` installed without `--require-hashes`; restful-doom tarball fetched at a commit without a local checksum. | Content-addressed fetch; adding hashes changes layer digests and needs an image rebuild + digest re-pin. | Follow-up with the next capsule release. |
| I-06 | INFO (functional) | DEFERRED | `image-copy.yaml:147` | `crane copy` to an IMMUTABLE `:latest` cannot be re-pushed on redeploy. | Not a security issue; fix with `ecr:BatchDeleteImage` + tag delete in the same change that next touches private-image mode. | Reproduce with two private-mode deploys. |
| P-3 | MEDIUM (accepted) | ACCEPTED RISK | Workbench autonomy / in-VM browsing | A prompt-injected agent can exfiltrate through an arbitrary URL without a policy bypass. | Inherent to an internet-connected computer-use agent; an egress allowlist would remove the core product. Documented as an operator option. | — |
| AR-01 | INFO | ACCEPTED RISK | `capsules/computer-use-desktop/.../process_service.py` | Remote shell is the product. | Capability HMAC, Origin rejection, `explicit_shell_mode`, 32 KB bound, fd confinement, `no_new_privs`, UID drop, cgroup/mount isolation already present and tested. | `pytest tests/test_computer_use_runtime.py` process-service cases. |
| AR-02 / P-5 | INFO | ACCEPTED RISK | capsule Dockerfiles | Capsules run as root (X11/broker/per-service UID model). | Removing root breaks Xvnc/websockify/setuid; per-job setuid/setgid/no_new_privs is the control. | Capsule tests. |
| AR-03 | INFO | ACCEPTED RISK | capsule WS binds | `0.0.0.0` inside the MicroVM. | Only ingress is the IAM-token-gated `:443` proxy; loopback bind would break the proxy hop. | Architecture tests. |
| AR-04 | INFO | ACCEPTED RISK | Dockerfiles | No `HEALTHCHECK`. | Never evaluated by AgentCore/Fargate (ALB/readiness gates instead). | — |
| AR-05 / AR-06 / AR-07 | INFO | ACCEPTED RISK | relay-network / ALB | ALB/NAT egress, public NAT subnets, no VPC flow logs. | By design; ingress is tightly scoped and flow-log retention is a cost/ops decision. | `tests/test_relay_origin_tls.py`. |
| AR-08 / PA-15 (logging) | INFO | ACCEPTED RISK | `relay.yaml` CloudFront logging off | Enabling standard logging would write live `?t=` bearer tokens to S3. | Keep logging off until the token moves out of the query string; added no naive logging. | — |
| AR-09 / AR-10 / AR-11 | INFO | ACCEPTED RISK | sessions/lambdas/KMS | No PITR, no X-Ray, AWS-managed KMS keys. | Disposable session state; single-tenant demo; CMKs add blast radius/cost. | — |
| N-04 | LOW | ACCEPTED RISK | `local-dev.sh:85-125` | Local Docker mode grants NET_ADMIN/SYS_ADMIN. | Explicitly a lower-assurance dev mode: seccomp, two caps only, loopback ports, 0600 capability file. The AWS path uses AgentCore capabilities. | `pytest tests/test_computer_use_capsule.py` local-dev assertions. |
| PA-17 | INFO | ACCEPTED RISK | input arbiter | Input spam. | Guest clamps and cooldown; relay caps 32 events/4 KB/240 per second. | Relay/guest tests. |
| PT-03 | MEDIUM (accepted) | ACCEPTED RISK | `server.py:3127-3132` | Approval error body tells the model the digest/preview it needs for the second call. | Inherent to MCP `requiresApproval`; the host allow-list fails closed for Claude. Changing the body breaks the documented flow and tests. | `tests/test_capsule_release_binding.py`. |
| PA-10 | INFO | FALSE POSITIVE | `server.py` dev token; relay `ALLOW_UNSIGNED_DEV` | "Dev backdoor". | The dev token is still passed through `_verify_jwt` (rejected when a discovery URL is set), and `ALLOW_UNSIGNED_DEV` only skips HMAC when the session secret is also unset, which the task definition always sets. Two independent misconfigurations. | Code review; `tests/test_relay_session_freshness.py`. |
| P-2 | INFO | FALSE POSITIVE | `server.py` image/microvm ids | IDOR claim. | Tenant partition is `sha256(iss:sub)`; `_resolve_image_id` maps registry keys; `_require_bound_session` re-checks with `compare_digest`; ownership is a conditional transaction. | Existing tenant tests. |
| P-4 / FP-02 | INFO | FALSE POSITIVE | `publish-images.yml:79-87` | Shell injection. | `workflow_dispatch` only, gated on `confirm == 'publish'`; interpolated values are repo-owned. A fork PR cannot dispatch it. | Workflow review. |
| PT-10 | INFO | FALSE POSITIVE | `.pyc` gitleaks hits | "Committed build output". | `git ls-files` shows zero tracked `.pyc`; they are untracked and gitignored — the scanner scanned the working tree. | `git ls-files '*pycache*'` → empty. |
| FP-01 | INFO | FALSE POSITIVE | `tests/test_computer_use_brain.py:191`, `tests/test_computer_use_runtime.py` | gitleaks AWS key / generic API key. | `AKIAABCDEFGHIJKLMNOP` is asserted to be *redacted* by the containing test; the other is the RFC 6455 example nonce. Placeholders, not credentials; no history rewrite. | `gitleaks detect --no-git` — all 12 hits are these fixtures or untracked `.pyc` copies. |
| FP-03 | INFO | FALSE POSITIVE | `task_journal.py:212-215` | SQL injection. | The f-string emits only `?` placeholders from a fixed enum; values are bound parameters. | `pytest` journal tests. |
| FP-04 / FP-15 | INFO | FALSE POSITIVE | video/vision/process services | Command injection. | Fixed argv lists, no `shell=True`; the one shell path is capability-gated. | Runtime tests. |
| FP-05 | INFO | FALSE POSITIVE | capsule `index.html` / `server.py` | Insecure WebSocket. | Same-origin loopback/local-preview only; the public viewer is CloudFront HTTPS. | `tests/test_agent_capsule.py`. |
| FP-06 | INFO | FALSE POSITIVE | `server.py:668` | Dangerous globals. | Hardcoded module-global name; no user input. | Code review. |
| FP-07 / FP-09 | INFO | FALSE POSITIVE | dynamic urllib / HTTPSConnection | SSRF / cert bypass. | Constant loopback, deploy-config HTTPS, or a pinned-SHA download; no attacker scheme/host. | Code review. |
| FP-08 | INFO | FALSE POSITIVE | `eval_runner.py:282` | SHA-1. | RFC 6455 `Sec-WebSocket-Accept` computation; required by spec. | Code review. |
| FP-10 | INFO | FALSE POSITIVE | fixtures/website | Django CSRF. | Static eval fixture; no Django/session. | — |
| FP-11 | INFO | FALSE POSITIVE | `workspace_service.py` | File permissions. | `0o660/0o770` after `fchown` for the deliberate human+broker share; fd-relative `O_NOFOLLOW` confinement. | Workspace tests. |
| FP-12 | INFO | FALSE POSITIVE | `index.mjs` | Unsafe format string. | Fixed label/port constants only. | — |
| FP-13 | INFO | FALSE POSITIVE | `/tmp` paths | Insecure temp. | Disposable VM-local scratch; `mkstemp` where a race matters. | — |
| FP-14 | INFO | FALSE POSITIVE | `except: pass` sites | Swallowed exceptions. | Best-effort cleanup only; security-relevant failures raise. | — |
| FP-16 | INFO | FALSE POSITIVE | `assert` in capsule | Assertion stripping. | Images do not run Python `-O`; asserts are invariant checks. | Dockerfile review. |
| FP-17 | INFO | FALSE POSITIVE | `"local"`, `"access"`, `""` | Hardcoded passwords. | Local-mode token label, Cognito `token_use`, optional defaults. | — |
| FP-18 | INFO | FALSE POSITIVE | CUD Dockerfile `ENV` | Secret in env. | File **paths** (`...KEY_FILE=/run/...`); keys are generated per VM. | Dockerfile review. |
| FP-19 | INFO | FALSE POSITIVE | `relay.yaml` internal ALB HTTP | Plaintext. | Private CloudFront VPC-origin hop asserted intentional by `tests/test_relay_origin_tls.py`; making it TLS would break that contract for zero gain. | `pytest tests/test_relay_origin_tls.py`. |
| FP-20 | INFO | FALSE POSITIVE | `relay.yaml:250` | Secret in task env. | The value is `PAIRPUTER_SESSION_SECRET_ARN` — an ARN reference resolved by the task role. | — |
| FP-21 | INFO | FALSE POSITIVE | `relay.yaml` WebACL conditional | No WAF. | Parent defaults `EnableCloudFrontWaf=true` and attaches the nested ACL; nested-only scan can't resolve it. | `tests/test_workbench_security_topology.py`. |
| FP-22 | INFO | FALSE POSITIVE | setuptools CVE | Windows-only wheel-install path escape. | Images are linux/arm64, digest-pinned, `--require-hashes`. | `requirements.txt` review. |
| I-01 / I-02 / I-03 | INFO | FALSE POSITIVE (as security) | Dockerfile/lint/style | Image hygiene, SG descriptions. | Size/reproducibility nits, no exploitable condition; opportunistic cleanup only. | `hadolint` advisory. |
| I-04 | INFO | ACCEPTED RISK | Container Insights | Observability only. | Relay already ships to its own log group. | — |
| PT-06 (WAF half) | INFO | FALSE POSITIVE | CloudFront WAF caveat | "No WAF by default". | Incorrect: `EnableCloudFrontWaf` defaults true and is attached; the real caveat is the us-east-1 WebACL constraint. | `tests/test_workbench_security_topology.py`. |
| PT-09 | INFO | INFO | relay DDB cache / CloudFront policy | 1 s staleness; blanket policy. | Bounded by the watchdog and per-session HMAC token. Deliberate. | `tests/test_relay_session_freshness.py`. |

## Verification (exact commands and results)

```
$ /opt/homebrew/bin/pytest tests/ -q -rs
1002 passed, 2 subtests passed in 11.42s        # baseline was 990 passed

$ uvx cfn-lint --non-zero-exit-code error \
    substrate/cloudformation/pairputer.yaml \
    substrate/cloudformation/nested/*.yaml ci/pairputer-ci.yaml
exit 0, zero E rows                             # was 3 x E0000 duplicate-key

$ node --check substrate/stateful-relay/index.mjs
exit 0
$ python -m py_compile <changed .py files>
exit 0
$ bash -n <changed .sh files>
exit 0
$ git ls-files '*.sh' | xargs shellcheck -S error
exit 0 (31 scripts)
$ python - <<'PY'  # PyYAML + CFN intrinsics, all templates
bad 0
PY
$ gitleaks detect --no-git --redact
12 hits — all FP-01 fixtures / untracked .pyc copies (no new secret)
$ bash tests/test_deploy_rebind.sh && bash tests/test_remove_cf.sh \
  && bash capsules/computer-use-desktop/tests/test_airgap.sh \
  && bash capsules/computer-use-desktop/tests/test_persistent_home.sh \
  && python substrate/mcp-server/test_recovery_image_id.py
all exit 0
```

New regression tests: `tests/test_ci_supply_chain.py` (action pinning, exact OIDC ref,
crane/cosign checksums, enumerated proxy ports, relay viewer handshake),
`tests/test_computer_use_runtime.py` (input-ws viewer capability, agent-key can't claim human,
Origin rejection), `tests/test_agent_capsule.py` (DOOM bridge capability/Origin),
`tests/test_manifest_stager_parity.py` (context tree hash recomputation),
`tests/test_video_stream_limit.py` (bounded viewer retirement).

## Residual risk

- **Capsule and substrate images must be rebuilt and digest-re-pinned** before the guest-side
  changes (input-ws viewer auth, DOOM bridge capability, viewer key provisioning) reach customers.
  Until then the published images are unchanged; `publish-images.yml` + `publish-launch.yml` do this
  on merge to `main`.
- **C-04** (fck-nat root volume) is not fixed: encrypting it correctly needs the AMI's root device
  name, which cannot be verified offline. C-06 (non-root substrate images) is deferred for the same
  rebuild/re-pin reason.
- **N-01** (JWT `client_id`/`scope`) remains the one defense-in-depth gap in the auth chain; it
  needs a config change enumerating all app clients and fails closed only once that config exists.
- **PT-05** (player `postMessage` origin) needs the chat-host/iframe origin allowlist coordinated
  with the widget; a partial allowlist would break controls.
- **PA-14** (capsule pip hashes / restful-doom tarball) is content-addressed today but not
  hash-pinned; deferred with the next capsule image.
- **N-03** (SAST/dependency/secret CI gate) is not added. Adding a blocking gate now would fail on
  the fixture placeholders unless allowlists and a pinned action land together.
- **N-05/PA-11** CLI argv secret exposure is operator-local and remains.
- **QA-06** branch protection is a repository setting; the code gates are meaningful only if `main`
  actually requires the CI job.
- Accepted design risk is unchanged: an internet-connected computer-use agent can exfiltrate via
  navigation (P-3), capsules are root brokers with per-job isolation (AR-01/02), and the internal
  CloudFront→ALB hop is plain HTTP inside a private VPC origin (FP-19).
- Local-dev sets `PAIRPUTER_INPUT_ALLOW_UNAUTHENTICATED_VIEWER=true`; this is a documented
  lower-assurance mode and is never set on the AWS path.
