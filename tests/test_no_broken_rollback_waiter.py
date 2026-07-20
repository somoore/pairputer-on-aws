"""Regression guard: `aws cloudformation wait stack-rollback-complete` must never be used.

That waiter's ONLY success acceptor is UPDATE_ROLLBACK_COMPLETE (an *update* rollback). A failed
CREATE terminates at ROLLBACK_COMPLETE, which matches no acceptor at all, so the waiter blind-polls
its full 120 x 30s = 60 minutes before failing — behind `|| true` it reads as "rollback settled"
while stalling every create-flake retry by an hour, and any command sequenced after it fires an hour
late at whatever stack then holds the name (observed live 2026-07-20: a delayed delete-stack aimed at
a re-created stack). Use hb_wait_stack_settled (substrate/lib/aws-env.sh) instead.
"""
import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]


def tracked_shell_scripts():
    out = subprocess.run(["git", "ls-files", "*.sh"], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / p for p in out.stdout.split()]


def test_no_stack_rollback_complete_waiter():
    offenders = []
    for path in tracked_shell_scripts():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "wait stack-rollback-complete" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(REPO)}:{n}")
    assert not offenders, (
        "broken waiter (never matches a CREATE rollback; stalls 60 min) — use hb_wait_stack_settled: "
        + ", ".join(offenders))


def test_settled_helper_exists():
    lib = (REPO / "substrate/lib/aws-env.sh").read_text()
    assert "hb_wait_stack_settled()" in lib
    for user in ["substrate/deploy.sh", "substrate/deploy-capsule.sh"]:
        assert "hb_wait_stack_settled" in (REPO / user).read_text(), user + " lost the settled helper"


if __name__ == "__main__":
    test_no_stack_rollback_complete_waiter()
    test_settled_helper_exists()
    print("OK: no broken rollback waiter")
