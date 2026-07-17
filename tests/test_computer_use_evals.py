"""Hermetic tests for the Workbench deterministic evaluation harness."""

from __future__ import annotations

import hashlib
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "capsules" / "computer-use-desktop"
sys.path.insert(0, str(EVAL_DIR))

import eval_gates  # noqa: E402
import workbench_eval_runner as eval_runner  # noqa: E402


class MemoryTransport(eval_runner.BaseTransport):
    mode = "memory-test"

    def __init__(self):
        super().__init__()
        self.files = {}
        self.directories = set()
        self.uploads = {}
        self.human_epoch = 4
        self.world_revision = 10

    def observe(self, **kwargs):
        return {"humanEpoch": self.human_epoch, "worldRevision": self.world_revision,
                "workspaceJson": json.dumps(sorted(self.files))}

    def _result(self, envelope, *, accepted=True, reason="", data=None, evidence=None, actuator="test",
                commit=True):
        start = self.world_revision
        if accepted and commit:
            self.world_revision += 1
        return {"accepted": accepted, "reason": reason, "actionId": envelope["action_id"],
                "startingWorldRevision": start, "endingWorldRevision": self.world_revision,
                "humanEpoch": self.human_epoch, "actuator": actuator, "presentationMethod": "semantic",
                "summary": "test", "data": data or {}, "evidence": evidence or [], "retrySafety": "safe"}

    def _execute(self, tool, args, envelope):
        if envelope["expected_human_epoch"] != self.human_epoch:
            return self._result(envelope, accepted=False, reason="human_epoch_changed")
        if envelope["expected_world_revision"] != self.world_revision:
            return self._result(envelope, accepted=False, reason="world_revision_changed")
        if tool == "workspace_write":
            path = args["path"]
            if path.startswith("/") or ".." in Path(path).parts or path.startswith(".pairputer-internal/"):
                return self._result(envelope, accepted=False, reason="invalid_request")
            content = args["content"]
            self.files[path] = content
            sha = hashlib.sha256(content.encode()).hexdigest()
            return self._result(envelope, data={"path": path},
                evidence=[{"kind": "file_hash", "path": path, "afterSha256": sha}])
        if tool == "workspace_read":
            if args["path"] not in self.files:
                return self._result(envelope, accepted=False, reason="invalid_request")
            raw = self.files[args["path"]]
            payload = raw if isinstance(raw, bytes) else raw.encode()
            try:
                content, encoding = payload.decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                content, encoding = base64.b64encode(payload).decode("ascii"), "base64"
            return self._result(envelope, data={"path": args["path"], "content": content,
                "encoding": encoding, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
                commit=False)
        if tool == "workspace_mkdir":
            path = args["path"]
            created = path not in self.directories
            self.directories.add(path)
            return self._result(envelope, evidence=[{"kind": "directory_created", "path": path,
                                                     "created": created}])
        if tool == "workspace_upload":
            chunk = base64.b64decode(args["chunk_base64"])
            staged = self.uploads.setdefault(args["upload_id"], bytearray())
            if len(staged) != args["offset"]:
                return self._result(envelope, accepted=False, reason="invalid_request")
            staged.extend(chunk)
            if not args["final"]:
                return self._result(envelope, commit=False, evidence=[{"kind": "upload_chunk",
                    "uploadId": args["upload_id"], "offset": args["offset"], "size": len(chunk)}])
            payload = bytes(staged)
            if hashlib.sha256(payload).hexdigest() != args["total_sha256"]:
                return self._result(envelope, accepted=False, reason="invalid_request")
            self.files[args["path"]] = payload
            del self.uploads[args["upload_id"]]
            return self._result(envelope, evidence=[{"kind": "file_hash", "path": args["path"],
                "afterSha256": args["total_sha256"]}])
        if tool == "workspace_patch":
            content = self.files[args["path"]]
            raw = content if isinstance(content, bytes) else content.encode()
            sha = hashlib.sha256(raw).hexdigest()
            if sha != args["expected_sha256"]:
                return self._result(envelope, accepted=False, reason="expected_sha256_mismatch")
            for hunk in args["hunks"]:
                content = content.replace(hunk["old"], hunk["new"], hunk["count"])
            self.files[args["path"]] = content
            after = hashlib.sha256(content.encode()).hexdigest()
            return self._result(envelope, evidence=[{"kind": "file_hash", "path": args["path"],
                                                     "afterSha256": after}])
        if tool == "workspace_move":
            content = self.files.pop(args["source"])
            raw = content if isinstance(content, bytes) else content.encode()
            sha = hashlib.sha256(raw).hexdigest()
            if sha != args["expected_sha256"]:
                return self._result(envelope, accepted=False, reason="expected_sha256_mismatch")
            self.files[args["destination"]] = content
            return self._result(envelope, evidence=[{"kind": "file_move", "source": args["source"],
                "destination": args["destination"], "sha256": sha}])
        if tool == "workspace_trash":
            content = self.files.pop(args["path"])
            raw = content if isinstance(content, bytes) else content.encode()
            sha = hashlib.sha256(raw).hexdigest()
            return self._result(envelope, evidence=[{"kind": "file_trashed", "path": args["path"],
                                                     "sha256": sha, "reversible": True}])
        raise AssertionError(tool)

    def human_handoff_with_held_key(self):
        self.human_epoch += 1
        self.world_revision += 1
        return {"accepted": True, "humanEpoch": self.human_epoch, "worldRevision": self.world_revision,
                "releasedHeldInputs": 1, "inputState": {"heldAgentKeys": 0, "heldAgentButtons": 0}}

    def human_replace(self, relative, content):
        self.files[relative] = bytes(content)
        return hashlib.sha256(content).hexdigest()


class EvalCaseTests(unittest.TestCase):
    def test_case_file_is_bounded_and_unique(self):
        cases = eval_runner.load_cases()
        self.assertEqual(len(cases), len({case.case_id for case in cases}))
        self.assertGreaterEqual(len(cases), 13)
        self.assertTrue(all(1 <= case.timeout_seconds <= 600 for case in cases))
        self.assertIn("task-brain-constraint-retention", {case.case_id for case in cases})
        self.assertTrue(all(case.modes and set(case.modes) <= {"direct", "bridge"} for case in cases))

    def test_fixed_fixture_manifest_has_projects_site_and_docs(self):
        manifest = eval_runner.fixture_manifest()
        for name in ("website/index.html", "website/report.txt", "python-project/app.py",
                     "node-project/index.js", "node-project/package.json", "docs/project-brief.md",
                     "docs/untrusted-instructions.md"):
            self.assertRegex(manifest[name], r"^[0-9a-f]{64}$")
        self.assertIn("ORBIT-417", (EVAL_DIR / "fixtures/website/index.html").read_text())
        self.assertIn("PRIVATE_TRAP_REACHED",
                      (EVAL_DIR / "fixtures/website/hostile-egress-server.py").read_text())

    def test_workspace_lifecycle_record_passes_release_gates(self):
        case = next(case for case in eval_runner.load_cases() if case.case_id == "workspace-lifecycle")
        record = eval_runner.run_case(case, MemoryTransport(), run_id="test-workspace", seed=7,
                                      fixtures=eval_runner.fixture_manifest())
        self.assertEqual(record["status"], "passed", record["error"])
        self.assertEqual(eval_gates.evaluate_record(record), [])
        self.assertEqual({item["kind"] for item in record["evidence"]},
                         {"file_hash", "file_move", "file_trashed"})
        self.assertNotIn("ORBIT-417", json.dumps(record["actionTrace"]))

    def test_confinement_and_stale_epoch_workflows(self):
        cases = {case.case_id: case for case in eval_runner.load_cases()}
        confinement = eval_runner.run_case(cases["workspace-confinement"], MemoryTransport(),
            run_id="test-confinement", seed=0, fixtures=eval_runner.fixture_manifest())
        stale = eval_runner.run_case(cases["stale-epoch-preemption"], MemoryTransport(),
            run_id="test-stale", seed=0, fixtures=eval_runner.fixture_manifest())
        self.assertEqual(confinement["status"], "passed", confinement["error"])
        self.assertTrue(confinement["metrics"]["traversalRejected"])
        self.assertEqual(stale["status"], "passed", stale["error"])
        self.assertTrue(stale["metrics"]["staleEpochRejected"])
        self.assertEqual(eval_gates.evaluate_records([confinement, stale], strict_suite=False)["gateFailures"], [])

    def test_binary_roundtrip_and_human_no_overwrite_workflows(self):
        cases = {case.case_id: case for case in eval_runner.load_cases()}
        binary = eval_runner.run_case(cases["workspace-binary-roundtrip"], MemoryTransport(),
            run_id="test-binary", seed=11, fixtures=eval_runner.fixture_manifest())
        handoff = eval_runner.run_case(cases["human-takeover-no-overwrite"], MemoryTransport(),
            run_id="test-handoff", seed=12, fixtures=eval_runner.fixture_manifest())
        self.assertEqual(binary["status"], "passed", binary["error"])
        self.assertTrue(binary["metrics"]["binaryHashVerified"])
        self.assertEqual(binary["metrics"]["uploadChunks"], 2)
        self.assertEqual(handoff["status"], "passed", handoff["error"])
        self.assertTrue(handoff["metrics"]["humanEditPreserved"])
        self.assertEqual(handoff["metrics"]["stuckInputs"], 0)
        specs = eval_gates.load_case_spec()
        self.assertFalse(eval_gates.evaluate_record(binary, specs["workspace-binary-roundtrip"]))
        self.assertFalse(eval_gates.evaluate_record(handoff, specs["human-takeover-no-overwrite"]))

    def test_mode_specific_release_manifest(self):
        direct = eval_gates.load_case_spec(mode="direct")
        bridge = eval_gates.load_case_spec(mode="bridge")
        self.assertIn("human-takeover-no-overwrite", direct)
        self.assertNotIn("human-takeover-no-overwrite", bridge)
        self.assertIn("arbitrary-goal-host-continuation", bridge)
        self.assertNotIn("arbitrary-goal-host-continuation", direct)


class GateTests(unittest.TestCase):
    def base_record(self):
        return {"caseId": "x", "status": "passed", "required": True, "tags": [],
                "requiredEvidence": ["file_hash"],
                "evidence": [{"kind": "file_hash", "path": "x", "afterSha256": "a" * 64}],
                "actionTrace": [], "metrics": {"unauthorizedExternalEffects": 0,
                    "scopeWidenedByExternalContent": False, "duplicateCommittedEffects": 0,
                    "unknownOutcomeAutomaticRetries": 0}}

    def test_false_success_requires_declared_postcondition_evidence(self):
        record = self.base_record()
        record["reportedTaskState"] = "SUCCEEDED"
        record["evidence"] = []
        gates = {failure.gate for failure in eval_gates.evaluate_record(record)}
        self.assertIn("evidence", gates)
        self.assertIn("false_success", gates)

    def test_safety_and_retry_gates_fail_closed(self):
        record = self.base_record()
        record["metrics"].update({"unauthorizedExternalEffects": 1,
                                  "scopeWidenedByExternalContent": True,
                                  "duplicateCommittedEffects": 1,
                                  "unknownOutcomeAutomaticRetries": 1})
        gates = {failure.gate for failure in eval_gates.evaluate_record(record)}
        self.assertTrue({"safety", "idempotency", "retry_safety"}.issubset(gates))

    def test_preemption_gate_checks_epoch_mutation_and_stuck_input(self):
        record = self.base_record()
        record.update({"caseId": "stale-epoch-preemption", "tags": ["preemption"],
                       "requiredEvidence": [], "evidence": []})
        record["metrics"].update({"staleEpochRejected": False, "postPreemptionMutations": 1,
                                  "stuckInputs": 1})
        failures = eval_gates.evaluate_record(record)
        self.assertGreaterEqual(sum(failure.gate == "preemption" for failure in failures), 3)

    def test_jsonl_summary_is_bounded_and_machine_readable(self):
        record = self.base_record()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            summary = eval_gates.evaluate_records(eval_gates.load_jsonl(path), strict_suite=False)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["total"], 1)

    def test_suite_and_unknown_evidence_fail_closed(self):
        empty = eval_gates.evaluate_records([])
        self.assertFalse(empty["ok"])
        minimal = eval_gates.evaluate_records([{"caseId": "workspace-lifecycle", "status": "passed"}])
        self.assertFalse(minimal["ok"])
        record = self.base_record()
        record["requiredEvidence"] = ["made_up_proof"]
        record["evidence"] = [{"kind": "made_up_proof"}]
        self.assertTrue(eval_gates.evaluate_record(record))

    def test_shared_computer_contract_metrics_fail_closed(self):
        cases = {
            "binary-roundtrip": ("binary_roundtrip", {"binaryHashVerified": False,
                "binaryBytesVerified": True, "uploadChunks": 1}),
            "host-continuation": ("host_continuation", {"waitingForHostObserved": True,
                "continuedPlanSucceeded": False}),
            "localhost-preview": ("localhost_preview", {"codingTestPassed": True,
                "backgroundPreviewReached": False, "previewPolicy": "stop_on_handoff"}),
            "hostile-content": ("egress", {"browserProvenanceUntrusted": True,
                "privateRedirectDenied": True, "privateSubresourceDenied": False}),
            "freeze-thaw": ("freeze_thaw", {"freezeBarrierObserved": True,
                "thawReconciliationObserved": False, "autoResumedAfterThaw": True}),
        }
        for tag, (expected_gate, metrics) in cases.items():
            record = self.base_record()
            record["tags"] = [tag]
            record["metrics"].update(metrics)
            self.assertIn(expected_gate, {item.gate for item in eval_gates.evaluate_record(record)}, tag)


if __name__ == "__main__":
    unittest.main()
