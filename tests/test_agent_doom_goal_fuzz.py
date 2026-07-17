"""Free-form goal fuzz corpus tests for Agent DOOM."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_ROOT = REPO_ROOT / "capsules" / "agent-doom"
sys.path.insert(0, str(RUNNER_ROOT))

import goal_fuzz  # noqa: E402


class TestAgentDoomGoalFuzz(unittest.TestCase):
    def test_corpus_has_product_scale_and_passes(self):
        cases = goal_fuzz.load_cases(goal_fuzz.DEFAULT_CORPUS)
        self.assertGreaterEqual(len(cases), 50)
        rows = goal_fuzz.evaluate_cases(cases)
        failed = [row for row in rows if not row["ok"]]
        self.assertEqual(failed, [])

    def test_pacifist_does_not_trigger_fist_only(self):
        row = goal_fuzz.evaluate_case(
            {
                "id": "pacifist",
                "goal": "make a run for the exit, pacifist style",
                "expected": {"objective": "exit_level", "style": "speedrun", "constraints": ["no_kills"], "absent_constraints": ["fist_only"]},
            }
        )
        self.assertTrue(row["ok"], row)

    def test_filter_cases_supports_limit_and_ids(self):
        cases = [
            {"id": "a", "goal": "find enemy"},
            {"id": "b", "goal": "race to exit"},
            {"id": "c", "goal": "punch enemy"},
        ]
        self.assertEqual([case["id"] for case in goal_fuzz.filter_cases(cases, ids="b,c", limit=1)], ["b"])
        self.assertEqual([case["id"] for case in goal_fuzz.filter_cases(cases, limit=2)], ["a", "b"])

    def test_tmux_goal_fuzz_client_scores_marked_drive_goal_contract(self):
        case = {
            "id": "exit-no-kill-live",
            "goal": "make a run for the exit, pacifist style",
            "expected": {"objective": "exit_level", "style": "speedrun", "constraints": ["no_kills", "avoid_combat"]},
        }
        client = goal_fuzz.TmuxGoalFuzzClient(target="agent-doom-codex", timeout_s=1.0, max_tics=1)
        client._send_line = mock.Mock()  # type: ignore[method-assign]

        def fake_capture():
            sent = client._send_line.call_args.args[0]
            marker = sent.split("EVAL_RESULT_JSON ", 1)[1].split(" followed", 1)[0]
            return goal_fuzz.subprocess.CompletedProcess(
                ["tmux"],
                0,
                stdout=(
                    'EVAL_RESULT_JSON exit-no-kill-live {"status":"failed","stop_reason":"stale",'
                    '"committed_contract":{"objective":"exit_level","constraints":["no_kills"],"max_tics":1}}\n'
                    f'{goal_fuzz.RESULT_MARKER} {marker} {{"status":"failed","stop_reason":"max_tics_exceeded",'
                    '"committed_contract":{"objective":"exit_level","style":"speedrun",'
                    '"constraints":["no_kills","avoid_combat"],"max_tics":1}}}\n'
                ),
            )

        client._capture = mock.Mock(side_effect=fake_capture)  # type: ignore[method-assign]

        row = client.run_case(case)

        self.assertTrue(row["ok"], row)
        self.assertEqual(row["source"], "tmux-codex")
        self.assertTrue(str(row["marker_id"]).startswith("exit-no-kill-live-"))
        self.assertEqual(row["driver_status"], "failed")
        self.assertEqual(row["stop_reason"], "max_tics_exceeded")
        self.assertGreater(row["response_bytes"], 50)

    def test_tmux_goal_fuzz_client_reports_contract_mismatch(self):
        case = {
            "id": "melee-live",
            "goal": "go find bad guy and punch him down - don't use any ammo",
            "expected": {"objective": "kill_enemy", "style": "melee", "constraints": ["no_ammo", "fist_only"]},
        }
        client = goal_fuzz.TmuxGoalFuzzClient(target="agent-doom-codex", timeout_s=1.0, max_tics=1)
        client._send_line = mock.Mock()  # type: ignore[method-assign]

        def fake_capture():
            sent = client._send_line.call_args.args[0]
            marker = sent.split("EVAL_RESULT_JSON ", 1)[1].split(" followed", 1)[0]
            return goal_fuzz.subprocess.CompletedProcess(
                ["tmux"],
                0,
                stdout=(
                    f'{goal_fuzz.RESULT_MARKER} {marker} {{"status":"failed","stop_reason":"max_tics_exceeded",'
                    '"committed_contract":{"objective":"find_enemy","style":"balanced",'
                    '"constraints":[],"max_tics":1}}}\n'
                ),
            )

        client._capture = mock.Mock(side_effect=fake_capture)  # type: ignore[method-assign]

        row = client.run_case(case)

        self.assertFalse(row["ok"], row)
        self.assertIn("objective:find_enemy!=kill_enemy", row["failures"])
        self.assertIn("missing_constraint:no_ammo", row["failures"])


if __name__ == "__main__":
    unittest.main()
