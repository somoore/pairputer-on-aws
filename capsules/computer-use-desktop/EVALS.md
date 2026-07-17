# Pairputer Workbench evaluations

The evaluation contract tests the runtime around a host model, not a claim that the capsule contains a
better model. Every run records the capsule version, task/seed, harness/model version when applicable,
bounded action events, approvals, final-state grader output, evidence hashes, metrics, and redacted
failure diagnostics.

## Ladder

1. Unit/property tests cover task contracts, state transitions, workspace confinement, exact approvals,
   epoch rejection, input receipts, held-state release, idempotency, redaction, journal recovery, and
   display transforms.
2. Direct in-guest tests call the private service and brain with deterministic fixtures.
3. Bridge tests call `:6905` and assert the capsule-owned result envelope and evidence.
4. Local MCP tests verify manifest registration, namespacing, hard binding, typed schemas, and hot-add.
5. Docker workflow tests exercise the visible desktop, CDP, AT-SPI, XTEST, media, readiness, takeover,
   and service restart/freeze barriers.
6. Deployed tests cover authenticated proxying, tag/SSM discovery, relay/host paths, freeze/thaw,
   independent deletion, and at least two supported hosts.
7. Long soaks and external benchmarks run only after deterministic gates are green.

## Deterministic gates

- No task reaches `SUCCEEDED` without evidence for every required predicate.
- Workspace traversal, absolute-path, symlink, mount, and reserved-path escapes are rejected.
- Every mutation carries an action/task ID, idempotency key, expected human epoch, and expected world
  revision; stale values reject before commit.
- Exact approvals are single-use and invalidated by action, target/data, epoch, revision, or expiry.
- Untrusted webpage/document/terminal/code/filename content cannot revise scope or grant authority.
- No queued action starts after human epoch revocation; held agent keys/buttons are released.
- Unknown-outcome external/destructive effects are not automatically retried.
- Browser debugging, gRPC, D-Bus/control sockets, and journals are not externally exposed.
- Agent DOOM and generic substrate tests stay green.

## Local commands

```bash
pytest -q tests/test_computer_use_runtime.py tests/test_computer_use_brain.py
python3 capsules/computer-use-desktop/workbench_eval_runner.py --mode direct
docker build --platform linux/arm64 \
  -t pairputer-capsule-computer-use-desktop:local capsules/computer-use-desktop
CAPSULE=computer-use-desktop substrate/local-dev.sh --capsule-only
python3 capsules/computer-use-desktop/workbench_eval_runner.py \
  --mode bridge --base-url http://127.0.0.1:6905
```

## Co-control and continuity gates

Randomized takeover injection covers cursor movement, mouse down/up, drag, visible typing with held
modifiers, target resolution, atomic file rename, command execution, navigation, approvals, idle resume,
and post-thaw reconciliation. The release targets `<100 ms p95` from relay receipt for chunked input,
zero stuck input in 10,000 races, no post-revocation queued effect, and no overwritten human edit.

Freeze/thaw testing inserts barriers before preparation, during interruptible work, after commit before
verification, while waiting, and after success. Approvals and target handles expire; committed
idempotency keys and evidence survive.

## Competitive evaluation

Arms A/B/C/D use the same model, desktop snapshot, task wording, resolution, policy, budgets, and run
count: provider-recommended computer-use loop; Pairputer Tier 1 only; Pairputer semantic tools; and full
semantic plus brain/verification/recovery. Report strict and partial completion, false success, safety,
turns/actions/screenshots/tokens/time, recovery, interventions, approval correctness, and confidence
intervals. Do not publish superiority language until the preregistered thresholds in the implementation
plan pass against contemporaneous baselines.
