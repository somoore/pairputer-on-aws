#!/usr/bin/env python3
"""Hard live gates for the Agent DOOM capsule brain.

Runs against a local capsule bridge on :6905. This is intentionally separate
from the light behavior harness because it is an autonomy benchmark, not a
basic bridge smoke test.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def call(bridge: str, path: str, body: dict | None = None, *, method: str = "POST", timeout: int = 30) -> tuple[int, int, dict, int]:
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(bridge + path, data=data, method=method, headers={"Content-Type": "application/json"})
    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        elapsed_ms = int((time.time() - start) * 1000)
        return resp.status, len(raw), json.loads(raw or b"{}"), elapsed_ms


def reset_level(bridge: str, *, seed: int = 0, episode: int = 1, map_id: int = 1) -> None:
    call(bridge, "/reset_episode", {"skill": 2, "episode": episode, "map": map_id, "seed": seed}, timeout=10)
    time.sleep(1.0)


def check(name: str, ok: bool, detail: str, failures: list[str]) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  - {detail}")
    if not ok:
        failures.append(name)


def delta(result: dict, key: str, default: int = 0) -> int:
    return int((result.get("delta") or {}).get(key, default) or 0)


def state(result: dict) -> dict:
    return result.get("state") or {}


def drive_goal(bridge: str, goal: str, *, timeout: int = 120, **extra: int | str) -> tuple[int, int, dict, int]:
    body: dict[str, int | str] = {"goal": goal}
    body.update(extra)
    return call(bridge, "/brain/drive_goal", body, timeout=timeout)


def compact_detail(result: dict, elapsed: int) -> str:
    return (
        f"status={result.get('status')} kills={delta(result, 'kills')} "
        f"agent_kills={delta(result, 'agent_kills')} infight={delta(result, 'infight')} "
        f"fired={delta(result, 'fired')} ammo={delta(result, 'ammo')} "
        f"steps={result.get('steps')} tics={result.get('tics')} elapsed={elapsed}ms "
        f"summary={result.get('summary')}"
    )


def run(bridge: str) -> int:
    bridge = bridge.rstrip("/")
    failures: list[str] = []
    print("== agent-doom autonomy gates ==")
    reset_level(bridge)

    st, size, observe, elapsed = call(bridge, "/observe", {"full": False}, timeout=10)
    check("observe compact", st == 200 and size < 1000, f"{size} bytes in {elapsed}ms", failures)

    st, size, result, elapsed = call(bridge, "/brain/drive_ticks", {"objective": "kill first enemy", "max_tics": 560}, timeout=75)
    kill_delta = delta(result, "kills")
    check("first kill under 10s", st == 200 and result.get("status") == "achieved" and kill_delta > 0 and elapsed <= 10_000,
          f"status={result.get('status')} kills={kill_delta} steps={result.get('steps')} elapsed={elapsed}ms", failures)
    check("brain response compact", size < 500, f"{size} bytes", failures)
    check("fire evidence", "kill" in str(result.get("summary", "")).lower() or bool((result.get("state") or {}).get("shootable")),
          str(result.get("summary", "")), failures)

    for seed in (1, 2):
        reset_level(bridge, seed=seed)
        st, size, result, elapsed = drive_goal(bridge, "kill first enemy", timeout=90)
        check(
            f"first kill seed {seed}",
            st == 200 and size < 650 and result.get("status") == "achieved" and delta(result, "kills") > 0 and elapsed <= 20_000,
            f"{compact_detail(result, elapsed)} size={size}",
            failures,
        )

    reset_level(bridge)
    st, size, result, elapsed = drive_goal(bridge, "find an enemy and punch it down without using ammo", timeout=140)
    check(
        "melee no-ammo kill",
        st == 200 and size < 650 and result.get("status") == "achieved" and delta(result, "kills") > 0
        and delta(result, "ammo") >= 0 and int(state(result).get("wp", -1)) == 0,
        f"{compact_detail(result, elapsed)} ammo_delta={delta(result, 'ammo')} wp={state(result).get('wp')} size={size}",
        failures,
    )

    reset_level(bridge)
    st, size, result, elapsed = drive_goal(bridge, "race to the exit without killing anything", timeout=120)
    check(
        "no-kill exit route",
        st == 200 and size < 650 and result.get("status") == "achieved" and delta(result, "fired") == 0 and delta(result, "ammo") >= 0
        and delta(result, "agent_kills") == 0 and int(result.get("steps") or 0) > 0 and int(result.get("tics") or 0) > 0 and elapsed <= 45_000,
        f"{compact_detail(result, elapsed)} size={size}",
        failures,
    )

    reset_level(bridge)
    st, size, result, elapsed = drive_goal(
        bridge,
        "race to the exit and get to next level as fast as you can without killing a bad guy",
        timeout=150,
    )
    final_map = state(result).get("m")
    check(
        "complete level no-kill",
        st == 200 and size < 700 and result.get("status") == "achieved" and delta(result, "fired") == 0 and delta(result, "ammo") >= 0
        and delta(result, "agent_kills") == 0 and final_map not in (None, [1, 1]) and "transition" in str(result.get("summary", "")).lower(),
        f"{compact_detail(result, elapsed)} map={final_map} size={size}",
        failures,
    )

    reset_level(bridge)
    st, size, result, elapsed = drive_goal(bridge, "clear this room safely", timeout=120)
    check(
        "clear room multi-kill",
        st == 200 and size < 650 and result.get("status") == "achieved" and delta(result, "kills") >= 2,
        f"{compact_detail(result, elapsed)} hp_delta={delta(result, 'health')} size={size}",
        failures,
    )

    st, size, status, elapsed = call(bridge, "/brain/map_status", method="GET", timeout=5)
    raw_status = json.dumps(status)
    doors = status.get("doors") or {}
    check("map_status compact", st == 200 and size < 900, f"{size} bytes", failures)
    check("no raw map geometry", all(token not in raw_status for token in ("vertices", "linedefs", "\"lines\":", "\"sectors\":", "\"things\":")),
          f"{size} bytes", failures)
    check("no repeated failed-use loop", int(doors.get("attempts", 0)) <= 12 and len(doors.get("blocked", [])) <= 4,
          json.dumps(doors), failures)

    total = 13
    passed = total - len(failures)
    print(f"\nSUMMARY: {passed}/{total} checks passed; suite {'passed' if not failures else 'failed'}")
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", default="http://127.0.0.1:6905")
    args = parser.parse_args()
    sys.exit(run(args.bridge))
