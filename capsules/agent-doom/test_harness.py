#!/usr/bin/env python3
"""Local behavior harness for the agent-doom capsule — drives the bridge like the LLM would and asserts
REAL game-state deltas. Runs in seconds against a local capsule (docker), no MCP/LLM/AWS.

Usage (after ./substrate/local-dev.sh --capsule-only, or any capsule with the bridge on :6905):
    python3 capsules/agent-doom/test_harness.py            # full suite
    python3 capsules/agent-doom/test_harness.py --bridge http://127.0.0.1:6905

What it proves: strict-parse rejects bad payloads (no silent no-op), ACTION_SHOOT drops ammo,
movement actually moves, and HOLD (hold_tics / movement default) produces a bigger step than a 1-tic tap.
It also verifies the high-level brain endpoint accepts an objective and returns compact bounded status.
"""
import argparse
import json
import sys
import time
import urllib.request

BRIDGE = "http://127.0.0.1:6905"


def call(path, body=None, method="POST"):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(BRIDGE + path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def observe():
    st, body = call("/observe", {"full": True})
    assert st == 200, f"/observe failed: {st} {body}"
    return body


def player(o):
    p = o["player"]
    pos = p["object"]["position"]
    return {
        "x": int(pos.get("x_fp", 0)), "y": int(pos.get("y_fp", 0)),
        "bullets": p.get("ammo", {}).get("bullets"),
        "weapon": p.get("ready_weapon"),
        "health": p.get("health"),
        "tick": int(o.get("tick", 0)),
    }


def dist(a, b):
    return int((((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5) / 65536)


def ensure_in_level():
    """The capsule boots straight into E1M1 (-warp), so just wait for a live player to be observable
    and for the weapon to be ready (not mid-raise). No menu navigation needed."""
    for _ in range(30):
        try:
            p = observe().get("player", {})
            if p.get("health") and p.get("ready_weapon"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def reset_episode(seed=0):
    st, body = call("/reset_episode", {"skill": 2, "episode": 1, "map": 1, "seed": seed})
    if st != 200:
        return False, body
    time.sleep(1)
    return ensure_in_level(), body


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")


def run():
    print("== agent-doom behavior harness ==")
    print(f"bridge: {BRIDGE}")
    st, health = call("/health", method="GET")
    check("bridge /health ok", st == 200 and health.get("ok"), json.dumps(health))
    check("gRPC alive (doom_grpc)", bool(health.get("doom_grpc")), json.dumps(health))

    if not ensure_in_level():
        print("!! could not reach a live level (player has no health) — is DOOM rendering?")
        print("SUMMARY:", len(PASS), "passed,", len(FAIL) + 1, "failed")
        return 1

    # 1. strict parse: a bad guess must ERROR, not silently succeed.
    st, body = call("/act", {"attack": True})
    check("bad payload {'attack':true} is rejected (no silent no-op)",
          st != 200 and "unknown action" in json.dumps(body).lower(), f"{st} {body.get('error','')[:80]}")

    # 2. empty action rejected.
    st, body = call("/act", {})
    check("empty action rejected", st != 200, f"{st}")

    # 3. ACTION_SHOOT actually fires (ammo drops).
    b0 = player(observe())["bullets"]
    call("/act", {"action": "ACTION_SHOOT"})
    time.sleep(0.4)
    b1 = player(observe())["bullets"]
    check("ACTION_SHOOT drops ammo", (b0 is not None and b1 is not None and b1 < b0),
          f"bullets {b0} -> {b1}")

    # 4. movement moves the player. Reset first so geometry from the prior shot test
    # does not turn a movement check into a wall-collision check.
    reset_episode()
    p0 = player(observe())
    call("/act", {"action": "ACTION_FORWARD"})
    time.sleep(0.4)
    p1 = player(observe())
    moved_default = dist(p0, p1)
    check("ACTION_FORWARD moves the player", moved_default > 0, f"{moved_default} units")

    # 5. HOLD: an explicit long hold_tics moves FARTHER than a 1-tic tap (the tap-vs-hold fix).
    reset_episode()
    p0 = player(observe())
    call("/act", {"action": "ACTION_FORWARD", "hold_tics": 1})
    time.sleep(0.3)
    tap = dist(p0, player(observe()))
    p0 = player(observe())
    call("/act", {"action": "ACTION_FORWARD", "hold_tics": 30})
    time.sleep(0.3)
    held = dist(p0, player(observe()))
    check("hold_tics=30 travels farther than hold_tics=1 (sustained, not a tap)",
          held > tap * 2, f"tap={tap} vs held={held} units")

    # 6. the response reports how long it held (for the agent's loop reasoning).
    st, body = call("/act", {"action": "ACTION_FORWARD", "hold_tics": 12})
    check("response reports _held_tics", body.get("_held_tics") == 12, f"_held_tics={body.get('_held_tics')}")

    # 7. brain endpoint exists and is bounded/compact. This may not achieve the objective in one step,
    # but it must not error or return a huge raw protobuf blob.
    st, body = call("/brain/drive", {"objective": "move and explore", "budget": 1})
    check("brain drive endpoint returns bounded objective status",
          st == 200 and body.get("status") in {"achieved", "budget_exhausted", "interrupted", "failed"},
          f"{st} {body}")
    check("brain drive result is compact", len(json.dumps(body)) < 2000, f"{len(json.dumps(body))} bytes")

    st, body = call("/brain/map_status", method="GET")
    raw = json.dumps(body)
    check("map_status endpoint returns compact planner status", st == 200 and len(raw) < 1200, f"{st} {len(raw)} bytes")
    check("map_status does not expose raw geometry",
          all(token not in body.get("cache", {}) for token in ("vertices", "lines", "sectors", "things")),
          raw[:120])

    print(f"\nSUMMARY: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default=BRIDGE)
    args = ap.parse_args()
    BRIDGE = args.bridge.rstrip("/")
    sys.exit(run())
