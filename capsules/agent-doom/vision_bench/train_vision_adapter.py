#!/usr/bin/env python3
"""Train and evaluate the tiny Agent DOOM vision adapter from a corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
RUNTIME_ROOT = HERE.parent / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(RUNTIME_ROOT))

from vision_adapter import (  # noqa: E402
    DEFAULT_IMAGE_SIZE,
    DEFAULT_MODEL_PATH,
    build_model,
    evaluate_examples,
    features_from_file,
    load_model,
    normalize_labels,
    save_model,
    sha256_file,
)

FeatureLoader = Callable[[Path, tuple[int, int]], list[float]]


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def load_labels(corpus: Path) -> dict[str, dict[str, Any]]:
    path = corpus / "labels.json"
    raw = load_json(path)
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key)] = normalize_labels(value)
    return out


def label_for(labels: dict[str, dict[str, Any]], image: Path) -> dict[str, Any]:
    return labels.get(image.name) or labels.get(image.stem) or {}


def examples_from_corpus(
    corpus: Path,
    *,
    image_size: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    feature_loader: FeatureLoader | None = None,
) -> list[dict[str, Any]]:
    manifest = load_json(corpus / "manifest.json")
    labels = load_labels(corpus)
    cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    loader = feature_loader or (lambda image, size: features_from_file(image, size=size))
    examples: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        image = corpus / str(case.get("image") or "")
        if not image.is_file():
            continue
        item_labels = label_for(labels, image)
        if not item_labels:
            continue
        examples.append(
            {
                "id": image.name,
                "image": str(image.relative_to(corpus)),
                "trigger": str(case.get("trigger") or "manual"),
                "sha256": str(case.get("sha256") or sha256_file(image)),
                "labels": item_labels,
                "features": loader(image, image_size),
            }
        )
    return examples


def train(args: argparse.Namespace) -> int:
    image_size = parse_size(args.image_size)
    examples = examples_from_corpus(args.corpus, image_size=image_size)
    if not examples:
        raise SystemExit("no labeled examples found")
    model = build_model(examples, name=args.name, image_size=image_size, top_k=args.top_k)
    save_model(model, args.out)
    summary = evaluate_examples(model, examples, leave_one_out=False)
    print(json.dumps({"out": str(args.out), "examples": len(examples), "resubstitution_ok": summary["ok"]}, sort_keys=True))
    return 0


def evaluate(args: argparse.Namespace) -> int:
    image_size = parse_size(args.image_size)
    model = load_model(args.model)
    examples = examples_from_corpus(args.corpus, image_size=image_size)
    summary = evaluate_examples(model, examples, leave_one_out=args.leave_one_out)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    compact = {
        "cases": summary["cases"],
        "ok": summary["ok"],
        "leave_one_out": summary["leave_one_out"],
        "per_label": summary["per_label"],
    }
    print(json.dumps(compact, sort_keys=True))
    return 2 if args.strict and summary["ok"] != summary["cases"] else 0


def parse_size(value: str) -> tuple[int, int]:
    parts = str(value).lower().split("x", 1)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT")
    width = max(8, min(128, int(parts[0])))
    height = max(8, min(128, int(parts[1])))
    return width, height


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    train_cmd = sub.add_parser("train", help="Train an adapter model from a labeled corpus.")
    train_cmd.add_argument("--corpus", required=True, type=Path)
    train_cmd.add_argument("--out", type=Path, default=DEFAULT_MODEL_PATH)
    train_cmd.add_argument("--name", default="agent-doom-vision-adapter")
    train_cmd.add_argument("--image-size", default=f"{DEFAULT_IMAGE_SIZE[0]}x{DEFAULT_IMAGE_SIZE[1]}")
    train_cmd.add_argument("--top-k", type=int, default=3)

    eval_cmd = sub.add_parser("eval", help="Evaluate an adapter model against a labeled corpus.")
    eval_cmd.add_argument("--corpus", required=True, type=Path)
    eval_cmd.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    eval_cmd.add_argument("--image-size", default=f"{DEFAULT_IMAGE_SIZE[0]}x{DEFAULT_IMAGE_SIZE[1]}")
    eval_cmd.add_argument("--leave-one-out", action="store_true")
    eval_cmd.add_argument("--strict", action="store_true")
    eval_cmd.add_argument("--output", type=Path)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.cmd == "train":
        return train(args)
    if args.cmd == "eval":
        return evaluate(args)
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
