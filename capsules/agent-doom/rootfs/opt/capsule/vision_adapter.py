#!/usr/bin/env python3.11
"""Tiny deterministic vision adapter for Agent DOOM screenshots.

This is not a general VLM. It is a deliberately small, local classifier that
turns harvested DOOM frames into a JSON exemplar model. Runtime inference is a
nearest-neighbor vote over compact color/grid features decoded from the current
JPEG artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any

MODEL_VERSION = 1
DEFAULT_MODEL_PATH = Path(os.environ.get("PAIRPUTER_VISION_ADAPTER_MODEL", "/opt/capsule/vision_adapter_model.json"))
DEFAULT_IMAGE_SIZE = (32, 32)
DEFAULT_TOP_K = 3

LABEL_KEYS = (
    "is_enemy_visible",
    "is_door_visible",
    "is_switch_visible",
    "is_wall_blocking",
    "is_exit_visible",
)

SCHEMA_DEFAULTS = {
    "is_enemy_visible": False,
    "is_door_visible": False,
    "is_switch_visible": False,
    "is_wall_blocking": False,
    "is_exit_visible": False,
    "hazard": "unknown",
    "confidence": 0.0,
    "short_reason": "",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_rgb_with_ffmpeg(
    image: Path,
    *,
    size: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    ffmpeg_bin: str | None = None,
    timeout_s: float = 4.0,
) -> tuple[bytes, int, int]:
    width, height = int(size[0]), int(size[1])
    cmd = [
        ffmpeg_bin or os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(image),
        "-vf",
        f"scale={width}:{height}:flags=bilinear",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=float(timeout_s))
    expected = width * height * 3
    if int(getattr(result, "returncode", 1) or 0) != 0 or len(result.stdout) != expected:
        stderr = result.stderr.decode("utf-8", "replace")[:160] if result.stderr else "decode_failed"
        raise RuntimeError(f"ffmpeg_rgb_decode_failed:{stderr}")
    return result.stdout, width, height


def features_from_file(
    image: Path,
    *,
    size: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    ffmpeg_bin: str | None = None,
    timeout_s: float = 4.0,
) -> list[float]:
    raw, width, height = decode_rgb_with_ffmpeg(image, size=size, ffmpeg_bin=ffmpeg_bin, timeout_s=timeout_s)
    return extract_features_from_rgb(raw, width, height)


def extract_features_from_rgb(raw: bytes, width: int, height: int) -> list[float]:
    if width <= 0 or height <= 0 or len(raw) < width * height * 3:
        raise ValueError("raw RGB buffer does not match dimensions")
    crop_h = max(1, int(height * 0.82))
    features: list[float] = []
    features.extend(_region_features(raw, width, height, 0, 0, width, crop_h))
    grid = 4
    for gy in range(grid):
        y0 = int(gy * crop_h / grid)
        y1 = int((gy + 1) * crop_h / grid)
        for gx in range(grid):
            x0 = int(gx * width / grid)
            x1 = int((gx + 1) * width / grid)
            features.extend(_region_features(raw, width, height, x0, y0, x1, y1))
    return [round(value, 6) for value in features]


def _region_features(raw: bytes, width: int, height: int, x0: int, y0: int, x1: int, y1: int) -> list[float]:
    x0 = max(0, min(width, int(x0)))
    x1 = max(x0 + 1, min(width, int(x1)))
    y0 = max(0, min(height, int(y0)))
    y1 = max(y0 + 1, min(height, int(y1)))
    total = 0
    sum_r = sum_g = sum_b = sum_luma = sum_sat = 0.0
    sum_luma2 = 0.0
    red_dom = green_dom = blue_dom = dark = bright = yellowish = 0
    for y in range(y0, y1):
        row = y * width * 3
        for x in range(x0, x1):
            idx = row + x * 3
            r = raw[idx] / 255.0
            g = raw[idx + 1] / 255.0
            b = raw[idx + 2] / 255.0
            luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
            sat = max(r, g, b) - min(r, g, b)
            total += 1
            sum_r += r
            sum_g += g
            sum_b += b
            sum_luma += luma
            sum_luma2 += luma * luma
            sum_sat += sat
            red_dom += int(r > g * 1.18 and r > b * 1.18)
            green_dom += int(g > r * 1.12 and g > b * 1.12)
            blue_dom += int(b > r * 1.12 and b > g * 1.12)
            dark += int(luma < 0.18)
            bright += int(luma > 0.68)
            yellowish += int(r > 0.45 and g > 0.36 and b < 0.28)
    denom = float(max(1, total))
    mean_luma = sum_luma / denom
    variance = max(0.0, sum_luma2 / denom - mean_luma * mean_luma)
    return [
        sum_r / denom,
        sum_g / denom,
        sum_b / denom,
        mean_luma,
        math.sqrt(variance),
        sum_sat / denom,
        red_dom / denom,
        green_dom / denom,
        blue_dom / denom,
        dark / denom,
        bright / denom,
        yellowish / denom,
    ]


def build_model(
    examples: list[dict[str, Any]],
    *,
    name: str = "agent-doom-vision-adapter",
    image_size: tuple[int, int] = DEFAULT_IMAGE_SIZE,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for index, example in enumerate(examples):
        features = [float(value) for value in example.get("features") or []]
        labels = normalize_labels(example.get("labels") if isinstance(example.get("labels"), dict) else {})
        if not features or not labels:
            continue
        normalized.append(
            {
                "id": str(example.get("id") or f"example-{index}")[:120],
                "image": str(example.get("image") or "")[:180],
                "trigger": str(example.get("trigger") or "manual")[:64],
                "sha256": str(example.get("sha256") or "")[:80],
                "features": [round(float(value), 6) for value in features],
                "labels": labels,
            }
        )
    priors = {}
    for key in LABEL_KEYS:
        values = [1.0 if bool(item["labels"].get(key)) else 0.0 for item in normalized if key in item["labels"]]
        priors[key] = round(sum(values) / len(values), 6) if values else 0.0
    return {
        "version": MODEL_VERSION,
        "name": str(name)[:120],
        "created_at": int(time.time()),
        "image_size": [int(image_size[0]), int(image_size[1])],
        "top_k": max(1, int(top_k)),
        "labels": list(LABEL_KEYS),
        "priors": priors,
        "exemplars": normalized,
    }


def normalize_labels(labels: dict[str, Any]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for key in LABEL_KEYS:
        if key in labels:
            out[key] = bool(labels[key])
    return out


def load_model(path: Path = DEFAULT_MODEL_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or int(data.get("version", 0) or 0) != MODEL_VERSION:
        raise RuntimeError("unsupported vision adapter model")
    if not isinstance(data.get("exemplars"), list) or not data["exemplars"]:
        raise RuntimeError("vision adapter model has no exemplars")
    return data


def save_model(model: dict[str, Any], path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(model, indent=2, sort_keys=True) + "\n")


def predict_facts(model: dict[str, Any], features: list[float]) -> dict[str, Any]:
    exemplars = [item for item in model.get("exemplars", []) if isinstance(item, dict) and isinstance(item.get("features"), list)]
    if not exemplars:
        raise RuntimeError("vision adapter model has no usable exemplars")
    ranked = sorted((_distance(features, item["features"]), item) for item in exemplars)
    nearest_dist, nearest = ranked[0]
    if nearest_dist <= 1e-12:
        scores = {key: 1.0 if bool(nearest.get("labels", {}).get(key)) else 0.0 for key in LABEL_KEYS}
        votes = {key: 1 for key in LABEL_KEYS if key in nearest.get("labels", {})}
    else:
        top_k = min(max(1, int(model.get("top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K)), len(ranked))
        top = ranked[:top_k]
        scores: dict[str, float] = {}
        votes: dict[str, int] = {}
        for key in LABEL_KEYS:
            numerator = denominator = 0.0
            count = 0
            for dist, item in top:
                labels = item.get("labels") if isinstance(item.get("labels"), dict) else {}
                if key not in labels:
                    continue
                weight = 1.0 / max(0.000001, dist)
                numerator += weight * (1.0 if bool(labels[key]) else 0.0)
                denominator += weight
                count += 1
            scores[key] = numerator / denominator if denominator else float(model.get("priors", {}).get(key, 0.0) or 0.0)
            votes[key] = count
    facts = dict(SCHEMA_DEFAULTS)
    for key in LABEL_KEYS:
        facts[key] = bool(scores.get(key, 0.0) >= 0.5)
    confidence = max(abs(float(scores.get(key, 0.0)) - 0.5) * 2.0 for key in LABEL_KEYS)
    facts["confidence"] = round(max(0.05, min(1.0, confidence)), 3)
    facts["hazard"] = "enemy" if facts["is_enemy_visible"] else ("blocked" if facts["is_wall_blocking"] else ("exit" if facts["is_exit_visible"] else "unknown"))
    facts["short_reason"] = f"adapter nearest={nearest.get('id', 'unknown')} dist={nearest_dist:.4f}"
    facts["adapter"] = {
        "nearest": str(nearest.get("id", ""))[:120],
        "distance": round(float(nearest_dist), 6),
        "scores": {key: round(float(scores.get(key, 0.0)), 4) for key in LABEL_KEYS},
        "votes": votes,
    }
    return facts


def _distance(left: list[float], right: list[float]) -> float:
    n = min(len(left), len(right))
    if n <= 0:
        return 999.0
    total = 0.0
    for index in range(n):
        delta = float(left[index]) - float(right[index])
        total += delta * delta
    return math.sqrt(total / n)


def evaluate_examples(model: dict[str, Any], examples: list[dict[str, Any]], *, leave_one_out: bool = False) -> dict[str, Any]:
    results = []
    for example in examples:
        labels = normalize_labels(example.get("labels") if isinstance(example.get("labels"), dict) else {})
        if not labels:
            continue
        current_model = model
        if leave_one_out:
            current_model = dict(model)
            current_model["exemplars"] = [
                item for item in model.get("exemplars", []) if str(item.get("id")) != str(example.get("id"))
            ]
            if not current_model["exemplars"]:
                continue
        facts = predict_facts(current_model, [float(value) for value in example.get("features") or []])
        failures = [key for key, expected in labels.items() if bool(facts.get(key)) != bool(expected)]
        results.append(
            {
                "id": str(example.get("id") or ""),
                "ok": not failures,
                "failures": failures,
                "expected": labels,
                "facts": {key: facts.get(key) for key in LABEL_KEYS},
                "confidence": facts.get("confidence", 0.0),
            }
        )
    per_label: dict[str, dict[str, int]] = {}
    for key in LABEL_KEYS:
        total = correct = 0
        for result in results:
            if key not in result["expected"]:
                continue
            total += 1
            correct += int(bool(result["facts"].get(key)) == bool(result["expected"][key]))
        per_label[key] = {"correct": correct, "total": total}
    return {
        "cases": len(results),
        "ok": sum(1 for item in results if item["ok"]),
        "leave_one_out": bool(leave_one_out),
        "per_label": per_label,
        "results": results,
    }
