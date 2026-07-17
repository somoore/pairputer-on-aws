#!/usr/bin/env python3
"""Trusted deployment policy for capsule authority manifests."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath

import yaml

MAX_MANIFEST_BYTES = 512 * 1024
ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
TOOL = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,127}$")
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{1,255}$")
ALLOWED_TOP = {
    "id", "name", "description", "interaction", "bridge", "lifecycle", "runtime",
    "experience", "tools", "permissions", "safety",
}
HIGH_RISK = {"local_destructive", "external_commit", "unknown"}
SECRET_SHAPES = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def fail(message: str) -> None:
    raise ValueError("capsule manifest rejected: " + message)


def valid_route(value: object, label: str) -> str:
    path = str(value or "")
    if not SAFE_PATH.fullmatch(path) or ".." in PurePosixPath(path).parts or "//" in path:
        fail(f"{label} must be a normalized relative HTTP route")
    return path


def main() -> int:
    path = Path(sys.argv[1])
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        fail("file exceeds 512 KiB")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"capsule"} or not isinstance(document["capsule"], dict):
        fail("root must contain exactly one capsule object")
    capsule = document["capsule"]
    extra = set(capsule) - ALLOWED_TOP
    if extra:
        fail("unknown capsule fields: " + ", ".join(sorted(extra)))
    if not ID.fullmatch(str(capsule.get("id") or "")):
        fail("id has an invalid shape")
    if not str(capsule.get("name") or "").strip() or len(str(capsule.get("name"))) > 256:
        fail("name is required and bounded")
    bridge = capsule.get("bridge") or {}
    port = int(bridge.get("port", 6905))
    if bridge.get("protocol", "http-json") != "http-json" or not 1 <= port <= 65535:
        fail("bridge must use bounded http-json")
    lifecycle = capsule.get("lifecycle") or {}
    for key, value in lifecycle.items():
        if key not in {"beforeFreeze", "afterThaw"}:
            fail(f"unknown lifecycle hook {key}")
        valid_route(value, f"lifecycle.{key}")
    runtime = capsule.get("runtime") or {}
    profile = runtime.get("localDockerSeccompProfile", "")
    if profile not in {"", "chromium-namespaces-v1"}:
        fail("runtime.localDockerSeccompProfile is not allowlisted")
    tools = capsule.get("tools") or []
    if not isinstance(tools, list) or len(tools) > 256:
        fail("tools must be a bounded list")
    names: set[str] = set()
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict) or not TOOL.fullmatch(str(tool.get("name") or "")):
            fail(f"tool {index} has an invalid name")
        name = str(tool["name"])
        if name in names:
            fail(f"duplicate tool {name}")
        names.add(name)
        valid_route(tool.get("path") or "/" + name, f"tool {name} path")
        if not isinstance(tool.get("requiresApproval", False), bool):
            fail(f"tool {name} requiresApproval must be Boolean")
        risk = str(tool.get("riskClass") or "")
        effects = {str(value).lower() for value in (tool.get("effects") or [])}
        safety_stop = tool.get("approvalPolicy") == "safety_stop"
        consequential = (risk in HIGH_RISK and not safety_stop) or any(
            token in effect for effect in effects
            for token in ("external", "destructive", "credential", "purchase", "delete")
        )
        if consequential and tool.get("requiresApproval") is not True:
            fail(f"consequential tool {name} must require approval")
        for schema_name in ("inputSchema", "outputSchema"):
            schema = tool.get(schema_name)
            if schema is not None and (not isinstance(schema, dict) or schema.get("type", "object") != "object"):
                fail(f"tool {name} {schema_name} must be an object schema")
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    for pattern in SECRET_SHAPES:
        if pattern.search(encoded):
            fail("manifest contains a credential-shaped value")
    print(encoded)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
