#!/usr/bin/env python3
"""Idle-takeover autopilot supervisor.

The human is the star: they play DOOM via the relay/input channel. When they step away
(no human input for AUTOPILOT_IDLE_S, published by the input_ws arbiter on :6906 as
`autopilotSuggested`), this loop auto-engages the DoomGuy autopilot — it drives the
capsule's own /brain/drive_goal (:6905), the SAME path Codex/the human-commander use, so
the AI hunts/fights/advances and keeps the game alive.

The handback is NOT this loop's job: any human input resets the arbiter's idle clock
(autopilotSuggested flips false within a poll) AND the input_ws "human always wins" revoke
stops the agent mid-action. This loop simply stops issuing drive calls the moment the human
is back, and drives short bounded bursts so a returning human is never more than one burst
from full control.

Env:
  PAIRPUTER_AUTOPILOT_ENABLE   default "true"  — master switch
  PAIRPUTER_AUTOPILOT_GOAL     the goal handed to the brain when it engages
  PAIRPUTER_AUTOPILOT_BURST_TICS  tics per autopilot drive call (bounded so handback is snappy)
"""
import json
import os
import time
import urllib.request

BRIDGE = os.environ.get("PAIRPUTER_AUTOPILOT_BRIDGE", "http://127.0.0.1:6905")
ARBITER = os.environ.get("PAIRPUTER_AUTOPILOT_ARBITER", "http://127.0.0.1:6906")
ENABLE = os.environ.get("PAIRPUTER_AUTOPILOT_ENABLE", "true").lower() not in ("", "0", "false", "no")
# The demo goal: hunt+fight+advance (the "DoomGuy autopilot" contract). Overridable per deploy.
GOAL = os.environ.get("PAIRPUTER_AUTOPILOT_GOAL", "clear the map of enemies and reach the exit")
# Short bursts: the human returning should regain control within one burst. ~100 tics ≈ a few
# seconds of play, then we re-poll the arbiter before driving again.
BURST_TICS = int(os.environ.get("PAIRPUTER_AUTOPILOT_BURST_TICS", "120"))
POLL_S = float(os.environ.get("PAIRPUTER_AUTOPILOT_POLL_S", "1.0"))


def _log(msg):
    print("[autopilot] %s" % msg, flush=True)


def _get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _post_json(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _human_idle_enough():
    """True when the arbiter says the human has been idle past the takeover threshold."""
    try:
        state = _get_json(ARBITER + "/")
    except Exception:
        return False  # arbiter unreachable -> do NOT auto-drive (fail closed; human-first)
    return bool(state.get("autopilotSuggested"))


def _drive_burst():
    """One bounded autopilot drive. Returns quickly so we re-check the human between bursts."""
    try:
        # trace_recent omitted: this is production driving, not measurement.
        result = _post_json(BRIDGE + "/brain/drive_goal",
                            {"goal": GOAL, "max_tics": BURST_TICS, "source": "autopilot"},
                            timeout=BURST_TICS * 0.25 + 30)
        if str(result.get("stop_reason") or "") == "player_dead":
            _respawn()
        return True
    except Exception as exc:
        _log("drive burst failed: %r" % (exc,))
        return False


def _respawn():
    """Dying is fine — a CORPSE is not. DOOM respawns on a key press (restarting the map);
    without this the demo froze on the death screen forever (soaked: dead at t=60s, corpse
    for the remaining 7 minutes). Press USE a few times: the first dismisses the death view,
    the map restarts, and the autopilot keeps playing."""
    _log("player dead -> respawning (map restarts)")
    for _ in range(3):
        try:
            _post_json(BRIDGE + "/act", {"action": "ACTION_USE", "amount": 1, "duration_tics": 2}, timeout=10)
        except Exception as exc:
            _log("respawn press failed: %r" % (exc,))
            return
        time.sleep(0.3)


def main():
    if not ENABLE:
        _log("disabled (PAIRPUTER_AUTOPILOT_ENABLE=false)")
        return
    _log("supervisor up: idle-takeover -> %r (burst=%d tics)" % (GOAL, BURST_TICS))
    driving = False
    while True:
        try:
            if _human_idle_enough():
                if not driving:
                    _log("human idle past threshold -> autopilot ENGAGE")
                    driving = True
                _drive_burst()
                # loop straight back to re-check the human before the next burst
                continue
            if driving:
                _log("human returned -> autopilot RELEASE (human always wins)")
                driving = False
        except Exception as exc:
            _log("loop error: %r" % (exc,))
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
