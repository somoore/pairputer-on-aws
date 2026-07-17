"""Triggered fake VLM perception tests for Agent DOOM."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from brain_runtime import BrainRuntime, parse_directive  # noqa: E402
from frame_sampler import FrameSampler  # noqa: E402
import vision_brain  # noqa: E402
from vision_adapter import build_model, save_model  # noqa: E402
from vision_brain import AdapterVisionProvider, LlamaServerVisionProvider, VisionBrain  # noqa: E402
from vision_state import build_observation, compact_status, empty_state, is_stale, write_state  # noqa: E402


class FakeAgentPb2:
    ACTION_FORWARD = 1
    ACTION_BACKWARD = 2
    ACTION_TURN_LEFT = 3
    ACTION_TURN_RIGHT = 4
    ACTION_STRAFE_LEFT = 5
    ACTION_STRAFE_RIGHT = 6
    ACTION_SHOOT = 7
    ACTION_USE = 8


class FakeVision:
    def __init__(self):
        self.calls = []

    def request(self, trigger, context):
        self.calls.append((trigger, context))
        return {"provider": "fake", "requests": len(self.calls), "last_trigger": trigger}

    def status(self):
        return {"provider": "fake", "requests": len(self.calls), "stale": False}

    def reset(self):
        self.calls.clear()


class ExplodingVision:
    def request(self, _trigger, _context):
        raise RuntimeError("vision down")


class FakeOpenAIServer:
    def __init__(self, content):
        self.content = content
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                size = int(self.headers.get("Content-Length", "0"))
                owner.requests.append(json.loads(self.rfile.read(size) or b"{}"))
                body = json.dumps({"choices": [{"message": {"content": owner.content}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _fmt, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.server.shutdown()
        self.thread.join(timeout=5)


def modules():
    return {
        "agent_pb2": FakeAgentPb2,
        "summarize_action": lambda action: {"action": int(getattr(action, "action", 0) or 0), "raw": {}, "mouse": {}},
    }


def metrics(**overrides):
    data = {
        "health": 100,
        "visible_enemy": False,
        "shootable": False,
        "exit_line": False,
        "exit_dist": 0,
    }
    data.update(overrides)
    return data


def state(*, forward_open=True, front_fp=128 * 65536):
    return SimpleNamespace(
        navigation=SimpleNamespace(
            forward_open=forward_open,
            front_block_distance_fp=front_fp,
        )
    )


class TestVisionState(unittest.TestCase):
    def test_compact_status_omits_raw_observation_payload(self):
        observation = build_observation(
            provider="fake",
            trigger="stuck_same_coordinates",
            confidence=0.25,
            context={"raw_prompt": "describe every pixel", "x": 10},
            artifact_path="/tmp/vision_events/frame.jpg",
            ts_ms=int(time.time() * 1000),
        )
        status = compact_status({"enabled": True, "provider": "fake", "status": "ok", "requests": 1, "last": observation})
        encoded = json.dumps(status)
        self.assertIn("stuck_same_coordinates", encoded)
        self.assertIn("frame.jpg", encoded)
        self.assertNotIn("/tmp/vision_events", encoded)
        self.assertNotIn("raw_prompt", encoded)
        self.assertNotIn("describe every pixel", encoded)
        self.assertNotIn("facts", encoded)

    def test_stale_perception_is_ignored(self):
        old = build_observation(
            provider="fake",
            trigger="manual",
            confidence=0.25,
            ts_ms=int((time.time() - 30) * 1000),
        )
        self.assertTrue(is_stale(old, ttl_s=1.0))
        status = compact_status({"enabled": True, "provider": "fake", "status": "ok", "requests": 1, "last": old}, ttl_s=1.0)
        self.assertTrue(status["stale"])
        self.assertEqual(status["status"], "stale")


class TestVisionBrain(unittest.TestCase):
    def test_fake_provider_writes_bounded_local_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            event_dir = root / "events"
            state_path = root / "vision_state.json"

            def runner(cmd, **_kwargs):
                Path(cmd[-1]).write_bytes(b"fake-jpeg")
                return subprocess.CompletedProcess(cmd, 0, b"", b"")

            sampler = FrameSampler(event_dir=event_dir, max_events=2, runner=runner)
            brain = VisionBrain(state_path=state_path, event_dir=event_dir, sampler=sampler, cooldown_s=0.0, max_events=2)
            for _ in range(6):
                brain.request("manual", {"step": _})

            files = sorted(p.name for p in event_dir.iterdir())
            self.assertLessEqual(len(files), 4)
            self.assertTrue(any(name.endswith(".jpg") for name in files))
            self.assertTrue(any(name.endswith(".json") for name in files))
            status = brain.status()
            encoded = json.dumps(status)
            self.assertLess(len(encoded), 500)
            self.assertNotIn("fake-jpeg", encoded)
            self.assertNotIn("/events/", encoded)

    def test_fresh_observation_returns_none_for_stale_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brain = VisionBrain(state_path=root / "vision_state.json", event_dir=root / "events", cooldown_s=0.0, ttl_s=0.01)
            old = build_observation(provider="fake", trigger="manual", confidence=0.25, ts_ms=int((time.time() - 1) * 1000))
            stale_state = empty_state(provider="fake")
            stale_state.update({"status": "ok", "requests": 1, "last": old})
            write_state(stale_state, root / "vision_state.json")
            self.assertIsNone(brain.fresh_observation())

    def test_llama_server_provider_normalizes_schema_and_omits_raw_text(self):
        content = json.dumps(
            {
                "is_enemy_visible": True,
                "is_door_visible": False,
                "is_switch_visible": False,
                "is_wall_blocking": False,
                "is_exit_visible": False,
                "hazard": "enemy",
                "confidence": 0.77,
                "short_reason": "enemy visible",
                "extra": "ignored",
            }
        )
        with tempfile.TemporaryDirectory() as td, FakeOpenAIServer(content) as server:
            image = Path(td) / "frame.jpg"
            image.write_bytes(b"fake jpg bytes")
            provider = LlamaServerVisionProvider(server_url=server.url, model="fake-model", timeout_s=5, image_marker="<image>\n")
            observation = provider.analyze(
                trigger="no_kill_speedrun_damage",
                context={"raw_prompt": "do not leak", "step": 12},
                artifact_path=str(image),
            )
        self.assertEqual(observation["provider"], "llama_server")
        self.assertEqual(observation["facts"]["hazard"], "enemy")
        self.assertTrue(observation["facts"]["is_enemy_visible"])
        self.assertEqual(observation["confidence"], 0.77)
        self.assertEqual(observation["context"]["vlm_model"], "fake-model")
        encoded = json.dumps(observation)
        self.assertNotIn("extra", encoded)
        self.assertNotIn("fake jpg bytes", encoded)
        self.assertEqual(server.requests[0]["response_format"], {"type": "json_object"})
        self.assertTrue(server.requests[0]["messages"][0]["content"][0]["text"].startswith("<image>\n"))
        self.assertIn("image pixels first", server.requests[0]["messages"][0]["content"][0]["text"])
        self.assertIn("classic DOOM", server.requests[0]["messages"][0]["content"][0]["text"])
        self.assertTrue(server.requests[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_llama_server_provider_can_use_caption_fallback(self):
        with tempfile.TemporaryDirectory() as td, FakeOpenAIServer("There is a person in a doorway holding a gun.") as server:
            image = Path(td) / "frame.jpg"
            image.write_bytes(b"fake jpg bytes")
            provider = LlamaServerVisionProvider(
                server_url=server.url,
                model="fake-model",
                timeout_s=5,
                allow_caption_fallback=True,
            )
            observation = provider.analyze(
                trigger="no_kill_speedrun_damage",
                context={"step": 12},
                artifact_path=str(image),
            )
        self.assertTrue(observation["facts"]["is_enemy_visible"])
        self.assertEqual(observation["facts"]["hazard"], "enemy")

    def test_llama_server_provider_caption_prompt_mode(self):
        with tempfile.TemporaryDirectory() as td, FakeOpenAIServer("There is a person in a doorway holding a gun.") as server:
            image = Path(td) / "frame.jpg"
            image.write_bytes(b"fake jpg bytes")
            provider = LlamaServerVisionProvider(
                server_url=server.url,
                model="fake-model",
                timeout_s=5,
                allow_caption_fallback=True,
                prompt_mode="caption",
            )
            provider.analyze(trigger="no_kill_speedrun_damage", context={"step": 12}, artifact_path=str(image))
        prompt = server.requests[0]["messages"][0]["content"][0]["text"]
        self.assertIn("visible classic DOOM gameplay objects", prompt)
        self.assertNotIn("Return exactly this schema", prompt)

    def test_adapter_provider_uses_tiny_local_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            image = root / "frame.jpg"
            image.write_bytes(b"fake jpg bytes")
            model = build_model(
                [
                    {
                        "id": "enemy-frame",
                        "image": "frame.jpg",
                        "trigger": "manual",
                        "features": [0.1, 0.9, 0.2],
                        "labels": {"is_enemy_visible": True, "is_wall_blocking": False},
                    }
                ],
                name="unit-adapter",
                top_k=1,
            )
            model_path = root / "adapter.json"
            save_model(model, model_path)
            original = vision_brain.features_from_file
            vision_brain.features_from_file = lambda _path, size=(32, 32): [0.1, 0.9, 0.2]
            try:
                provider = AdapterVisionProvider(model_path=model_path)
                observation = provider.analyze(trigger="manual", context={"step": 1}, artifact_path=str(image))
            finally:
                vision_brain.features_from_file = original
        self.assertEqual(observation["provider"], "adapter")
        self.assertTrue(observation["facts"]["is_enemy_visible"])
        self.assertEqual(observation["facts"]["hazard"], "enemy")
        self.assertEqual(observation["context"]["adapter_model"], "unit-adapter")


class TestBrainVisionTriggers(unittest.TestCase):
    def test_fake_provider_called_when_stuck(self):
        runtime = BrainRuntime()
        runtime._vision = FakeVision()
        directive = parse_directive({"goal": "explore"})
        hint = runtime._maybe_request_vision(
            directive,
            object(),
            state(forward_open=False),
            SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD),
            {"skill": "route_progression"},
            modules(),
            step=3,
            stuck_steps=3,
            step_moved=0.0,
            previous_metrics=metrics(),
            current_metrics=metrics(),
        )
        self.assertIsNotNone(hint)
        self.assertEqual(runtime._vision.calls[0][0], "stuck_same_coordinates")

    def test_no_calls_during_normal_combat(self):
        runtime = BrainRuntime()
        runtime._vision = FakeVision()
        directive = parse_directive({"goal": "find an enemy and shoot"})
        hint = runtime._maybe_request_vision(
            directive,
            object(),
            state(forward_open=False),
            SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD),
            {"skill": "engage"},
            modules(),
            step=3,
            stuck_steps=4,
            step_moved=0.0,
            previous_metrics=metrics(visible_enemy=True),
            current_metrics=metrics(shootable=True),
        )
        self.assertIsNone(hint)
        self.assertEqual(runtime._vision.calls, [])

    def test_repeated_failed_use_triggers_vision(self):
        runtime = BrainRuntime()
        runtime._vision = FakeVision()
        runtime._door_memory.record_attempt(42, status="planner_use")
        runtime._door_memory.record_attempt(42, status="planner_use")
        directive = parse_directive({"goal": "open the door"})
        runtime._maybe_request_vision(
            directive,
            object(),
            state(),
            SimpleNamespace(action=FakeAgentPb2.ACTION_USE),
            {"line_id": 42, "skill": "open_use_line"},
            modules(),
            step=6,
            stuck_steps=1,
            step_moved=0.0,
            previous_metrics=metrics(),
            current_metrics=metrics(),
        )
        self.assertEqual(runtime._vision.calls[0][0], "repeated_failed_use")

    def test_route_open_but_blocked_triggers_vision(self):
        runtime = BrainRuntime()
        runtime._vision = FakeVision()
        directive = parse_directive({"goal": "beat the level"})
        runtime._maybe_request_vision(
            directive,
            object(),
            state(forward_open=True),
            SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD),
            {"source": "spatial_planner", "planner_skill": "sector_route_to_exit_line", "line_id": 77},
            modules(),
            step=4,
            stuck_steps=1,
            step_moved=0.0,
            previous_metrics=metrics(),
            current_metrics=metrics(),
        )
        self.assertEqual(runtime._vision.calls[0][0], "route_open_but_blocked")

    def test_no_kill_speedrun_damage_triggers_even_under_contact(self):
        runtime = BrainRuntime()
        runtime._vision = FakeVision()
        directive = parse_directive({"goal": "race to the exit without killing anything"})
        runtime._maybe_request_vision(
            directive,
            object(),
            state(),
            SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD),
            {"skill": "route_progression"},
            modules(),
            step=5,
            stuck_steps=0,
            step_moved=20.0,
            previous_metrics=metrics(health=100, visible_enemy=True),
            current_metrics=metrics(health=88, visible_enemy=True),
        )
        self.assertEqual(runtime._vision.calls[0][0], "no_kill_speedrun_damage")

    def test_exit_target_nearby_but_unconfirmed_triggers_vision(self):
        runtime = BrainRuntime()
        runtime._vision = FakeVision()
        directive = parse_directive({"goal": "beat the level"})
        runtime._maybe_request_vision(
            directive,
            object(),
            state(forward_open=False),
            SimpleNamespace(action=FakeAgentPb2.ACTION_USE),
            {"skill": "press_exit"},
            modules(),
            step=5,
            stuck_steps=0,
            step_moved=0.0,
            previous_metrics=metrics(exit_dist=120, exit_line=False),
            current_metrics=metrics(exit_dist=120, exit_line=False),
        )
        self.assertEqual(runtime._vision.calls[0][0], "exit_target_ambiguous")

    def test_vision_failure_is_nonfatal_to_brain_loop(self):
        runtime = BrainRuntime()
        runtime._vision = ExplodingVision()
        directive = parse_directive({"goal": "explore"})
        hint = runtime._maybe_request_vision(
            directive,
            object(),
            state(forward_open=False),
            SimpleNamespace(action=FakeAgentPb2.ACTION_FORWARD),
            {"skill": "route_progression"},
            modules(),
            step=3,
            stuck_steps=3,
            step_moved=0.0,
            previous_metrics=metrics(),
            current_metrics=metrics(),
        )
        self.assertIsNone(hint)


if __name__ == "__main__":
    unittest.main()
