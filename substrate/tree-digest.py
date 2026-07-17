#!/usr/bin/env python3
"""Digest a build-context tree while rejecting symlinks and special files."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1]).resolve(strict=True)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError(f"capsule context contains unsupported file type: {relative.decode()}")
        digest.update(b"D\0" if stat.S_ISDIR(info.st_mode) else b"F\0")
        digest.update(relative + b"\0")
        digest.update(str(stat.S_IMODE(info.st_mode)).encode() + b"\0")
        if stat.S_ISREG(info.st_mode):
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    print(digest.hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
