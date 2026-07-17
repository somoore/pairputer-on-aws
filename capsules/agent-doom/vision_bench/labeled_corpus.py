#!/usr/bin/env python3
"""Build and validate local labeled Agent DOOM vision corpora.

The corpus itself is local/gitignored because it contains game screenshots.
The format is intentionally simple so benchmark_vlm.py can consume it without
additional dependencies:

  corpus/
    manifest.json
    labels.json
    frames/*.jpg
    sidecars/*.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

BOOLEAN_LABELS = (
    "is_enemy_visible",
    "is_door_visible",
    "is_switch_visible",
    "is_wall_blocking",
    "is_exit_visible",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def load_labels(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    raw = load_json(path)
    labels: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            labels[str(key)] = normalize_label(value)
    return labels


def normalize_label(label: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in label.items():
        if key in BOOLEAN_LABELS:
            out[key] = bool(value)
        elif key == "any_true" and isinstance(value, list):
            out[key] = [str(item) for item in value if str(item) in BOOLEAN_LABELS]
        elif key in {"note", "source"}:
            out[key] = str(value)[:240]
    return out


def label_for(labels: dict[str, dict[str, Any]], image: Path) -> dict[str, Any]:
    return labels.get(image.name) or labels.get(image.stem) or {}


def read_sidecar(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return load_json(path)
    except Exception:
        return {}


def discover_images(events_dir: Path) -> list[Path]:
    files = sorted(events_dir.glob("*.jpg")) + sorted(events_dir.glob("*.jpeg"))
    return [path for path in files if path.is_file()]


def build_manifest(*, name: str, events_dir: Path, labels: dict[str, dict[str, Any]], out: Path) -> dict[str, Any]:
    frames_dir = out / "frames"
    sidecars_dir = out / "sidecars"
    frames_dir.mkdir(parents=True, exist_ok=True)
    sidecars_dir.mkdir(parents=True, exist_ok=True)

    cases = []
    normalized_labels: dict[str, dict[str, Any]] = {}
    for image in discover_images(events_dir):
        dest_image = frames_dir / image.name
        shutil.copy2(image, dest_image)
        sidecar = image.with_suffix(".json")
        dest_sidecar = sidecars_dir / sidecar.name
        sidecar_payload = read_sidecar(sidecar)
        if sidecar.is_file():
            shutil.copy2(sidecar, dest_sidecar)
        label = label_for(labels, image)
        if label:
            normalized_labels[image.name] = label
        cases.append(
            {
                "image": str(dest_image.relative_to(out)),
                "sidecar": str(dest_sidecar.relative_to(out)) if sidecar.is_file() else None,
                "sha256": sha256_file(dest_image),
                "bytes": dest_image.stat().st_size,
                "trigger": str(sidecar_payload.get("trigger") or trigger_from_name(image.name)),
                "label_status": "labeled" if label else "unlabeled",
            }
        )

    manifest = {
        "version": 1,
        "name": name,
        "created_at": int(time.time()),
        "source_events_dir": str(events_dir),
        "cases": cases,
        "counts": {
            "cases": len(cases),
            "labeled": sum(1 for case in cases if case["label_status"] == "labeled"),
            "unlabeled": sum(1 for case in cases if case["label_status"] != "labeled"),
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "labels.json").write_text(json.dumps(normalized_labels, indent=2, sort_keys=True) + "\n")
    return manifest


def trigger_from_name(name: str) -> str:
    for trigger in (
        "no_kill_speedrun_damage",
        "repeated_failed_use",
        "route_open_but_blocked",
        "stuck_same_coordinates",
        "exit_target_ambiguous",
    ):
        if trigger in name:
            return trigger
    return "manual"


def validate_corpus(path: Path, *, strict: bool = False) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    labels_path = path / "labels.json"
    manifest = load_json(manifest_path)
    labels = load_labels(labels_path if labels_path.is_file() else None)
    failures: list[str] = []
    cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    labeled = 0
    for case in cases:
        if not isinstance(case, dict):
            failures.append("case_not_object")
            continue
        image = path / str(case.get("image") or "")
        if not image.is_file():
            failures.append(f"missing_image:{case.get('image')}")
            continue
        expected_sha = str(case.get("sha256") or "")
        if expected_sha and sha256_file(image) != expected_sha:
            failures.append(f"sha_mismatch:{case.get('image')}")
        label = labels.get(image.name) or labels.get(image.stem)
        if label:
            labeled += 1
        elif strict:
            failures.append(f"missing_label:{image.name}")
    summary = {"cases": len(cases), "labeled": labeled, "failures": failures}
    if failures:
        raise SystemExit(json.dumps(summary, sort_keys=True))
    return summary


def make_template(events_dir: Path, out: Path) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    for image in discover_images(events_dir):
        labels[image.name] = {key: False for key in BOOLEAN_LABELS}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n")
    return labels


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="Create a local corpus from event artifacts and optional labels.")
    init.add_argument("--events-dir", required=True, type=Path)
    init.add_argument("--labels", type=Path)
    init.add_argument("--out", required=True, type=Path)
    init.add_argument("--name", default="agent-doom-vision-corpus")

    validate = sub.add_parser("validate", help="Validate corpus files, hashes, and optional strict labels.")
    validate.add_argument("--corpus", required=True, type=Path)
    validate.add_argument("--strict", action="store_true")

    template = sub.add_parser("template", help="Write a label template for event artifacts.")
    template.add_argument("--events-dir", required=True, type=Path)
    template.add_argument("--out", required=True, type=Path)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.cmd == "init":
        labels = load_labels(args.labels)
        manifest = build_manifest(name=args.name, events_dir=args.events_dir, labels=labels, out=args.out)
        print(json.dumps(manifest["counts"], sort_keys=True))
        return 0
    if args.cmd == "validate":
        print(json.dumps(validate_corpus(args.corpus, strict=args.strict), sort_keys=True))
        return 0
    if args.cmd == "template":
        labels = make_template(args.events_dir, args.out)
        print(json.dumps({"labels": len(labels), "out": str(args.out)}, sort_keys=True))
        return 0
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
