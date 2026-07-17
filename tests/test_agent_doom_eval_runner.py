"""Product eval runner tests for Agent DOOM."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_ROOT = REPO_ROOT / "capsules" / "agent-doom"
sys.path.insert(0, str(RUNNER_ROOT))

import eval_runner  # noqa: E402


class TestAgentDoomEvalRunner(unittest.TestCase):
    def test_load_cases_accepts_commander_contract_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cases.json"
            path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "no-kill",
                                "goal": "race to the exit without killing anyone",
                                "objective": "exit_level",
                                "constraints": ["no_kills"],
                                "max_tics": 2800,
                                "episode": 1,
                                "map": 2,
                                "seed": 42,
                                "human_interrupt_after_s": 2,
                            }
                        ]
                    }
                )
            )
            cases = eval_runner.load_cases(path)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].payload()["objective"], "exit_level")
        self.assertEqual(cases[0].payload()["constraints"], ["no_kills"])
        self.assertEqual(cases[0].episode, 1)
        self.assertEqual(cases[0].map, 2)
        self.assertEqual(cases[0].seed, 42)
        self.assertEqual(cases[0].human_interrupt_after_s, 2.0)

    def test_eval_payload_ignores_viewer_human_noise_except_interrupt_cases(self):
        normal = eval_runner.EvalCase(
            "beat",
            "beat the level",
            objective="complete_level",
            max_tics=4200,
            human_interrupt_after_s=0.0,
        )
        interrupt = eval_runner.EvalCase(
            "human-interrupt",
            "beat the level",
            objective="complete_level",
            max_tics=4200,
            human_interrupt_after_s=2.0,
        )

        self.assertTrue(eval_runner._case_payload_with_wall_budget(normal, 300)["ignore_human_interrupt"])
        self.assertFalse(eval_runner._case_payload_with_wall_budget(interrupt, 300)["ignore_human_interrupt"])

    def test_eval_case_files_load_and_cover_required_axes(self):
        case_root = RUNNER_ROOT / "eval-cases"
        fast = eval_runner.load_cases(case_root / "reliability-fast-100.json")
        map_smoke = eval_runner.load_cases(case_root / "map-smoke.json")
        hard = eval_runner.load_cases(case_root / "hard-generalization.json")

        self.assertEqual(len(fast), 1)
        self.assertIn("100_run", fast[0].tags)
        self.assertTrue(fast[0].reset_episode)
        self.assertEqual(fast[0].objective, "find_enemy")

        self.assertEqual({case.map for case in map_smoke}, {1, 2, 3})
        self.assertTrue(all("map_smoke" in case.tags for case in map_smoke))
        self.assertTrue(all(case.objective == "find_enemy" for case in map_smoke))

        hard_by_id = {case.case_id: case for case in hard}
        self.assertIn("e1m2-beat-level-key-route", hard_by_id)
        self.assertIn("e1m2-no-kill-exit-key-route", hard_by_id)
        self.assertIn("e1m3-beat-level-lift-route", hard_by_id)
        self.assertIn("e1m3-punch-pinky-no-ammo", hard_by_id)
        self.assertIn("no_kills", hard_by_id["e1m2-no-kill-exit-key-route"].constraints)
        self.assertIn("fist_only", hard_by_id["e1m3-punch-pinky-no-ammo"].constraints)
        self.assertIn("locked_doors", hard_by_id["e1m2-beat-level-key-route"].tags)
        self.assertIn("lifts", hard_by_id["e1m3-beat-level-lift-route"].tags)
        self.assertIn("pinky", hard_by_id["e1m3-punch-pinky-no-ammo"].tags)

    def test_score_case_extracts_stable_scoreboard_fields(self):
        case = eval_runner.EvalCase(
            "case-a",
            "find an enemy and punch it, no ammo",
            objective="kill_enemy",
            constraints=("no_ammo", "fist_only"),
            max_tics=1600,
        )
        row = eval_runner.score_case(
            case,
            {
                "status": "success",
                "stop_reason": "enemy_killed",
                "steps": 12,
                "tics": 80,
                "progress_metrics": {"kills_delta": 1, "shots_fired": 0, "ammo_delta": 0, "health_delta": -6},
                "state": {"x": 100, "y": -200, "wp": 0},
                "human_interrupt_ms": 0,
            },
            commit="abc123",
        )
        self.assertEqual(row["commit"], "abc123")
        self.assertEqual(row["case_id"], "case-a")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["stop_reason"], "enemy_killed")
        self.assertEqual(row["kills_delta"], 1)
        self.assertEqual(row["shots_fired"], 0)
        self.assertEqual(row["final_x"], 100)
        self.assertEqual(row["final_y"], -200)
        self.assertGreater(row["response_bytes"], 20)

    def test_score_case_extracts_compact_trace_breadcrumbs(self):
        case = eval_runner.EvalCase("no-kill", "race to the exit", objective="exit_level", constraints=("no_kills",))
        row = eval_runner.score_case(
            case,
            {
                "status": "failed",
                "stop_reason": "player_dead",
                "state": {"x": 10, "y": 20, "hp": 0},
                "recent": [
                    {
                        "plan": {"action": "panic_sidestep_close_blocker", "skill": "no_kill_route_evasion", "line": 309, "sector": 74},
                        "pos": {"hp": 17},
                    },
                    {
                        "plan": {"action": "center_passable_portal_raw", "skill": "center_passable_portal", "line": 315, "sector": 78},
                        "pos": {"hp": 0},
                    },
                ],
            },
            commit="abc123",
        )
        self.assertEqual(row["final_health"], 0)
        self.assertEqual(row["last_plan_action"], "center_passable_portal_raw")
        self.assertEqual(row["last_plan_skill"], "center_passable_portal")
        self.assertEqual(row["last_plan_line"], 315)
        self.assertEqual(row["last_plan_sector"], 78)
        self.assertIn("panic_sidestep_close_blocker@309", row["recent_plan_trail"])

    def test_score_case_uses_agent_kills_for_no_kill_contracts(self):
        case = eval_runner.EvalCase(
            "no-kill",
            "race to the exit without killing anyone",
            objective="exit_level",
            constraints=("no_kills",),
        )
        row = eval_runner.score_case(
            case,
            {
                "status": "success",
                "stop_reason": "reached_exit",
                "progress_metrics": {"kills_delta": 1, "agent_kills": 0, "ammo_delta": 0, "health_delta": 0},
                "committed_contract": {"constraints": ["no_kills"]},
            },
            commit="abc123",
        )
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["agent_kills"], 0)
        self.assertEqual(row["eval_failures"], "")

    def test_parse_marked_json_supports_tmux_codex_output(self):
        parsed = eval_runner.parse_marked_json(
            'noise\nEVAL_RESULT_JSON human-interrupt {"status":"interrupted","stop_reason":"human_interrupt"}\n'
        )
        self.assertEqual(parsed["stop_reason"], "human_interrupt")

    def test_parse_marked_json_supports_wrapped_tmux_output(self):
        parsed = eval_runner.parse_marked_json(
            'Summary: objective=exit_level constraints=no_kills stop_reason=reached_exit\n'
            'EVAL_RESULT_JSON tmux-no-kill-smoke\n'
            '{"status":"success","stop_reason":"reached_exit","committed_contract":\n'
            '{"objective":"exit_level","constraints":["no_kills","avoid_combat"],"max_tics":2800},\n'
            '"progress_metrics":{"shots_fired"\n'
            ':0}}\n',
            case_id="tmux-no-kill-smoke",
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["status"], "success")
        self.assertEqual(parsed["committed_contract"]["objective"], "exit_level")
        self.assertEqual(parsed["progress_metrics"]["shots_fired"], 0)

    def test_parse_marked_json_ignores_prompt_args_after_marker_text(self):
        parsed = eval_runner.parse_marked_json(
            'Then print one final line starting with EVAL_RESULT_JSON tmux-no-kill-smoke '
            'followed by compact JSON. Args: {"goal":"race","max_tics":2800}\n',
            case_id="tmux-no-kill-smoke",
        )
        self.assertIsNone(parsed)

    def test_parse_marked_json_requires_exact_case_id_boundary(self):
        parsed = eval_runner.parse_marked_json(
            'EVAL_RESULT_JSON tmux-find-smoke {"status":"success","stop_reason":"enemy_found"}\n',
            case_id="tmux-find",
        )
        self.assertIsNone(parsed)

    def test_parse_marked_json_repairs_tmux_hard_wrap_inside_json_string(self):
        parsed = eval_runner.parse_marked_json(
            "EVAL_RESULT_JSON tmux-no-kill-smoke\n"
            '{"status":"success","progress_metrics":{"shots_fired\n'
            '  ":0},"stop_reason":"reached_exit"}\n',
            case_id="tmux-no-kill-smoke",
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["progress_metrics"]["shots_fired"], 0)

    def test_tmux_summary_ok_requires_objective_constraints_and_stop_reason(self):
        result = {
            "stop_reason": "reached_exit",
            "committed_contract": {"objective": "exit_level", "constraints": ["no_kills"]},
        }
        self.assertTrue(
            eval_runner.tmux_summary_ok(
                "Summary: objective=exit_level constraints=no_kills stop_reason=reached_exit\n"
                'EVAL_RESULT_JSON x {"status":"success"}',
                result,
            )
        )
        self.assertFalse(eval_runner.tmux_summary_ok("Summary: objective=exit_level stop_reason=reached_exit", result))

    def test_tmux_summary_ok_accepts_wrapped_summary_lines(self):
        result = {
            "stop_reason": "reached_exit",
            "committed_contract": {"objective": "exit_level", "constraints": ["no_kills", "avoid_combat"]},
        }
        self.assertTrue(
            eval_runner.tmux_summary_ok(
                "Summary: objective=exit_level constraints=[\"no_kills\",\"avoid_combat\"]\n"
                "  stop_reason=reached_exit\n"
                "  EVAL_RESULT_JSON tmux-no-kill-smoke\n",
                result,
            )
        )

    def test_tmux_send_line_pastes_via_buffer_then_submits(self):
        client = eval_runner.TmuxCodexClient(target="agent-doom-codex")
        with mock.patch.object(eval_runner.subprocess, "run") as run:
            with mock.patch.object(eval_runner.time, "sleep") as sleep:
                client._send_line("long prompt text")

        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0][:3], ["tmux", "load-buffer", "-b"])
        self.assertEqual(run.call_args_list[0].kwargs["input"], "long prompt text")
        self.assertEqual(run.call_args_list[0].kwargs["text"], True)
        self.assertIn("paste-buffer", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[2].args[0], ["tmux", "send-keys", "-t", "agent-doom-codex", "Enter"])
        sleep.assert_called_once_with(0.2)

    def test_tmux_capture_joins_wrapped_terminal_lines(self):
        client = eval_runner.TmuxCodexClient(target="agent-doom-codex")
        with mock.patch.object(eval_runner.subprocess, "run") as run:
            client._capture()
        self.assertEqual(
            run.call_args.args[0],
            ["tmux", "capture-pane", "-J", "-p", "-S", "-3000", "-t", "agent-doom-codex"],
        )

    def test_tmux_reset_episode_waits_for_clean_spawn(self):
        client = eval_runner.TmuxCodexClient(target="agent-doom-codex")
        states = [
            {"m": [1, 1, 2], "p": {"hp": 1, "bul": 50, "x": 0, "y": 0}, "k": 0},
            {
                "m": [1, 1, 2],
                "p": {
                    "hp": 100,
                    "bul": 50,
                    "x": eval_runner.E1M1_SPAWN_X_FP,
                    "y": eval_runner.E1M1_SPAWN_Y_FP,
                },
                "k": 0,
            },
        ]
        calls = []

        def fake_post(path, payload, *, timeout_s=None):
            calls.append((path, payload, timeout_s))
            if path == "/reset_episode":
                return {}
            return states.pop(0)

        client._post_json = fake_post  # type: ignore[method-assign]
        with mock.patch.object(eval_runner.time, "sleep"):
            client._reset_episode(eval_runner.EvalCase("case", "beat the level", reset_episode=True))

        self.assertEqual(calls[0][0], "/reset_episode")
        self.assertEqual([call[0] for call in calls[1:]], ["/observe", "/observe"])

    def test_tmux_runner_records_tactical_poll_transitions(self):
        client = eval_runner.TmuxCodexClient(target="agent-doom-codex", poll_interval_s=0.2)
        statuses = [
            {
                "status": "running",
                "phase": "route_progression",
                "objective": "find_enemy",
                "steps": 1,
                "tics": 8,
                "state": {"hp": 100, "m": [1, 1]},
            },
            {
                "status": "success",
                "stop_reason": "enemy_found",
                "phase": "route_progression",
                "objective": "find_enemy",
                "steps": 2,
                "tics": 16,
                "state": {"hp": 100, "m": [1, 1]},
            },
        ]
        captures = [
            eval_runner.subprocess.CompletedProcess(["tmux"], 0, stdout="working\n"),
            eval_runner.subprocess.CompletedProcess(
                ["tmux"],
                0,
                stdout=(
                    "Summary: objective=find_enemy constraints=none stop_reason=enemy_found\n"
                    'EVAL_RESULT_JSON tmux-find {"status":"success","stop_reason":"enemy_found",'
                    '"committed_contract":{"objective":"find_enemy","constraints":[],"max_tics":700},'
                    '"progress_metrics":{},"state":{"hp":100},"steps":2,"tics":16}\n'
                ),
            ),
        ]

        client._send_line = mock.Mock()  # type: ignore[method-assign]
        client._get_json = mock.Mock(side_effect=statuses)  # type: ignore[method-assign]
        client._capture = mock.Mock(side_effect=captures)  # type: ignore[method-assign]
        with mock.patch.object(eval_runner.time, "sleep"):
            result = client.run_case(eval_runner.EvalCase("tmux-find", "find an enemy", objective="find_enemy"))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["_tactical_poll_count"], 2)
        self.assertTrue(result["_tactical_stop_seen"])
        self.assertGreaterEqual(result["_tactical_stop_ms"], 0)
        self.assertEqual(result["_tactical_status"]["status"], "success")
        self.assertEqual(result["_tactical_status_transitions"][0]["status"], "running")
        self.assertEqual(result["_tactical_status_transitions"][1]["status"], "success")

    def test_score_case_fails_tmux_without_terminal_tactical_status(self):
        case = eval_runner.EvalCase("tmux", "find an enemy", objective="find_enemy")
        row = eval_runner.score_case(
            case,
            {
                "status": "success",
                "stop_reason": "enemy_found",
                "_tmux_summary_ok": True,
                "_tactical_poll_count": 1,
                "_tactical_stop_seen": False,
                "_tactical_status_transitions": [{"status": "running"}],
                "committed_contract": {"objective": "find_enemy", "constraints": []},
                "progress_metrics": {"health_delta": 0},
            },
            commit="abc123",
        )
        self.assertEqual(row["status"], "failed")
        self.assertIn("tmux_tactical_terminal_missing", row["eval_failures"])

    def test_score_case_fails_tmux_without_tactical_polling(self):
        case = eval_runner.EvalCase("tmux", "find an enemy", objective="find_enemy")
        row = eval_runner.score_case(
            case,
            {
                "status": "success",
                "stop_reason": "enemy_found",
                "_tmux_summary_ok": True,
                "_tactical_poll_count": 0,
                "committed_contract": {"objective": "find_enemy", "constraints": []},
                "progress_metrics": {"health_delta": 0},
            },
            commit="abc123",
        )
        self.assertEqual(row["status"], "failed")
        self.assertIn("tmux_tactical_poll_missing", row["eval_failures"])

    def test_direct_runner_can_request_recent_trace_for_breadcrumbs(self):
        client = eval_runner.DirectBridgeClient(trace_recent=32)
        posts = []

        def fake_post(path, payload, *, timeout_s=None):
            posts.append((path, payload, timeout_s))
            return {"status": "success", "stop_reason": "reached_exit", "progress_metrics": {}}

        client._post = fake_post  # type: ignore[method-assign]
        client._get = lambda path: {}  # type: ignore[method-assign]

        client.run_case(eval_runner.EvalCase("case", "race to the exit", objective="exit_level"))

        self.assertEqual(posts[0][0], "/brain/drive_goal")
        self.assertEqual(posts[0][1]["trace_recent"], 32)
        self.assertIn("max_wall_s", posts[0][1])

    def test_runner_goal_wall_budget_stays_below_timeout(self):
        self.assertEqual(eval_runner._runner_goal_wall_budget(120), 115)
        self.assertEqual(eval_runner._runner_goal_wall_budget(3), 1)

    def test_direct_runner_recovers_after_goal_timeout(self):
        client = eval_runner.DirectBridgeClient(timeout_s=0.2, poll_interval_s=0.2)
        cleanup_called = []

        def fake_post(path, payload, *, timeout_s=None):
            if path == "/brain/drive_goal":
                self.assertEqual(payload["max_wall_s"], 1)
                time.sleep(1.5)
                return {"status": "success", "stop_reason": "late", "progress_metrics": {}, "state": {}}
            cleanup_called.append(path)
            return {"ok": True}

        client._post = fake_post  # type: ignore[method-assign]
        client._get = lambda path: {"status": "running", "phase": "route_progression"}  # type: ignore[method-assign]
        client._wait_after_timeout = lambda thread, case: cleanup_called.append("timeout")  # type: ignore[method-assign]

        result = client.run_case(eval_runner.EvalCase("case", "beat the level", reset_episode=False))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop_reason"], "runner_error")
        self.assertIn("timeout", cleanup_called)

    def test_direct_runner_polls_tactical_status_while_goal_runs(self):
        client = eval_runner.DirectBridgeClient(poll_interval_s=0.2)
        release_goal = eval_runner.threading.Event()
        get_calls = []

        def fake_post(path, payload, *, timeout_s=None):
            self.assertEqual(path, "/brain/drive_goal")
            release_goal.wait(timeout=1)
            return {
                "status": "success",
                "stop_reason": "enemy_found",
                "committed_contract": {"objective": "find_enemy", "constraints": [], "max_tics": 700},
                "progress_metrics": {},
                "state": {"hp": 100},
                "steps": 2,
                "tics": 16,
            }

        def fake_get(path):
            self.assertEqual(path, "/brain/tactical_status")
            get_calls.append(path)
            if len(get_calls) == 1:
                release_goal.set()
                return {
                    "status": "running",
                    "phase": "route_progression",
                    "objective": "find_enemy",
                    "steps": 1,
                    "tics": 8,
                    "state": {"hp": 100, "m": [1, 1]},
                }
            return {
                "status": "success",
                "stop_reason": "enemy_found",
                "phase": "route_progression",
                "objective": "find_enemy",
                "steps": 2,
                "tics": 16,
                "state": {"hp": 100, "m": [1, 1]},
            }

        client._post = fake_post  # type: ignore[method-assign]
        client._get = fake_get  # type: ignore[method-assign]

        result = client.run_case(eval_runner.EvalCase("case", "find an enemy", objective="find_enemy"))

        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(result["_tactical_poll_count"], 2)
        self.assertTrue(result["_tactical_stop_seen"])
        self.assertEqual(result["_tactical_status"]["status"], "success")
        self.assertEqual(result["_tactical_status_transitions"][0]["status"], "running")
        self.assertEqual(result["_tactical_status_transitions"][-1]["status"], "success")

    def test_trace_recent_debug_bytes_do_not_trip_product_response_budget(self):
        client = eval_runner.DirectBridgeClient(trace_recent=32)

        def fake_post(path, payload, *, timeout_s=None):
            return {
                "status": "success",
                "stop_reason": "reached_exit",
                "steps": 1,
                "tics": 8,
                "progress_metrics": {"agent_kills": 0, "ammo_delta": 0, "health_delta": 0},
                "state": {"x": 1, "y": 2, "hp": 100},
                "recent": [{"plan": {"action": "x" * 1000, "line": 1}, "pos": {"hp": 100}}],
            }

        client._post = fake_post  # type: ignore[method-assign]
        client._get = lambda path: {}  # type: ignore[method-assign]

        result = client.run_case(eval_runner.EvalCase("case", "race to the exit", objective="exit_level"))
        self.assertGreater(result["_debug_response_bytes"], result["_bridge_response_bytes"])
        row = eval_runner.score_case(
            eval_runner.EvalCase("case", "race to the exit", objective="exit_level"),
            result,
            commit="abc123",
        )
        self.assertEqual(row["debug_response_bytes"], result["_debug_response_bytes"])
        self.assertEqual(row["status"], "success")

    def test_clean_episode_start_requires_spawn_state(self):
        self.assertTrue(
            eval_runner._is_clean_episode_start(
                {
                    "m": [1, 1, 2],
                    "p": {"hp": 100, "bul": 50, "x": eval_runner.E1M1_SPAWN_X_FP, "y": eval_runner.E1M1_SPAWN_Y_FP},
                    "k": 0,
                    "t": "10",
                }
            )
        )
        self.assertFalse(
            eval_runner._is_clean_episode_start(
                {"m": [1, 1, None], "p": {"hp": 18, "bul": 50}, "k": 1, "t": "17993"}
            )
        )
        self.assertFalse(
            eval_runner._is_clean_episode_start(
                {"m": [1, 1, 2], "p": {"hp": 100, "bul": 50, "x": 0, "y": 0}, "k": 0, "t": "10"}
            )
        )
        self.assertTrue(
            eval_runner._is_clean_episode_start(
                {"m": [1, 2, 2], "p": {"hp": 100, "bul": 50, "x": 0, "y": 0}, "k": 0, "t": "10"},
                episode=1,
                map_id=2,
            )
        )

    def test_write_outputs_writes_jsonl_and_csv(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rows = [{"case_id": "a", "status": "success", "stop_reason": "objective_achieved"}]
            eval_runner.write_outputs(rows, jsonl=root / "score.jsonl", csv_path=root / "score.csv")
            self.assertIn('"case_id": "a"', (root / "score.jsonl").read_text())
            self.assertIn("stop_reason", (root / "score.csv").read_text())

    def test_load_existing_rows_skips_bad_lines_for_resume(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "score.jsonl"
            path.write_text('{"iteration":1,"case_id":"a","status":"success"}\nnot json\n{"iteration":2,"case_id":"b"}\n')
            rows = eval_runner.load_existing_rows(path)

        self.assertEqual([eval_runner.row_key(row) for row in rows], [(1, "a"), (2, "b")])

    def test_summarize_rows_reports_success_rate_and_failure_positions(self):
        rows = [
            {
                "case_id": "a",
                "status": "success",
                "tics": 30,
                "elapsed_ms": 1000,
                "health_delta": 0,
                "response_bytes": 200,
                "response_budget_bytes": 500,
                "tactical_poll_count": 3,
                "tactical_stop_seen": True,
            },
            {
                "case_id": "a",
                "status": "success",
                "tics": 50,
                "elapsed_ms": 3000,
                "health_delta": -2,
                "response_bytes": 501,
                "response_budget_bytes": 500,
                "tactical_poll_count": 0,
                "tactical_stop_seen": "",
            },
            {
                "case_id": "a",
                "status": "failed",
                "stop_reason": "player_dead",
                "iteration": 3,
                "final_x": 10,
                "final_y": 20,
                "final_health": 0,
                "last_plan_action": "center_passable_portal_raw",
                "last_plan_line": 315,
                "recent_plan_trail": "panic_sidestep_close_blocker@309 > center_passable_portal_raw@315",
                "health_delta": -100,
                "elapsed_ms": 5000,
                "response_bytes": 450,
                "response_budget_bytes": 500,
                "tactical_poll_count": 2,
                "tactical_stop_seen": False,
            },
        ]
        summary = eval_runner.summarize_rows(rows)
        self.assertEqual(summary["total_runs"], 3)
        self.assertEqual(summary["cases"]["a"]["successes"], 2)
        self.assertEqual(summary["cases"]["a"]["median_tics"], 40)
        self.assertEqual(summary["cases"]["a"]["median_elapsed_ms"], 3000)
        self.assertEqual(summary["cases"]["a"]["response_budget_violations"], 1)
        self.assertEqual(summary["cases"]["a"]["max_response_bytes"], 501)
        self.assertEqual(summary["cases"]["a"]["tactical_poll_missing"], 1)
        self.assertEqual(summary["cases"]["a"]["tactical_terminal_missing"], 1)
        self.assertEqual(summary["cases"]["a"]["failures_by_reason"], {"player_dead": 1})
        self.assertEqual(summary["cases"]["a"]["failure_positions"][0]["x"], 10)
        self.assertEqual(summary["cases"]["a"]["failure_positions"][0]["plan"], "center_passable_portal_raw")
        self.assertEqual(summary["cases"]["a"]["failure_positions"][0]["line"], 315)
        self.assertEqual(
            summary["cases"]["a"]["failure_clusters"],
            {"player_dead|center_passable_portal_raw|line:315": 1},
        )
        self.assertEqual(summary["reliability"]["response_budget_violations"], 1)
        self.assertEqual(summary["reliability"]["max_response_bytes"], 501)
        self.assertEqual(summary["reliability"]["tactical_poll_missing"], 1)
        self.assertEqual(summary["reliability"]["tactical_terminal_missing"], 1)
        self.assertEqual(summary["reliability"]["worst_health_delta"], -100)
        self.assertEqual(summary["reliability"]["failures_by_reason"], {"player_dead": 1})
        self.assertEqual(summary["reliability"]["failure_clusters"], {"player_dead|center_passable_portal_raw|line:315": 1})

    def test_main_resume_skips_completed_case_iteration_pairs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cases_path = root / "cases.json"
            jsonl_path = root / "score.jsonl"
            csv_path = root / "score.csv"
            summary_path = root / "summary.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {"id": "a", "goal": "beat the level", "objective": "complete_level"},
                            {"id": "b", "goal": "find an enemy", "objective": "find_enemy"},
                        ]
                    }
                )
            )
            jsonl_path.write_text(json.dumps({"iteration": 1, "case_id": "a", "status": "success"}) + "\n")
            calls = []

            class FakeClient:
                def run_case(self, case):
                    calls.append(case.case_id)
                    return {"status": "success", "stop_reason": "ok", "progress_metrics": {}, "state": {}}

            with mock.patch.object(eval_runner, "make_client", return_value=FakeClient()):
                code = eval_runner.main(
                    [
                        "--mode",
                        "direct",
                        "--cases",
                        str(cases_path),
                        "--repeat",
                        "2",
                        "--resume",
                        "--jsonl",
                        str(jsonl_path),
                        "--csv",
                        str(csv_path),
                        "--summary-json",
                        str(summary_path),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(calls, ["b", "a", "b"])
            rows = eval_runner.load_existing_rows(jsonl_path)
            self.assertEqual([eval_runner.row_key(row) for row in rows], [(1, "a"), (1, "b"), (2, "a"), (2, "b")])

    def test_main_keyboard_interrupt_preserves_partial_scoreboard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cases_path = root / "cases.json"
            jsonl_path = root / "score.jsonl"
            csv_path = root / "score.csv"
            summary_path = root / "summary.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {"id": "a", "goal": "find an enemy", "objective": "find_enemy"},
                            {"id": "b", "goal": "beat the level", "objective": "complete_level"},
                        ]
                    }
                )
            )
            calls = []

            class FakeClient:
                def run_case(self, case):
                    calls.append(case.case_id)
                    if case.case_id == "b":
                        raise KeyboardInterrupt()
                    return {"status": "success", "stop_reason": "enemy_found", "progress_metrics": {}, "state": {}}

            with mock.patch.object(eval_runner, "make_client", return_value=FakeClient()):
                code = eval_runner.main(
                    [
                        "--mode",
                        "direct",
                        "--cases",
                        str(cases_path),
                        "--jsonl",
                        str(jsonl_path),
                        "--csv",
                        str(csv_path),
                        "--summary-json",
                        str(summary_path),
                    ]
                )

            self.assertEqual(code, 130)
            self.assertEqual(calls, ["a", "b"])
            rows = eval_runner.load_existing_rows(jsonl_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["case_id"], "a")
            summary = json.loads(summary_path.read_text())
            self.assertTrue(summary["interrupted"])
            self.assertEqual(summary["total_runs"], 1)
            self.assertIn("case_id", csv_path.read_text())

    def test_score_case_fails_missing_tmux_summary(self):
        case = eval_runner.EvalCase("tmux", "beat the level", objective="complete_level")
        row = eval_runner.score_case(
            case,
            {
                "status": "success",
                "stop_reason": "reached_exit",
                "_tmux_summary_ok": False,
                "progress_metrics": {"health_delta": 0},
            },
            commit="abc123",
        )
        self.assertEqual(row["status"], "failed")
        self.assertIn("tmux_summary_missing", row["eval_failures"])

    def test_score_case_uses_tmux_driver_response_budget_not_augmented_runner_payload(self):
        case = eval_runner.EvalCase("tmux", "beat the level", objective="complete_level")
        row = eval_runner.score_case(
            case,
            {
                "status": "success",
                "stop_reason": "reached_exit",
                "_driver_response_bytes": 365,
                "_tactical_status": {"large": "x" * 2000},
                "progress_metrics": {"health_delta": 0},
            },
            commit="abc123",
        )
        self.assertEqual(row["response_bytes"], 365)
        self.assertEqual(row["status"], "success")

    def test_score_case_extracts_tactical_status_fields(self):
        case = eval_runner.EvalCase("tmux", "beat the level", objective="complete_level")
        row = eval_runner.score_case(
            case,
            {
                "status": "success",
                "stop_reason": "reached_exit",
                "_tactical_status": {"status": "stopped", "phase": "press_exit"},
                "_eval_elapsed_ms": 1234,
                "progress_metrics": {"health_delta": 0},
            },
            commit="abc123",
        )
        self.assertEqual(row["tactical_status"], "stopped")
        self.assertEqual(row["tactical_phase"], "press_exit")
        self.assertEqual(row["elapsed_ms"], 1234)


if __name__ == "__main__":
    unittest.main()
