#!/usr/bin/env python3.11
"""Bounded HTTP/JSON adapter to the loopback DesktopAgent gRPC service."""

from __future__ import annotations

import json
import os
import base64
import asyncio
import hmac
import socket
import subprocess
import time
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict

from control_client import ControlClient
from desktopgen.pairputer.desktop.v1 import desktop_pb2, desktop_pb2_grpc
from desktopd_adapters import production_services
from services.browser_service import strict_browser_query_request
from state_fusion import StateFusion
import brain as brain_api

HOST = os.environ.get("PAIRPUTER_BRIDGE_BIND", "0.0.0.0")
PORT = 6905
GRPC_TARGET = "127.0.0.1:50051"
GRPC_KEY_FILE = os.environ.get("PAIRPUTER_DESKTOP_AGENT_KEY_FILE", "/run/pairputer/desktop-agent.key")
BRIDGE_KEY_FILE = os.environ.get(
    "PAIRPUTER_BRIDGE_CAPABILITY_FILE", "/run/pairputer/bridge-ingress.key"
)
MAX_BODY = int(os.environ.get("PAIRPUTER_BRIDGE_MAX_BODY", "1048576"))
MAX_RESPONSE = int(os.environ.get("PAIRPUTER_BRIDGE_MAX_RESPONSE", "2097152"))

ROUTES = {
    "/workspace/list": "workspace_list", "/workspace/describe": "workspace_describe",
    "/workspace/read": "workspace_read", "/workspace/write": "workspace_write",
    "/workspace/mkdir": "workspace_mkdir",
    "/workspace/upload": "workspace_upload",
    "/workspace/patch": "workspace_patch", "/workspace/move": "workspace_move",
    "/workspace/trash": "workspace_trash", "/process/run": "process_start",
    "/process/status": "process_status", "/process/cancel": "process_cancel",
    "/apps/open": "app_open", "/windows/list": "window_list", "/windows/focus": "window_focus",
    "/browser/open": "browser_open", "/browser/observe": "browser_observe",
    "/browser/action": "browser_action", "/browser/query": "browser_action",
    "/accessibility/tree": "accessibility_tree",
    "/accessibility/action": "accessibility_action", "/screenshot": "screenshot",
    "/artifacts/export": "artifact_export", "/computer/action": "computer_action",
}
ENVELOPE_FIELDS = {"task_id", "step_id", "action_id", "expected_world_revision",
                   "expected_human_epoch", "idempotency_key", "presentation_mode", "deadline_unix_ms"}

# Brain-domain rejections that are the CALLER's fault, not a bridge crash: map them to a clean 409
# with a machine code instead of an opaque 500, so a model can tell "rejected because X" from "the
# bridge crashed". Matched by TYPE NAME (import-free — the runtime owns these classes and importing
# them here risks a cycle). KeyError from a task lookup = unknown id. The base tuple is broad enough
# to catch the runtime's RuntimeError/PermissionError subclasses; _is_brain_client_error() then
# gates on the known NAMES so a genuine bug still falls through to the 500 path.
_BRAIN_CLIENT_ERRORS = (KeyError, RuntimeError, PermissionError)
_BRAIN_ERROR_CODES = {
    "KeyError": "unknown_task",
    "ActiveTaskConflict": "task_already_active",
    "TaskAlreadyRunning": "task_already_running",
    "FreezeBarrier": "frozen",
    "IdleResumeDenied": "idle_resume_denied",
}


def _is_brain_client_error(exc):
    return type(exc).__name__ in _BRAIN_ERROR_CODES


def _brain_error_code(exc):
    return _BRAIN_ERROR_CODES.get(type(exc).__name__, "task_rejected")


# Air-gap: the bridge (unprivileged) only writes desired state here; a root
# reconciler enforces it on iptables. Status is read from the marker root writes.
AIRGAP_INTENT_FILE = os.environ.get("PAIRPUTER_AIRGAP_INTENT_FILE", "/run/pairputer/brain/airgap.intent")
AIRGAP_STATE_FILE = os.environ.get("PAIRPUTER_AIRGAP_STATE_FILE", "/run/pairputer/airgap.state")


def _airgap_enforced():
    """Enforced truth as the root reconciler last wrote it ('on'|'off'|'unknown')."""
    try:
        with open(AIRGAP_STATE_FILE, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return "unknown"
    return value if value in {"on", "off"} else "unknown"


def _airgap_intent():
    try:
        with open(AIRGAP_INTENT_FILE, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value if value in {"on", "off"} else None


AIRGAP_DETAIL_FILE = os.environ.get("PAIRPUTER_AIRGAP_DETAIL_FILE", "/run/pairputer/airgap.detail")


def _airgap_detail():
    try:
        with open(AIRGAP_DETAIL_FILE, encoding="utf-8") as handle:
            return handle.read()[:2000]
    except OSError:
        return ""


def _airgap_snapshot():
    enforced = _airgap_enforced()
    intent = _airgap_intent()
    return {
        "enabled": enforced == "on",
        "enforced": enforced,
        "intent": intent if intent is not None else enforced,
        "converging": intent is not None and intent != enforced,
        "detail": _airgap_detail(),
    }


def _set_airgap(body):
    """Record the desired air-gap state; the root reconciler applies it in ~1s."""
    if set(body) - {"enabled"} or "enabled" not in body:
        raise ValueError("network_airgap requires only a boolean 'enabled'")
    want = "on" if bool(body["enabled"]) else "off"
    os.makedirs(os.path.dirname(AIRGAP_INTENT_FILE), exist_ok=True)
    tmp = AIRGAP_INTENT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(want + "\n")
    os.replace(tmp, AIRGAP_INTENT_FILE)
    snap = _airgap_snapshot()
    snap["ok"] = True
    snap["requested"] = want
    return snap
class _ControlStateReader:
    """Read the root-owned cross-process epoch without obtaining mutation rights."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "control-state.json")

    def snapshot(self):
        with open(self.path, encoding="utf-8") as handle:
            value = json.load(handle)
        epoch, revision = int(value["humanEpoch"]), int(value["worldRevision"])
        owner = str(value["owner"])
        if epoch < 0 or revision < 0 or owner not in {"idle", "human", "agent"}:
            raise RuntimeError("invalid authoritative control state")
        return {"humanEpoch": epoch, "worldRevision": revision, "owner": owner,
                "updatedAt": float(value.get("updatedAt", 0))}


CONTROL = _ControlStateReader(os.environ.get("PAIRPUTER_CONTROL_STATE_DIR", "/run/pairputer"))
PREEMPT_SOCKET = os.environ.get("PAIRPUTER_BRAIN_PREEMPT_SOCKET", "/run/pairputer/brain-preempt.sock")

# Routes that support the frictionless "drop code into the sandbox, then open it" flow: when the caller
# omits the anti-drift envelope, we fill it from the current control state (unconditional action). The
# write-family AND the low-risk local navigation/open routes — so "write a page then open it" is a
# two-call flow with no envelope ceremony. (Effectful external-commit routes are deliberately excluded.)
_AUTOFILL_ENVELOPE_ROUTES = frozenset({
    "/workspace/write", "/workspace/mkdir", "/workspace/patch", "/workspace/upload",
    "/browser/open", "/apps/open", "/windows/focus",
})


def _autofill_write_envelope(body):
    """Fill the anti-drift/idempotency envelope from current state ONLY for fields the caller omitted.
    A caller that passes expected_human_epoch/expected_world_revision keeps the exact-consent check;
    one that omits them gets an unconditional write against the current epoch/revision. action_id /
    idempotency_key are synthesized when absent (they're bookkeeping, not consent)."""
    need_epoch = "expected_human_epoch" not in body
    need_rev = "expected_world_revision" not in body
    if need_epoch or need_rev:
        try:
            snap = CONTROL.snapshot()
        except Exception:
            snap = {"humanEpoch": 0, "worldRevision": 0}
        if need_epoch:
            body["expected_human_epoch"] = int(snap.get("humanEpoch") or 0)
        if need_rev:
            body["expected_world_revision"] = int(snap.get("worldRevision") or 0)
    body.setdefault("action_id", "auto-" + uuid.uuid4().hex[:12])
    body.setdefault("idempotency_key", "auto-" + uuid.uuid4().hex[:12])


class _BrainLoop:
    """Own the brain's asyncio worker on one durable loop.

    ``ThreadingHTTPServer`` creates a thread per request. Calling ``asyncio.run``
    in those threads would cancel the background worker after every response and
    bind its locks to different loops. All orchestration calls are therefore
    marshalled onto this one loop.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="desktop-brain-loop", daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("desktop brain event loop did not start")
        self.call(self._initialize(), timeout=15)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    async def _initialize(self):
        authoritative_epoch = lambda: int(CONTROL.snapshot()["humanEpoch"])
        authoritative_revision = lambda: int(CONTROL.snapshot()["worldRevision"])
        state_fusion = StateFusion(
            human_epoch_provider=authoritative_epoch,
            world_revision_provider=authoritative_revision,
        )
        services = production_services()
        state_fusion.register("desktopd", services["desktopd"].observe)
        control = ControlClient(
            world_revision_provider=authoritative_revision,
            authoritative_epoch_provider=authoritative_epoch,
        )
        instance = brain_api.get_brain(
            state_fusion=state_fusion, control=control, services=services,
        )
        await instance.start()
        state = CONTROL.snapshot()
        instance.runtime.control.synchronize_human_epoch(
            state["humanEpoch"], "shared_control_startup")

    def call(self, coroutine, *, timeout=30):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=timeout)


async def _brain_status(task_id):
    return brain_api.task_status(task_id)


async def _brain_approve(approval_id):
    return brain_api.approve_action(approval_id)


async def _brain_sync_epoch(epoch, event_type):
    return brain_api.get_brain().runtime.control.synchronize_human_epoch(epoch, event_type)


BRAIN = _BrainLoop()


def _preemption_listener():
    """Consume the input arbiter's high-priority local datagrams.

    The socket is guest-local, root-owned, and mode 0600. Shared ``ControlState``
    remains authoritative for primitive service commits; this listener revokes
    the brain's in-process lease and journals the same epoch for task execution.
    """

    try:
        os.unlink(PREEMPT_SOCKET)
    except FileNotFoundError:
        pass
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
        listener.bind(PREEMPT_SOCKET)
        os.chmod(PREEMPT_SOCKET, 0o600)
        listener.settimeout(0.05)
        while True:
            try:
                raw = listener.recv(4096)
                event = json.loads(raw)
                if event.get("type") == "human_preempt":
                    epoch, event_type = int(event["humanEpoch"]), "authenticated_human_input"
                else:
                    continue
            except socket.timeout:
                epoch, event_type = int(CONTROL.snapshot()["humanEpoch"]), "authoritative_epoch_poll"
            except Exception as exc:
                print(f"[agent_bridge] preemption event rejected: {type(exc).__name__}", flush=True)
                continue
            try:
                BRAIN.call(_brain_sync_epoch(epoch, event_type), timeout=2)
            except Exception as exc:
                print(f"[agent_bridge] preemption sync failed closed: {type(exc).__name__}", flush=True)


threading.Thread(target=_preemption_listener, name="brain-preemption-listener", daemon=True).start()


def _agent_input(events, target_proof, expected_human_epoch, expected_world_revision):
    if not isinstance(events, list) or not events or len(events) > 32:
        raise ValueError("events must be a non-empty list of at most 32 events")
    for name, value in (("expected_human_epoch", expected_human_epoch),
                        ("expected_world_revision", expected_world_revision)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    from websocket import create_connection
    try:
        key = open(os.environ.get("PAIRPUTER_AGENT_KEY_FILE", "/run/pairputer/agent-input.key"),
                   encoding="utf-8").read().strip()
    except OSError as exc:
        raise RuntimeError("agent input credential unavailable") from exc
    # This is the privileged local agent channel, not a browser WebSocket.
    # Suppressing Origin lets input_ws reject every browser-originated socket
    # while retaining this explicit key-authenticated path.
    ws = create_connection("ws://127.0.0.1:6904", timeout=2, http_proxy_host=None,
                           suppress_origin=True)
    receipts = []
    try:
        ws.send(json.dumps({"t": "auth", "key": key}))
        auth = json.loads(ws.recv())
        if not auth.get("authenticated"):
            raise RuntimeError("agent input authentication failed")
        for offset in range(0, len(events), 32):
            batch = {"t": "batch", "sequence": len(receipts) + 1,
                     "expected_human_epoch": expected_human_epoch,
                     "display_revision": expected_world_revision,
                     "target_proof": target_proof,
                     "events": events[offset:offset + 32]}
            ws.send(json.dumps(batch, separators=(",", ":")))
            receipt = json.loads(ws.recv())
            receipts.append(receipt)
            if not receipt.get("accepted"):
                break
    finally:
        ws.close()
    accepted = sum(int(item.get("acceptedEvents", 0)) for item in receipts)
    dropped = len(events) - accepted
    return {"ok": dropped == 0, "acceptedEvents": accepted, "droppedEvents": dropped,
            "receipts": receipts,
            "humanEpoch": receipts[-1].get("humanEpoch", expected_human_epoch)
                if receipts else expected_human_epoch,
            "worldRevision": receipts[-1].get("worldRevision", expected_world_revision)
                if receipts else expected_world_revision}


def _raw_input(events):
    """Open CUA path: submit coordinate events as the 'agent_raw' actor over the same
    key-authenticated :6904 channel, with mode='raw' so input_ws skips target_proof/epoch.
    The human-first cooldown still applies server-side, so the human always wins."""
    if not isinstance(events, list) or not events or len(events) > 64:
        raise ValueError("events must be a non-empty list of at most 64 events")
    from websocket import create_connection
    try:
        key = open(os.environ.get("PAIRPUTER_AGENT_KEY_FILE", "/run/pairputer/agent-input.key"),
                   encoding="utf-8").read().strip()
    except OSError as exc:
        raise RuntimeError("agent input credential unavailable") from exc
    ws = create_connection("ws://127.0.0.1:6904", timeout=2, http_proxy_host=None,
                           suppress_origin=True)
    receipts = []
    try:
        ws.send(json.dumps({"t": "auth", "key": key}))
        if not json.loads(ws.recv()).get("authenticated"):
            raise RuntimeError("agent input authentication failed")
        for offset in range(0, len(events), 32):
            ws.send(json.dumps({"t": "batch", "mode": "raw", "sequence": len(receipts) + 1,
                                "events": events[offset:offset + 32]}, separators=(",", ":")))
            receipt = json.loads(ws.recv())
            receipts.append(receipt)
            if not receipt.get("accepted"):
                break
    finally:
        ws.close()
    accepted = sum(int(item.get("acceptedEvents", 0)) for item in receipts)
    return {"ok": accepted == len(events), "acceptedEvents": accepted,
            "droppedEvents": len(events) - accepted, "receipts": receipts}


def _computer_action(body):
    """Translate a stock CUA action (OpenAI/Anthropic) into events and inject them.
    Drop-in: {action:'click',x,y} / {action:'type',text} / {action:'key',keys} / wait / etc."""
    from cua_adapter import to_events, CuaError
    actions = body.get("actions")
    if actions is None:
        actions = [body]  # single-action convenience form
    if not isinstance(actions, list) or not actions or len(actions) > 32:
        raise ValueError("actions must be a non-empty list of at most 32 CUA actions")
    all_events, total_wait = [], 0
    for action in actions:
        try:
            events, wait_ms = to_events(action)
        except CuaError as exc:
            raise ValueError(str(exc)) from exc
        all_events.extend(events)
        total_wait += wait_ms
    # _raw_input caps at 64 events/call; a long `type` easily exceeds that (2 events/char).
    # Chunk the event stream so long typing succeeds instead of being rejected wholesale.
    accepted = 0
    receipts = []
    ok = True
    for i in range(0, len(all_events), 60):
        chunk = all_events[i:i + 60]
        r = _raw_input(chunk)
        accepted += int(r.get("acceptedEvents", 0))
        receipts.extend(r.get("receipts", []))
        if not r.get("ok"):
            ok = False
            break  # stop on first rejected chunk (e.g. human took over)
    if total_wait:
        time.sleep(min(total_wait, 10000) / 1000.0)
    return {"ok": ok and accepted == len(all_events), "acceptedEvents": accepted,
            "droppedEvents": len(all_events) - accepted, "receipts": receipts,
            "waitedMs": total_wait}


def _screen():
    # See observers/screen.py: 15s cap + fast x11grab setup so a boot-time-contended single-frame
    # grab doesn't spuriously time out (the observe/screenshot "flaky for the first minute" bug).
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
        "-probesize", "32", "-thread_queue_size", "8", "-f", "x11grab",
        "-i", os.environ.get("DISPLAY", ":1") + ".0", "-frames:v", "1", "-f", "image2pipe",
        "-vcodec", "png", "pipe:1"], capture_output=True, timeout=15, check=False)
    if proc.returncode or not proc.stdout or len(proc.stdout) > 8 * 1024 * 1024:
        raise RuntimeError("bounded screen capture failed")
    return {"ok": True, "format": "png", "b64": base64.b64encode(proc.stdout).decode("ascii")}


def _current_cursor():
    try:
        with urllib.request.urlopen("http://127.0.0.1:6906/", timeout=1) as response:
            state = json.loads(response.read(65536))
        return float(state["cursorX"]), float(state["cursorY"])
    except Exception:
        return None


def _glide_to(x, y):
    """Visible presentation (hybrid, the default): sweep the REAL cursor to where a semantic action
    just landed. Real XTEST motion through the agent_raw path means real frames in the stream and
    the truthful owner=agent halo — the human watches the agent work without asking for it. Bounded
    (~300ms), best-effort: presentation must never fail or slow the action meaningfully."""
    try:
        cur = _current_cursor() or (x - 240.0, y - 160.0)
        steps = 10
        for i in range(1, steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)  # smoothstep
            _raw_input([{"t": "m",
                         "x": int(cur[0] + (x - cur[0]) * ease),
                         "y": int(cur[1] + (y - cur[1]) * ease)}])
            time.sleep(0.03)
    except Exception:
        pass


# Semantic action routes that MOVE the cursor to a screen target when the result carries one.
# computer_action already injects real XTEST motion (skip — don't double-move). Read-only routes
# (list/read/describe/observe/screenshot/query/status) have no visible effect and are skipped.
_PRESENT_GLIDE_ROUTES = frozenset({
    "/browser/open", "/browser/action", "/accessibility/action",
    "/apps/open", "/windows/focus",
})
# Effectful routes with NO natural screen location — keep the halo lit (owner=agent) so presence
# stays continuous while the agent works in the shell / filesystem, without faking a destination.
_PRESENT_KEEPALIVE_ROUTES = frozenset({
    "/workspace/write", "/workspace/upload", "/workspace/mkdir", "/workspace/patch",
    "/workspace/move", "/workspace/trash", "/process/run", "/artifacts/export",
})


def _present_action(path, payload, presentation_mode):
    """Visible-by-default agent presence for EVERY effectful action, not just browser ones.
    Glide the real cursor to the action's screen target when it has one; otherwise nudge the
    attribution so the blue halo + 'Agent' label stay visible during shell/file work. Best-effort:
    never raises, never blocks the action. Suppressed only by presentation_mode='fast'."""
    if str(presentation_mode or "hybrid") == "fast":
        return
    try:
        if path in _PRESENT_GLIDE_ROUTES:
            data = payload.get("dataJson")
            data = json.loads(data) if isinstance(data, str) and data else (data if isinstance(data, dict) else {})
            target = (data.get("screenTarget") or (data.get("result") or {}).get("screenTarget")
                      or {}) if isinstance(data, dict) else {}
            if target.get("x") is not None:
                _glide_to(float(target["x"]), float(target["y"]))
                return
        # No target (or a keepalive route): re-assert agent ownership so the overlay halo stays lit.
        # A 1px cursor wiggle is enough to register motion through the agent_raw path and refresh
        # the attribution decay, showing "the agent is working here" without a fake destination.
        if path in _PRESENT_KEEPALIVE_ROUTES or path in _PRESENT_GLIDE_ROUTES:
            cur = _current_cursor()
            if cur:
                _raw_input([{"t": "m", "x": int(cur[0]) + 1, "y": int(cur[1])},
                            {"t": "m", "x": int(cur[0]), "y": int(cur[1])}])
    except Exception:
        pass


def _input_lifecycle(path):
    request = urllib.request.Request(
        "http://127.0.0.1:6906" + path, data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read(65536))


def _input_freeze_barrier():
    return _input_lifecycle("/lifecycle/pre-freeze")


def _json_message(message):
    return MessageToDict(message, preserving_proto_field_name=False,
                         always_print_fields_with_no_presence=True)


def _rpc(method, request, timeout=10):
    with open(GRPC_KEY_FILE, encoding="utf-8") as handle:
        metadata = (("authorization", "Bearer " + handle.read().strip()),)
    with grpc.insecure_channel(GRPC_TARGET, options=[
            ("grpc.max_send_message_length", MAX_BODY),
            ("grpc.max_receive_message_length", MAX_RESPONSE)]) as channel:
        stub = desktop_pb2_grpc.DesktopAgentStub(channel)
        return getattr(stub, method)(request, timeout=timeout, metadata=metadata)


class Handler(BaseHTTPRequestHandler):
    server_version = "PairputerDesktopBridge/1"

    def _send(self, status, payload):
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_RESPONSE:
            status, data = 507, b'{"ok":false,"error":{"code":"response_too_large"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        return body

    def _authorized(self):
        """Authenticate the server-to-guest hop before parsing a request body.

        Lambda authenticates access to the MicroVM endpoint, but its reserved
        routing headers are stripped before the guest sees the request.  The
        Run hook therefore installs a separate per-MicroVM capability that is
        known only to the MCP control plane and this root-owned file.
        """
        supplied = self.headers.get("X-Pairputer-Bridge-Capability", "")
        if len(supplied) < 32 or len(supplied) > 256:
            return False
        try:
            with open(BRIDGE_KEY_FILE, encoding="ascii") as handle:
                expected = handle.read(257).strip()
        except OSError:
            return False
        return len(expected) >= 32 and len(expected) <= 256 and hmac.compare_digest(
            supplied, expected
        )

    def do_GET(self):
        if not self._authorized():
            self._send(403, {"ok": False, "error": {"code": "forbidden"}})
            return
        try:
            if self.path == "/health":
                caps = _rpc("GetCapabilities", desktop_pb2.GetCapabilitiesRequest(), 2)
                self._send(200, {"ok": True, "protocolVersion": caps.protocol_version,
                                 "brain": True, "humanEpoch": CONTROL.snapshot()["humanEpoch"]})
                return
            if self.path == "/screen":
                self._send(200, _screen())
                return
            self._send(404, {"ok": False, "error": {"code": "unknown_route"}})
        except grpc.RpcError as exc:
            self._send(503, {"ok": False, "error": {"code": "desktopd_unavailable", "detail": exc.code().name}})

    def do_POST(self):
        if not self._authorized():
            self._send(403, {"ok": False, "error": {"code": "forbidden"}})
            return
        try:
            if self.headers.get("Origin") is not None:
                self._send(403, {"ok": False, "error": "browser-origin requests are forbidden"})
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._send(415, {"ok": False, "error": "application/json is required"})
                return
            body = self._body()
            path = self.path.split("?", 1)[0]
            if path == "/input":
                required = {"events", "target_proof", "expected_human_epoch",
                            "expected_world_revision"}
                unknown = set(body) - required
                if unknown:
                    raise ValueError("unknown input fields: " + ", ".join(sorted(unknown)))
                missing = required - set(body)
                if missing:
                    raise ValueError("missing input fields: " + ", ".join(sorted(missing)))
                self._send(200, _agent_input(
                    body["events"], body["target_proof"], body["expected_human_epoch"],
                    body["expected_world_revision"],
                ))
                return
            if path == "/computer/action":
                # Open CUA surface: stock computer-use actions, no proof/epoch ceremony.
                self._send(200, _computer_action(body))
                return
            if path == "/brain/drive":
                self._send(200, BRAIN.call(brain_api.submit_task(body), timeout=15))
                return
            if path == "/brain/continue":
                self._send(200, BRAIN.call(brain_api.continue_task(body), timeout=15))
                return
            if path == "/brain/status":
                task_id = str(body.pop("task_id", ""))
                if not task_id or body:
                    raise ValueError("task_status requires only task_id")
                self._send(200, BRAIN.call(_brain_status(task_id), timeout=5))
                return
            if path == "/brain/cancel":
                task_id = str(body.pop("task_id", ""))
                if not task_id or body:
                    raise ValueError("cancel_task requires only task_id")
                self._send(200, BRAIN.call(brain_api.cancel_task(task_id), timeout=5))
                return
            if path == "/brain/approve":
                approval_id = str(body.pop("approval_id", ""))
                if not approval_id or body:
                    raise ValueError("approve_action requires only approval_id")
                self._send(200, BRAIN.call(_brain_approve(approval_id), timeout=5))
                return
            if path == "/lifecycle/pre-freeze":
                if body:
                    raise ValueError("pre-freeze accepts no fields")
                input_barrier = _input_freeze_barrier()
                BRAIN.call(_brain_sync_epoch(int(input_barrier["humanEpoch"]), "freeze_barrier"), timeout=2)
                brain_barrier = BRAIN.call(brain_api.before_freeze(), timeout=10)
                self._send(200, {"ok": True, "input": input_barrier, "brain": brain_barrier})
                return
            if path == "/lifecycle/post-thaw":
                if body:
                    raise ValueError("post-thaw accepts no fields")
                brain_result = BRAIN.call(brain_api.after_thaw(), timeout=10)
                state = _input_lifecycle("/lifecycle/post-thaw")
                BRAIN.call(_brain_sync_epoch(state["humanEpoch"], "thaw_control_sync"), timeout=2)
                self._send(200, {"ok": True, "control": state, "brain": brain_result})
                return
            if path == "/network/airgap":
                # Express the desired air-gap state; a root reconciler enforces it
                # on iptables within ~1s. The bridge is unprivileged and only
                # writes intent — it never touches the firewall itself.
                self._send(200, _set_airgap(body))
                return
            if path == "/capabilities":
                if body:
                    raise ValueError("capabilities accepts no fields")
                result = _rpc("GetCapabilities", desktop_pb2.GetCapabilitiesRequest(), 3)
                self._send(200, _json_message(result))
                return
            elif path == "/apps/list":
                if body:
                    raise ValueError("list_apps accepts no fields")
                caps = _rpc("GetCapabilities", desktop_pb2.GetCapabilitiesRequest(), 3)
                self._send(200, {"ok": True, "apps": list(caps.installed_apps),
                                 "browserCdpReady": bool(caps.browser_cdp_ready),
                                 "accessibilityReady": bool(caps.accessibility_ready)})
                return
            elif path == "/observe":
                request = ParseDict(body, desktop_pb2.ObserveRequest(), ignore_unknown_fields=False)
                result = _rpc("Observe", request, 5)
                payload = _json_message(result)
                # Surface network posture so the widget/agent can read air-gap
                # state from the same snapshot instead of a separate call.
                if isinstance(payload, dict):
                    payload["network"] = _airgap_snapshot()
                self._send(200, payload)
                return
            elif path == "/accessibility/ground":
                # Prune4Web-style grounding: get the a11y nodes via Observe (the accessibility_tree
                # Execute path wraps its result through _action_pb, which does not surface the nodes
                # list — verified live; Observe carries them in accessibilityJson), then rank them
                # against the intent HERE and return a short candidate list.
                from element_grounding import rank_elements
                intent = str(body.get("intent", "")).strip()
                if not intent:
                    raise ValueError("ground_target requires a non-empty intent")
                top_k = int(body.get("top_k", 12))
                if not 1 <= top_k <= 50:
                    raise ValueError("top_k must be in [1, 50]")
                obs = _rpc("Observe", ParseDict({"include_accessibility": True},
                                                desktop_pb2.ObserveRequest(), ignore_unknown_fields=False), 8)
                acc_raw = getattr(obs, "accessibility_json", "") or "{}"
                try:
                    acc = json.loads(acc_raw)
                except (ValueError, TypeError):
                    acc = {}
                nodes = acc.get("nodes", []) if isinstance(acc, dict) else []
                # optional app/role/name pre-filter (Observe returns the whole desktop)
                app = str(body.get("app_name", "")).strip().casefold()
                role = str(body.get("role", "")).strip().casefold()
                name = str(body.get("name", "")).strip().casefold()
                if app or role or name:
                    nodes = [n for n in nodes if isinstance(n, dict)
                             and (not app or app in str(n.get("appIdentity", "")).casefold())
                             and (not role or role == str(n.get("role", "")).casefold())
                             and (not name or name in str(n.get("name", "")).casefold())]
                ranked = rank_elements(intent, nodes, top_k=top_k)
                ranked["source"] = "atspi"
                ranked["treeTruncated"] = bool(acc.get("truncated")) if isinstance(acc, dict) else False
                ranked["ok"] = True
                self._send(200, ranked)
                return
            elif path in ROUTES:
                if path == "/browser/query":
                    body = strict_browser_query_request(body)
                kind = ROUTES[path]
                # FRICTIONLESS WRITE: a host that just wants to "drop code into the sandbox" should not
                # have to observe -> extract epoch/revision -> write. For the write-family routes, when
                # the caller OMITS the anti-drift envelope fields we fill the CURRENT values (an
                # unconditional write) and synthesize the id fields. A caller that DOES pass epoch/
                # revision still gets the exact-consent anti-drift check — the strict path stays opt-in.
                if path in _AUTOFILL_ENVELOPE_ROUTES:
                    _autofill_write_envelope(body)
                envelope = {key: body.pop(key) for key in list(body) if key in ENVELOPE_FIELDS}
                request = ParseDict({"envelope": envelope, "action": {kind: body}},
                                    desktop_pb2.ExecuteRequest(), ignore_unknown_fields=False)
                # /browser/open may LAUNCH the browser on demand (it doesn't run at boot) — a cold
                # Chromium start + CDP wait doesn't fit the default 15s. 22s stays under the ~25s
                # host tool-call cap (codex-mcp-tool-timeout-25s).
                result = _rpc("Execute", request, 22 if path == "/browser/open" else 15)
                payload = _json_message(result)
                payload["ok"] = bool(getattr(result, "accepted", True))
                # Visible-by-default agent presence for EVERY effectful action: glide the real cursor
                # to the action's screen target when it has one (browser/UI/app/window), else keep the
                # halo lit during shell/file work. computer_action already injects real XTEST motion.
                if payload.get("ok") and path != "/computer/action":
                    _present_action(path, payload, envelope.get("presentation_mode"))
                self._send(200, payload)
                return
            else:
                self._send(404, {"ok": False, "error": {"code": "unknown_route"}})
                return
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"ok": False, "error": {"code": "invalid_request", "detail": str(exc)[:500]}})
        except grpc.RpcError as exc:
            code = 503 if exc.code() == grpc.StatusCode.UNAVAILABLE else 400
            self._send(code, {"ok": False, "error": {"code": exc.code().name.lower(),
                                                       "detail": exc.details()[:500] if exc.details() else ""}})
        except _BRAIN_CLIENT_ERRORS as exc:
            # KNOWABLE brain-domain rejections (unknown task_id, an already-active task, a freeze
            # barrier) are the CALLER's to act on — return a clean 409 with a machine code, not an
            # opaque 500. Gate on the known names so a genuine RuntimeError bug still 500s.
            if not _is_brain_client_error(exc):
                self._send(500, {"ok": False, "error": {"code": "bridge_failure", "detail": str(exc)[:500]}})
                return
            self._send(409, {"ok": False, "error": {"code": _brain_error_code(exc), "detail": str(exc)[:500]}})
        except Exception as exc:
            self._send(500, {"ok": False, "error": {"code": "bridge_failure", "detail": str(exc)[:500]}})

    def log_message(self, fmt, *args):
        print("[agent_bridge] " + (fmt % args), flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
