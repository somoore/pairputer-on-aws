#!/usr/bin/env python3.11
"""Tee one service's metadata-only diagnostics into a bounded runtime log."""

from __future__ import annotations

import os
import sys
from pathlib import Path


MAX_BYTES = 8 * 1024 * 1024
KEEP_BYTES = 4 * 1024 * 1024


def _compact(path: Path) -> None:
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        with path.open("rb") as source:
            source.seek(-KEEP_BYTES, os.SEEK_END)
            retained = source.read(KEEP_BYTES)
        temporary = path.with_name(path.name + ".rotate")
        with temporary.open("wb") as target:
            target.write(retained)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except FileNotFoundError:
        return


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    path = Path(sys.argv[1])
    if path.parent != Path("/var/log") or not path.name.startswith("pairputer-"):
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    _compact(path)
    log = path.open("ab", buffering=0)
    try:
        os.chmod(path, 0o600)
        while True:
            chunk = sys.stdin.buffer.read1(65536)
            if not chunk:
                break
            log.write(chunk)
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            if log.tell() > MAX_BYTES:
                log.flush()
                os.fsync(log.fileno())
                log.close()
                _compact(path)
                log = path.open("ab", buffering=0)
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
