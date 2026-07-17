"""Offline VLM benchmark harness tests for Agent DOOM."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "vision_bench"
sys.path.insert(0, str(BENCH_ROOT))

import benchmark_vlm  # noqa: E402
import download_vlm_models  # noqa: E402
import labeled_corpus  # noqa: E402
import train_vision_adapter  # noqa: E402


class FakeOpenAIServer:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size) or b"{}")
                owner.requests.append(payload)
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
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "FakeOpenAIServer":
        self.thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


def write_case(root: Path, name: str, trigger: str) -> Path:
    image = root / name
    image.write_bytes(b"fake jpg bytes")
    image.with_suffix(".json").write_text(
        json.dumps(
            {
                "trigger": trigger,
                "context": {
                    "objective": "exit_level",
                    "skill": "press_exit",
                    "hp_delta": -12,
                    "exit_dist": 854,
                },
            }
        )
    )
    return image


class TestVisionBenchmarkHarness(unittest.TestCase):
    def test_build_payload_uses_openai_multimodal_shape(self):
        with tempfile.TemporaryDirectory() as td:
            image = write_case(Path(td), "sample-no_kill_speedrun_damage.jpg", "no_kill_speedrun_damage")
            case = benchmark_vlm.case_from_image(image)
            payload = benchmark_vlm.build_payload(case, model="local-test")
        self.assertEqual(payload["model"], "local-test")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("image pixels first", content[0]["text"])
        self.assertIn("classic DOOM", content[0]["text"])
        self.assertIn("Return ONLY raw JSON", content[0]["text"])
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_build_payload_can_prefix_model_specific_image_marker(self):
        with tempfile.TemporaryDirectory() as td:
            image = write_case(Path(td), "sample-no_kill_speedrun_damage.jpg", "no_kill_speedrun_damage")
            case = benchmark_vlm.case_from_image(image)
            payload = benchmark_vlm.build_payload(case, model="local-test", image_marker="<image>\n")
        self.assertTrue(payload["messages"][0]["content"][0]["text"].startswith("<image>\n"))

    def test_caption_prompt_mode_avoids_schema_text(self):
        with tempfile.TemporaryDirectory() as td:
            image = write_case(Path(td), "sample-no_kill_speedrun_damage.jpg", "no_kill_speedrun_damage")
            case = benchmark_vlm.case_from_image(image)
            payload = benchmark_vlm.build_payload(case, model="local-test", prompt_mode="caption")
        text = payload["messages"][0]["content"][0]["text"]
        self.assertIn("visible classic DOOM gameplay objects", text)
        self.assertNotIn("Return exactly this schema", text)

    def test_benchmark_case_scores_no_kill_enemy_visibility(self):
        model_json = json.dumps(
            {
                "is_enemy_visible": True,
                "is_door_visible": False,
                "is_switch_visible": False,
                "is_wall_blocking": False,
                "is_exit_visible": False,
                "hazard": "enemy",
                "confidence": 0.83,
                "short_reason": "enemy sprite visible",
            }
        )
        with tempfile.TemporaryDirectory() as td, FakeOpenAIServer(model_json) as server:
            image = write_case(Path(td), "sample-no_kill_speedrun_damage.jpg", "no_kill_speedrun_damage")
            result = benchmark_vlm.benchmark_case(
                benchmark_vlm.case_from_image(image),
                server_url=server.url,
                model="fake",
                timeout_s=5,
                target_ms=2500,
                discard_ms=5000,
                response_format=True,
                include_raw=False,
            )
        self.assertTrue(result["valid_json"])
        self.assertTrue(result["accuracy_ok"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["observation"]["hazard"], "enemy")
        self.assertEqual(len(server.requests), 1)

    def test_parse_json_from_fenced_response(self):
        parsed, error = benchmark_vlm.parse_observation(
            '```json\n{"is_enemy_visible": true, "confidence": 0.9, "hazard": "enemy"}\n```'
        )
        self.assertIsNone(error)
        self.assertTrue(parsed["is_enemy_visible"])
        self.assertEqual(parsed["hazard"], "enemy")

    def test_trigger_expectation_failure_is_explicit(self):
        ok, failures, expected = benchmark_vlm.grade_observation(
            "no_kill_speedrun_damage",
            {"is_enemy_visible": False},
        )
        self.assertFalse(ok)
        self.assertEqual(expected, {"is_enemy_visible": True})
        self.assertIn("is_enemy_visible!=1", failures)

    def test_exact_labels_can_override_trigger_expectations(self):
        ok, failures, expected = benchmark_vlm.grade_expected(
            {"is_enemy_visible": False, "is_wall_blocking": True},
            {"is_enemy_visible": False, "is_wall_blocking": True},
        )
        self.assertTrue(ok)
        self.assertEqual(failures, [])
        self.assertEqual(expected["is_enemy_visible"], False)

    def test_label_file_matches_filename_or_stem(self):
        with tempfile.TemporaryDirectory() as td:
            labels_path = Path(td) / "labels.json"
            labels_path.write_text(json.dumps({"frame-a.jpg": {"is_enemy_visible": True}, "frame-b": {"is_exit_visible": True}}))
            labels = benchmark_vlm.load_labels(labels_path)
        self.assertEqual(benchmark_vlm.label_for(labels, Path("frame-a.jpg")), {"is_enemy_visible": True})
        self.assertEqual(benchmark_vlm.label_for(labels, Path("frame-b.jpg")), {"is_exit_visible": True})

    def test_caption_fallback_extracts_enemy_fact(self):
        obs = benchmark_vlm.caption_to_observation("There is a person in a doorway holding a gun.")
        self.assertTrue(obs["is_enemy_visible"])
        self.assertTrue(obs["is_door_visible"])
        self.assertEqual(obs["hazard"], "enemy")

    def test_caption_fallback_handles_doom_ocr_and_combat_terms(self):
        self.assertTrue(benchmark_vlm.caption_to_observation("50.01% of the player is currently in combat mode.")["is_enemy_visible"])
        self.assertTrue(benchmark_vlm.caption_to_observation("HANGAGER FINEDED.")["is_exit_visible"])

    def test_benchmark_case_can_use_caption_fallback_without_json(self):
        with tempfile.TemporaryDirectory() as td, FakeOpenAIServer("There is a person in a doorway holding a gun.") as server:
            image = write_case(Path(td), "sample-no_kill_speedrun_damage.jpg", "no_kill_speedrun_damage")
            result = benchmark_vlm.benchmark_case(
                benchmark_vlm.case_from_image(image),
                server_url=server.url,
                model="fake",
                timeout_s=5,
                target_ms=2500,
                discard_ms=5000,
                response_format=True,
                include_raw=False,
                allow_caption_fallback=True,
            )
        self.assertFalse(result["valid_json"])
        self.assertTrue(result["valid_observation"])
        self.assertEqual(result["parse_mode"], "caption")
        self.assertTrue(result["accuracy_ok"])

    def test_summary_marks_discard_latency(self):
        summary = benchmark_vlm.summarize(
            [
                {"ok": True, "valid_json": True, "accuracy_ok": True, "elapsed_ms": 1000},
                {"ok": False, "valid_json": True, "accuracy_ok": True, "elapsed_ms": 6000, "discard": True},
            ],
            target_ms=2500,
            discard_ms=5000,
        )
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["discard"], 1)
        self.assertEqual(summary["latency_ms"]["median"], 3500)

    def test_candidate_manifest_has_projector_for_primary_multimodal_models(self):
        manifest = json.loads((BENCH_ROOT / "vlm_candidates.json").read_text())
        candidates = manifest["candidates"]
        self.assertIn("qwen2-vl-2b-q4-k-m", candidates)
        self.assertTrue(any(item["role"] == "mmproj" for item in candidates["qwen2-vl-2b-q4-k-m"]["files"]))
        self.assertTrue(any(item["role"] == "mmproj" for item in candidates["smolvlm2-500m-q8-official"]["files"]))
        self.assertIn("ggml-org/SmolVLM-256M-Instruct-GGUF:Q8_0", candidates["smolvlm-256m-q8-official"]["hf_alias"])
        self.assertIn("jc-builds/smolvlm2-500m-gguf:Q8_0", candidates["smolvlm2-500m-q8-official"]["hf_alias"])
        self.assertIn("moondream2-20250414-GGUF:F16", candidates["moondream2-20250414-f16"]["hf_alias"])

    def test_downloader_dry_run_does_not_create_model_cache(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "models"
            args = download_vlm_models.parse_args(["--dry-run", "--dest", str(dest), "qwen2-vl-2b-q4-k-m"])
            rc = download_vlm_models.run(args)
        self.assertEqual(rc, 0)
        self.assertFalse(dest.exists())

    def test_cli_returns_one_when_no_images_exist(self):
        with tempfile.TemporaryDirectory() as td:
            rc = benchmark_vlm.main(["--events-dir", td])
        self.assertEqual(rc, 1)

    def test_labeled_corpus_init_validate_and_benchmark_load(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = root / "events"
            events.mkdir()
            image = write_case(events, "frame-no_kill_speedrun_damage.jpg", "no_kill_speedrun_damage")
            labels = root / "labels.json"
            labels.write_text(json.dumps({image.name: {"is_enemy_visible": True}}))
            corpus = root / "corpus"
            manifest = labeled_corpus.build_manifest(
                name="test-corpus",
                events_dir=events,
                labels=labeled_corpus.load_labels(labels),
                out=corpus,
            )
            summary = labeled_corpus.validate_corpus(corpus, strict=True)
            cases = benchmark_vlm.cases_from_corpus(corpus)
            loaded_labels = benchmark_vlm.load_labels(corpus / "labels.json")
        self.assertEqual(manifest["counts"], {"cases": 1, "labeled": 1, "unlabeled": 0})
        self.assertEqual(summary["cases"], 1)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].trigger, "no_kill_speedrun_damage")
        self.assertEqual(benchmark_vlm.label_for(loaded_labels, Path("frame-no_kill_speedrun_damage.jpg")), {"is_enemy_visible": True})

    def test_labeled_corpus_template_writes_boolean_labels(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = root / "events"
            events.mkdir()
            image = write_case(events, "frame-route_open_but_blocked.jpg", "route_open_but_blocked")
            out = root / "labels.json"
            labels = labeled_corpus.make_template(events, out)
            self.assertTrue(out.is_file())
        self.assertIn(image.name, labels)
        self.assertFalse(labels[image.name]["is_enemy_visible"])

    def test_vision_adapter_training_loads_labeled_corpus(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            events = root / "events"
            events.mkdir()
            enemy = write_case(events, "enemy-no_kill_speedrun_damage.jpg", "no_kill_speedrun_damage")
            door = write_case(events, "door-repeated_failed_use.jpg", "repeated_failed_use")
            labels = root / "labels.json"
            labels.write_text(
                json.dumps(
                    {
                        enemy.name: {"is_enemy_visible": True, "is_door_visible": False},
                        door.name: {"is_enemy_visible": False, "is_door_visible": True},
                    }
                )
            )
            corpus = root / "corpus"
            labeled_corpus.build_manifest(
                name="adapter-test",
                events_dir=events,
                labels=labeled_corpus.load_labels(labels),
                out=corpus,
            )

            def fake_features(image: Path, _size: tuple[int, int]) -> list[float]:
                return [1.0, 0.0] if image.name.startswith("enemy-") else [0.0, 1.0]

            examples = train_vision_adapter.examples_from_corpus(corpus, feature_loader=fake_features)
            model = train_vision_adapter.build_model(examples, name="adapter-test", top_k=1)
            summary = train_vision_adapter.evaluate_examples(model, examples)
        self.assertEqual(len(examples), 2)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["cases"], 2)


if __name__ == "__main__":
    unittest.main()
