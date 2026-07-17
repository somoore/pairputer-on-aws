#!/usr/bin/env python3
"""Generate one trusted, pinned local Docker seccomp profile by policy name."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

POLICIES = {
    "chromium-namespaces-v1": {
        "url": "https://raw.githubusercontent.com/moby/profiles/refs/tags/seccomp/v0.2.1/seccomp/default.json",
        "sha256": "536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74",
        "syscalls": ["clone", "clone3", "unshare"],
    },
}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in POLICIES:
        print(f"usage: {Path(sys.argv[0]).name} <{'|'.join(POLICIES)}> OUTPUT.json", file=sys.stderr)
        return 2
    policy, output = POLICIES[sys.argv[1]], Path(sys.argv[2]).resolve()
    with urllib.request.urlopen(policy["url"], timeout=60) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != policy["sha256"]:
        raise RuntimeError("pinned Moby seccomp profile failed its size or digest check")
    profile = json.loads(raw)
    profile["syscalls"].append({
        "names": policy["syscalls"], "action": "SCMP_ACT_ALLOW",
        "comment": "Chromium namespace sandbox; all other Moby defaults remain enforced",
    })
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pairputer-seccomp-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
