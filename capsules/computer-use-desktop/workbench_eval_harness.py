#!/usr/bin/env python3
"""Fast local :6905 behavior harness for deterministic Workbench semantics."""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from eval_gates import evaluate_records
from workbench_eval_runner import BridgeTransport, fixture_manifest, load_cases, run_case


QUICK_CASES = {"workspace-lifecycle", "workspace-confinement", "stale-epoch-preemption"}


def run(base_url: str, *, full: bool = False, seed: int = 0) -> int:
    probe = BridgeTransport(base_url, timeout=5)
    try:
        health = probe.health()
    except Exception as exc:
        print(f"FAIL bridge health: {exc}")
        return 1
    if not health.get("ok") or health.get("protocolVersion") != "pairputer.desktop.v1":
        print("FAIL bridge health:", json.dumps(health, sort_keys=True))
        return 1
    print("PASS bridge health:", health.get("protocolVersion"))

    fixtures = fixture_manifest()
    cases = [case for case in load_cases() if case.required and case.applies_to("bridge")
             and (full or case.case_id in QUICK_CASES)]
    records = []
    for case in cases:
        transport = BridgeTransport(base_url, timeout=case.timeout_seconds)
        record = run_case(case, transport, run_id="harness-" + uuid.uuid4().hex[:12], seed=seed,
                          fixtures=fixtures)
        records.append(record)
        detail = record.get("error") or record.get("grader", {}).get("summary", "")
        print(f"{'PASS' if record['status'] == 'passed' else 'FAIL'} {case.case_id}: {detail}")
    summary = evaluate_records(records, strict_suite=full, mode="bridge")
    print("SUMMARY:", json.dumps({key: summary[key] for key in ("total", "passed", "failed", "skipped", "ok")},
                                 sort_keys=True))
    if summary["gateFailures"]:
        print(json.dumps(summary["gateFailures"], indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", default="http://127.0.0.1:6905")
    parser.add_argument("--full", action="store_true", help="also run fixed Python and Node project cases")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    return run(args.bridge.rstrip("/"), full=args.full, seed=args.seed)


if __name__ == "__main__":
    sys.exit(main())
