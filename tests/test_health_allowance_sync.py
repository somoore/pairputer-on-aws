"""Lock the avoid_damage health allowance in sync across its two definitions.

The capsule (contract_eval.py) and the host-side eval runner (eval_runner.py) cannot
share an import, so the 9hp allowance is duplicated. If this test fails, update both
sites AND the prose in capsules/agent-doom/EVALS.md.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_avoid_damage_allowance_in_sync():
    capsule = (REPO_ROOT / "capsules/agent-doom/rootfs/opt/capsule/contract_eval.py").read_text()
    runner = (REPO_ROOT / "capsules/agent-doom/eval_runner.py").read_text()

    allowance = re.search(r"^PRESERVE_HEALTH_DAMAGE_ALLOWANCE = (\d+)$", capsule, re.M)
    assert allowance, "PRESERVE_HEALTH_DAMAGE_ALLOWANCE not found in contract_eval.py"

    gate = re.search(r'"avoid_damage" in constraints and row\["health_delta"\] < -(\d+)', runner)
    assert gate, "avoid_damage gate not found in eval_runner.py — update this test's pattern"

    assert int(gate.group(1)) == int(allowance.group(1))
