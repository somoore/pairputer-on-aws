#!/usr/bin/env python3.11
"""Agent bridge on :6905 — the capsule's agent-facing surface.

HTTP/1.1 + JSON shim in front of restful-doom's in-process gRPC DoomAgent service
(proto/restfuldoom/v1/agent.proto, loopback :50051). Exists because the MicroVM's
authenticated :443 proxy gateway speaks HTTP/1.1 + WebSocket, not gRPC/h2 — so the
protobuf contract stays the engine interface and this shim is transport adaptation
only. The pairputer MCP server calls these routes through the gateway with
X-aws-proxy-auth / X-aws-proxy-port: 6905 (IAM-gated via CreateMicrovmAuthToken),
which is the same trust model that already protects :6903/:6904.

Routes (all JSON in/out):
  GET  /health                     -> {ok, doom_grpc}
  POST /observe   {<ObserveRequest fields>}        -> one GameState (proto JSON)
  POST /act       {<PlayerAction fields>}          -> next GameState after the action
  POST /reset_episode {<ResetEpisodeRequest>}      -> ResetEpisodeResponse
  POST /snapshot/save {<SaveSnapshotRequest>}      -> SnapshotCommandResponse
  POST /snapshot/load {<LoadSnapshotRequest>}      -> SnapshotCommandResponse
  POST /brain/drive {objective,budget,allowed_skills} -> bounded deterministic objective driver
  POST /brain/drive_ticks {objective,max_tics}         -> tic-bounded deterministic objective driver
  POST /brain/drive_goal {goal,max_tics}               -> free-form goal contract driver
  GET  /brain/status                               -> last objective-driver status
  GET  /brain/memory                               -> compact AgentMemory summary
  GET  /brain/map_status                           -> compact planner/map cache summary
  GET  /brain/vision_status                        -> compact triggered vision-sensor summary
  GET  /brain/tactical_status                      -> compact Commander-facing aggregate status
  POST /input     {t:"k"/"m"/"b", ...}             -> Tier 1: forward to input_ws :6904, tagged actor=agent
  GET  /screen                                     -> Tier 1 read_screen: one PNG frame, base64

Proto messages are converted generically with google.protobuf.json_format
(MessageToDict/ParseDict) against the stubs generated at image build from the same
proto the engine was compiled with — no hand-coded field names to drift.
"""
import asyncio
import base64
import json
import math
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/opt/capsule")
sys.path.insert(0, "/opt/capsule/rdgen")

import grpc  # noqa: E402
from google.protobuf.json_format import MessageToDict, ParseDict  # noqa: E402
from restfuldoom.v1 import agent_pb2, agent_pb2_grpc  # noqa: E402
from brain_runtime import brain_memory, brain_status, drive_goal, drive_objective, drive_ticks, map_status, reset_brain_episode, tactical_status, vision_status  # noqa: E402

GRPC_TARGET = "127.0.0.1:50051"
INPUT_WS = "ws://127.0.0.1:6904"
PORT = 6905
GRPC_TIMEOUT_S = 10


def _stub():
    chan = grpc.insecure_channel(GRPC_TARGET)
    return agent_pb2_grpc.DoomAgentStub(chan), chan


def _to_json(msg) -> dict:
    return MessageToDict(msg, preserving_proto_field_name=True)


FP_UNIT = 65536


def _as_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _distance_units(distance_fp):
    return int(_as_int(distance_fp) / FP_UNIT)


def _position(obj):
    return (obj.get("position") or {}) if isinstance(obj, dict) else {}


def _angle_delta(target, current):
    return ((int(round(target)) - int(round(current)) + 540) % 360) - 180


def _bearing_from(src, dst):
    sx, sy = _as_int(src.get("x_fp")), _as_int(src.get("y_fp"))
    dx, dy = _as_int(dst.get("x_fp")) - sx, _as_int(dst.get("y_fp")) - sy
    if dx == 0 and dy == 0:
        return 0
    return int(round(math.degrees(math.atan2(dy, dx)))) % 360


def _compact_object(obj, player_pos=None, player_angle=None, *, include_position=True, include_fp=True):
    pos = _position(obj)
    out = {
        "id": obj.get("id"),
        "type_id": obj.get("type_id"),
        "health": obj.get("health"),
        "distance_units": _distance_units(obj.get("distance_fp")),
    }
    if include_fp:
        out["distance_fp"] = obj.get("distance_fp")
    if include_position:
        out["position"] = {k: pos[k] for k in ("x_fp", "y_fp", "z_fp") if k in pos}
    if "angle_degrees" in obj:
        out["angle_degrees"] = obj.get("angle_degrees")
    if player_pos is not None:
        bearing = _bearing_from(player_pos, pos)
        out["bearing_degrees"] = bearing
        if player_angle is not None:
            out["turn_degrees"] = _angle_delta(bearing, _as_int(player_angle))
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def _nearest_objects(items, *, limit, player_pos=None, player_angle=None,
                     include_position=True, include_fp=True):
    objs = []
    for item in items or []:
        obj = item.get("object") if isinstance(item, dict) and isinstance(item.get("object"), dict) else item
        if isinstance(obj, dict):
            objs.append(obj)
    objs.sort(key=lambda o: _as_int(o.get("distance_fp"), 1 << 60))
    return [_compact_object(o, player_pos, player_angle,
                            include_position=include_position, include_fp=include_fp)
            for o in objs[:limit]]


def _action_state(state: dict) -> dict:
    player = state.get("player") or {}
    pobj = player.get("object") or {}
    ppos = _position(pobj)
    enemies = _nearest_objects(state.get("enemies") or [], limit=3,
                               player_pos=ppos, player_angle=pobj.get("angle_degrees"),
                               include_position=False, include_fp=False)
    enemies = [[e.get("id"), e.get("health"), e.get("distance_units"), e.get("turn_degrees")]
               for e in enemies]
    nav = state.get("navigation") or {}
    level = state.get("level") or {}
    ammo = player.get("ammo") or {}
    weapon = str(player.get("ready_weapon") or "")
    if weapon.startswith("WEAPON_"):
        weapon = weapon[7:]
    combat = state.get("combat") or {}
    combat_hint = {}
    for src, dst in (
        ("has_shootable_target", "shootable"),
        ("target_id", "target"),
        ("target_health", "target_hp"),
    ):
        if src in combat:
            combat_hint[dst] = combat[src]
    return {
        "t": state.get("tick"),
        "p": {
            "a": pobj.get("angle_degrees"),
            "hp": player.get("health"),
            "wp": weapon,
            "bul": ammo.get("bullets"),
        },
        "k": player.get("kills"),
        "kt": level.get("total_kills"),
        "n": [
            "".join(k[0] for k in ("forward", "left", "right") if nav.get(f"{k}_open")),
            _distance_units(nav.get("front_block_distance_fp")),
        ],
        "e": enemies,
        "ec": len(state.get("enemies") or []),
        **({"combat": combat_hint} if combat_hint else {}),
    }


def _observe_state(state: dict) -> dict:
    player = state.get("player") or {}
    pobj = player.get("object") or {}
    ppos = _position(pobj)
    ammo = player.get("ammo") or {}
    weapon = str(player.get("ready_weapon") or "")
    if weapon.startswith("WEAPON_"):
        weapon = weapon[7:]
    nav = state.get("navigation") or {}
    probes = [[p.get("angle_offset_degrees", 0), 1 if p.get("open") else 0,
               _distance_units(p.get("block_distance_fp"))]
              for p in (nav.get("direction_probes") or [])]
    enemies = _nearest_objects(state.get("enemies") or [], limit=6,
                               player_pos=ppos, player_angle=pobj.get("angle_degrees"),
                               include_position=False, include_fp=False)
    enemies = [[e.get("id"), e.get("health"), e.get("type_id"), e.get("distance_units"),
                e.get("bearing_degrees"), e.get("turn_degrees")]
               for e in enemies]
    use_lines = sorted(nav.get("use_lines") or [], key=lambda l: _as_int(l.get("distance_fp"), 1 << 60))
    uses = [[l.get("line_id"), l.get("special"), l.get("tag"), _distance_units(l.get("distance_fp"))]
            for l in use_lines[:3]]
    level = state.get("level") or {}
    return {
        "t": state.get("tick"),
        "p": {
            "x": ppos.get("x_fp"),
            "y": ppos.get("y_fp"),
            "a": pobj.get("angle_degrees"),
            "hp": player.get("health"),
            "wp": weapon,
            "bul": ammo.get("bullets"),
        },
        "m": [level.get("episode"), level.get("map"), level.get("skill")],
        "k": player.get("kills"),
        "kt": level.get("total_kills"),
        "n": {
            "o": "".join(k[0] for k in ("forward", "left", "right") if nav.get(f"{k}_open")),
            "f": _distance_units(nav.get("front_block_distance_fp")),
            "pr": probes,
            "u": uses,
        },
        "e": enemies,
        "ec": len(state.get("enemies") or []),
    }


def _observe(body: dict) -> dict:
    stub, chan = _stub()
    try:
        full = bool((body or {}).get("full")) or str((body or {}).get("detail", "")).lower() == "full"
        req = ParseDict(body or {}, agent_pb2.ObserveRequest(), ignore_unknown_fields=True)
        stream = stub.Observe(req, timeout=GRPC_TIMEOUT_S)
        state = next(iter(stream))  # latest snapshot: first state off the stream
        out = _to_json(state)
        return out if full else _observe_state(out)
    finally:
        chan.close()


# The engine applies ONE queued PlayerAction per game tic (AgentBridge_ApplyTiccmd). So a single action
# = a single tic of movement = a TAP. Humans HOLD W/A/S/D. To make one agent call a real sustained burst,
# the bridge queues the action `hold_tics` times over the GameSession stream — the engine holds the key
# for that many tics, then movement stops (auto-releases; no sticky-key possible, each tic is explicit).
HOLD_MAX_TICS = 70                 # ~2s at 35 tics/s — hard safety cap so a hold can never run away
MOVE_ACTIONS = {"ACTION_FORWARD", "ACTION_BACKWARD", "ACTION_TURN_LEFT", "ACTION_TURN_RIGHT",
                "ACTION_STRAFE_LEFT", "ACTION_STRAFE_RIGHT"}
DEFAULT_HOLD = {"move": 10, "shoot": 12, "use": 4, "instant": 1}


def _act(body: dict) -> dict:
    # Strict parse: an unknown field (a wrong-shaped guess like {"attack":true}) would otherwise be
    # SILENTLY dropped -> an empty PlayerAction -> DOOM does nothing, but we'd return 200 and the agent
    # would wrongly report success. Reject it so the agent gets a real error + the correct vocabulary.
    b = dict(body or {})
    # hold_tics is a pairputer bridge concept (how long to HOLD the action), separate from the proto's
    # `amount` (per-tic magnitude). Pull it out before proto parsing so it isn't an "unknown field".
    hold_req = b.pop("hold_tics", None)
    detail = str(b.pop("detail", "")).lower()
    full = bool(b.pop("full", False)) or detail == "full"
    if b.pop("hold", False) and hold_req is None:
        hold_req = HOLD_MAX_TICS // 2   # {"hold":true} without a count -> a solid ~1s hold
    try:
        action = ParseDict(b, agent_pb2.PlayerAction(), ignore_unknown_fields=False)
    except Exception as exc:
        raise ValueError(
            f"unknown action payload {b!r}: {exc}. Use {{\"action\":\"ACTION_SHOOT\"}} (or ACTION_FORWARD, "
            "ACTION_BACKWARD, ACTION_TURN_LEFT/RIGHT, ACTION_STRAFE_LEFT/RIGHT, ACTION_USE, "
            "ACTION_SWITCH_WEAPON), optional \"amount\":<int> (per-tic strength) and \"hold_tics\":<int> "
            "(how long to hold, 1-70; movement defaults to ~10 = a real step, not a tap).")
    # A well-formed but effect-less action (no action set AND no raw/keys/mouse overlay) is also a no-op.
    action_name = agent_pb2.PlayerActionType.Name(action.action) if action.action else ""
    if (action.action == agent_pb2.ACTION_UNSPECIFIED
            and not action.HasField("raw") and not action.keys and not action.HasField("mouse")):
        raise ValueError(
            "empty action (nothing set). Provide {\"action\":\"ACTION_SHOOT\"} or another ACTION_* verb.")
    # Resolve the hold: explicit hold_tics wins; else movement, shoot, and use get short reliable holds.
    if hold_req is not None:
        hold = max(1, min(int(hold_req), HOLD_MAX_TICS))
    elif action_name == "ACTION_SHOOT":
        hold = DEFAULT_HOLD["shoot"]
    elif action_name == "ACTION_USE":
        hold = DEFAULT_HOLD["use"]
    else:
        hold = DEFAULT_HOLD["move"] if action_name in MOVE_ACTIONS else DEFAULT_HOLD["instant"]
    stub, chan = _stub()
    try:
        state = _run_held_action(stub, action, hold)
        out = _to_json(state) if state is not None else {}
        out = out if full else _action_state(out)
        out["_held_tics"] = hold   # tell the agent how long it actually held (for its loop reasoning)
        return out
    finally:
        chan.close()


def _run_held_action(stub, action, hold: int):
    # Direct /act favors exact, non-sticky semantics over speed. The brain has its own
    # tic-bounded runner; this endpoint is the user/tool primitive and must behave like
    # explicit held key tics.
    state = None
    one = agent_pb2.PlayerAction()
    one.CopyFrom(action)
    one.duration_tics = 1
    for _ in range(max(1, int(hold))):
        state = next(iter(stub.GameSession(iter([one]), timeout=GRPC_TIMEOUT_S)))
    return state


def _unary(body: dict, req_cls, rpc_name: str) -> dict:
    stub, chan = _stub()
    try:
        req = ParseDict(body or {}, req_cls(), ignore_unknown_fields=True)
        resp = getattr(stub, rpc_name)(req, timeout=GRPC_TIMEOUT_S)
        return _to_json(resp)
    finally:
        chan.close()


def _forward_input(body: dict) -> dict:
    """Tier 1: forward input event(s) to input_ws, actor identity attached to every event.

    Accepts either one event ({t:"k",...}) or a batch ({"events":[...]}) — batching matters
    because each MCP round-trip crosses the VM gateway; typing a word is one call, not ten.
    """
    import websockets  # already in the capsule requirements (input_ws server)

    events = body.get("events") if isinstance(body, dict) and isinstance(body.get("events"), list) else [body or {}]
    for e in events:
        e["actor"] = "agent"  # actor identity per interaction.md — audit + ghost-cursor hook

    # Connection auth: input_ws only honors actor=agent from a connection that proves it can read the
    # in-VM key file (provisioned by start.sh). Without the handshake our events get forced to human.
    key = ""
    try:
        with open(os.environ.get("PAIRPUTER_AGENT_KEY_FILE", "/run/pairputer/agent-input.key")) as fh:
            key = fh.read().strip()
    except Exception:
        pass

    async def send():
        async with websockets.connect(INPUT_WS, open_timeout=3, close_timeout=1) as ws:
            if key:
                await ws.send(json.dumps({"t": "auth", "key": key}))
            for e in events:
                await ws.send(json.dumps(e))
                await asyncio.sleep(0.01)  # keep key ordering deterministic for the X server

    asyncio.run(send())
    return {"ok": True, "forwarded": len(events)}


def _screen() -> dict:
    """Tier 1 read_screen: one PNG frame off the live display via ffmpeg x11grab."""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab",
         "-i", ":1", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, timeout=10,
    )
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"x11grab failed: {p.stderr.decode(errors='replace')[:200]}")
    return {"format": "png", "b64": base64.b64encode(p.stdout).decode()}


def _grpc_alive() -> bool:
    try:
        stub, chan = _stub()
        try:
            grpc.channel_ready_future(chan).result(timeout=2)
            return True
        finally:
            chan.close()
    except Exception:
        return False


def _reset_episode(body: dict) -> dict:
    reset_brain_episode()
    return _unary(body, agent_pb2.ResetEpisodeRequest, "ResetEpisode")


def _set_autopilot(body):
    """Toggle idle-assist from chat. Forwards to the input arbiter (:6906) which owns
    the flag; the autopilot supervisor reads it. body: {enabled: true|false}."""
    import urllib.request
    enabled = bool((body or {}).get("enabled", True))
    req = urllib.request.Request("http://127.0.0.1:6906/autopilot",
                                 data=json.dumps({"enabled": enabled}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            out = json.loads(r.read() or b"{}")
    except Exception as exc:
        return {"error": "autopilot toggle failed: %r" % (exc,)}
    return {"ok": True, "autopilotEnabled": out.get("autopilotEnabled", enabled),
            "message": "Idle assist %s." % ("enabled" if out.get("autopilotEnabled", enabled) else "disabled")}


ROUTES = {
    ("POST", "/observe"): lambda b: _observe(b),
    ("POST", "/autopilot"): lambda b: _set_autopilot(b),
    ("POST", "/act"): lambda b: _act(b),
    ("POST", "/reset_episode"): lambda b: _reset_episode(b),
    ("POST", "/snapshot/save"): lambda b: _unary(b, agent_pb2.SaveSnapshotRequest, "SaveSnapshot"),
    ("POST", "/snapshot/load"): lambda b: _unary(b, agent_pb2.LoadSnapshotRequest, "LoadSnapshot"),
    ("POST", "/brain/drive"): lambda b: drive_objective(b),
    ("POST", "/brain/drive_ticks"): lambda b: drive_ticks(b),
    ("POST", "/brain/drive_goal"): lambda b: drive_goal(b),
    ("GET", "/brain/status"): lambda b: brain_status(),
    ("POST", "/brain/status"): lambda b: brain_status(),
    ("GET", "/brain/memory"): lambda b: brain_memory(),
    ("POST", "/brain/memory"): lambda b: brain_memory(),
    ("GET", "/brain/map_status"): lambda b: map_status(),
    ("POST", "/brain/map_status"): lambda b: map_status(),
    ("GET", "/brain/vision_status"): lambda b: vision_status(),
    ("POST", "/brain/vision_status"): lambda b: vision_status(),
    ("GET", "/brain/tactical_status"): lambda b: tactical_status(),
    ("POST", "/brain/tactical_status"): lambda b: tactical_status(),
    ("POST", "/input"): lambda b: _forward_input(b),
    ("GET", "/screen"): lambda b: _screen(),
    ("GET", "/health"): lambda b: {"ok": True, "doom_grpc": _grpc_alive(), "ts": int(time.time())},
}


class H(BaseHTTPRequestHandler):
    def _dispatch(self, method: str):
        handler = ROUTES.get((method, self.path))
        if handler is None:
            return self._send(404, {"error": f"no route {method} {self.path}"})
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            return self._send(200, handler(body))
        except Exception as exc:  # surface the real error to the MCP server for diagnosis
            return self._send(502, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[bridge]", fmt % args, file=sys.stderr)


if __name__ == "__main__":
    print(f"[bridge] agent bridge on :{PORT} -> gRPC {GRPC_TARGET}", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
