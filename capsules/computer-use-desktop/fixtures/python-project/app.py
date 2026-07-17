#!/usr/bin/env python3
"""Tiny deterministic project used by the Workbench evaluation harness."""

from __future__ import annotations

import json
import sys


def summarize(values: list[int]) -> dict[str, int]:
    return {"count": len(values), "sum": sum(values), "maximum": max(values) if values else 0}


def self_test() -> None:
    assert summarize([2, 3, 5]) == {"count": 3, "sum": 10, "maximum": 5}
    assert summarize([]) == {"count": 0, "sum": 0, "maximum": 0}
    print("PYTHON_FIXTURE_OK")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        print(json.dumps(summarize([int(value) for value in sys.argv[1:]]), sort_keys=True))
