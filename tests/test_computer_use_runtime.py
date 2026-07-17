from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import http.server
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from types import SimpleNamespace
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CAPSULE = ROOT / "capsules" / "computer-use-desktop"
RUNTIME = CAPSULE / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(RUNTIME))

from observers.browser_cdp import CdpClient
from services.control_state import ControlState, ControlStateCorrupt, LeaseRejected
from services.browser_service import BrowserService, TaskDomainGrantStore, strict_browser_query_request
from services.app_service import AppService
from services.process_service import ProcessService
from services.screenshot_service import ScreenshotService
from services.workspace_service import WorkspaceError, WorkspaceService
from egress_proxy import EgressPolicy, EgressProxyServer, ProxyPolicyDenied


def envelope(control, action="a", idem="i"):
    state = control.snapshot()
    return {"action_id": action, "idempotency_key": idem,
            "expected_human_epoch": state["humanEpoch"],
            "expected_world_revision": state["worldRevision"]}


class _FakeAppProcess:
    def __init__(self, pid=901, returncode=None):
        self.pid, self.returncode = pid, returncode
        self.terminated = False

    def poll(self): return self.returncode
    def terminate(self): self.terminated = True; self.returncode = -15
    def wait(self, timeout=None): return self.returncode
    def kill(self): self.terminated = True; self.returncode = -9


class _FakeAppWindows:
    def __init__(self, visible=True): self.visible = visible
    def list_windows(self):
        windows = ([{"windowId": "0x1", "appIdentity": "chromium-browser.Chromium-browser",
                     "title": "Workbench"}] if self.visible else [])
        return windows, []


def test_app_service_launches_browser_as_unprivileged_user_and_waits_for_verified_state(tmp_path):
    control, calls = ControlState(tmp_path / "control"), []
    process = _FakeAppProcess()

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    service = AppService(
        control, _FakeAppWindows(), launcher=launch,
        browser_probe=lambda: True, pid_probe=lambda: 4242,
        sleep=lambda _: None, launch_timeout=1,
    )
    result = service.open({**envelope(control), "app_id": "browser"})
    assert result["accepted"] is True
    assert result["data"] == {"appId": "browser", "pid": 4242, "ready": True,
                              "cdpReady": True, "topLevelWindow": True}
    argv, kwargs = calls[0]
    assert argv[:6] == ["runuser", "-u", "app", "--", "env", "-i"]
    assert "HOME=/home/app" in argv and "USER=app" in argv
    assert "XAUTHORITY=/run/pairputer/xauthority" in argv
    assert "XDG_RUNTIME_DIR=/run/user/1000" in argv and "DISPLAY=:1" in argv
    assert kwargs["env"] == {"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert result["endingWorldRevision"] == 1


def test_app_service_never_reports_dead_browser_launcher_as_success(tmp_path):
    control = ControlState(tmp_path / "control")
    process = _FakeAppProcess(returncode=70)
    service = AppService(
        control, _FakeAppWindows(visible=False), launcher=lambda *_a, **_k: process,
        browser_probe=lambda: False, pid_probe=lambda: None,
        sleep=lambda _: None, launch_timeout=0,
    )
    result = service.open({**envelope(control), "app_id": "browser"})
    assert result["accepted"] is False and result["reason"] == "effect_failed"
    assert result["summary"] == "browser failed verified startup"
    assert "browser" not in service.processes
    # The launch attempt itself crossed the shared mutation boundary and is
    # therefore represented in the world revision even though readiness failed.
    assert control.snapshot()["worldRevision"] == 1


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    state_dir = tmp_path / "state"
    control = ControlState(tmp_path / "control")
    service = WorkspaceService(workspace, control, state_dir=state_dir, max_file_bytes=1024 * 1024)
    yield workspace, control, service
    service.close()


def test_atomic_write_requires_hash_and_returns_machine_evidence(runtime):
    root, control, service = runtime
    result = service.write({**envelope(control), "path": "hello.txt", "content": "hello", "encoding": "utf-8"})
    assert result["accepted"] is True
    assert result["endingWorldRevision"] == 1
    assert result["evidence"][0] == {
        "kind": "file_hash", "observedAt": result["evidence"][0]["observedAt"], "path": "hello.txt",
        "beforeSha256": "", "afterSha256": hashlib.sha256(b"hello").hexdigest(),
        "size": 5, "mimeType": "text/plain"}
    with pytest.raises(WorkspaceError, match="expected_sha256 is required"):
        service.write({**envelope(control, "b", "j"), "path": "hello.txt", "content": "changed"})
    current = hashlib.sha256(b"hello").hexdigest()
    changed = service.write({**envelope(control, "c", "k"), "path": "hello.txt", "content": "changed",
                             "expected_sha256": current})
    assert changed["evidence"][0]["beforeSha256"] == current
    assert (root / "hello.txt").read_text() == "changed"


def test_workspace_mkdir_nested_replay_and_desktop_ownership(runtime):
    root, control, service = runtime
    request = {
        **envelope(control, "mkdir", "mkdir-key"),
        "path": "e2e/site/assets", "parents": True,
    }
    created = service.mkdir(request)
    assert created["accepted"] and created["data"] == {
        "path": "e2e/site/assets", "created": True,
        "createdDepth": 3, "mode": "0770",
    }
    revision = control.snapshot()["worldRevision"]
    assert service.mkdir(request) == created
    assert control.snapshot()["worldRevision"] == revision
    for path in (root / "e2e", root / "e2e/site", root / "e2e/site/assets"):
        assert path.is_dir()
        assert path.stat().st_uid == root.stat().st_uid
        assert path.stat().st_gid == root.stat().st_gid
        assert path.stat().st_mode & 0o777 == 0o770
        assert os.access(path, os.R_OK | os.W_OK | os.X_OK)


def test_workspace_mkdir_rejects_symlink_file_collision_and_missing_parent(runtime, tmp_path):
    root, control, service = runtime
    outside = tmp_path / "outside"; outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "file").write_text("collision")
    with pytest.raises(WorkspaceError, match="symlink or file collision"):
        service.mkdir({**envelope(control, "symlink", "symlink"),
                       "path": "escape/subdir", "parents": True})
    with pytest.raises(WorkspaceError, match="symlink or file collision"):
        service.mkdir({**envelope(control, "file", "file"),
                       "path": "file/subdir", "parents": True})
    with pytest.raises(WorkspaceError, match="parent directory does not exist"):
        service.mkdir({**envelope(control, "parents", "parents"),
                       "path": "missing/leaf", "parents": False})
    assert not (outside / "subdir").exists()


def test_workspace_mkdir_stale_epoch_and_revision_do_not_create(runtime):
    root, control, service = runtime
    stale_epoch = {**envelope(control, "epoch", "epoch"), "path": "blocked/epoch"}
    control.human_takeover()
    rejected = service.mkdir(stale_epoch)
    assert rejected["accepted"] is False and rejected["reason"] == "human_epoch_changed"
    stale_revision = {**envelope(control, "revision", "revision"), "path": "blocked/revision"}
    control.note_observed_change()
    rejected = service.mkdir(stale_revision)
    assert rejected["accepted"] is False and rejected["reason"] == "world_revision_changed"
    assert not (root / "blocked").exists()


def test_stale_epoch_and_revision_reject_before_commit(runtime):
    root, control, service = runtime
    stale = envelope(control)
    control.human_takeover()
    result = service.write({**stale, "path": "blocked.txt", "content": "no"})
    assert result["accepted"] is False and result["reason"] == "human_epoch_changed"
    assert not (root / "blocked.txt").exists()
    stale_revision = envelope(control, "b", "j")
    control.note_observed_change()
    result = service.write({**stale_revision, "path": "also-blocked.txt", "content": "no"})
    assert result["accepted"] is False and result["reason"] == "world_revision_changed"


def test_symlink_traversal_mount_style_and_reserved_paths_fail_closed(runtime, tmp_path):
    root, control, service = runtime
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "secret").write_text("secret")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symlink"):
        service.read("escape/secret")
    for path in ("../outside/secret", "/etc/passwd", ".pairputer-trash/x", "a/../../b"):
        with pytest.raises(WorkspaceError): service.read(path)
    assert (outside / "secret").read_text() == "secret"


def test_hardlinks_and_corrupt_control_state_fail_closed(runtime, tmp_path):
    root, control, service = runtime
    outside = tmp_path / "outside-file"
    outside.write_text("outside-secret")
    os.link(outside, root / "hardlink")
    with pytest.raises(WorkspaceError, match="single-link"):
        service.read("hardlink")
    control.path.write_text("not-json")
    with pytest.raises(ControlStateCorrupt):
        control.snapshot()


def test_patch_move_trash_and_idempotent_replay(runtime):
    root, control, service = runtime
    first = service.write({**envelope(control, idem="create"), "path": "a.txt", "content": "alpha beta"})
    digest = first["evidence"][0]["afterSha256"]
    request = {**envelope(control, "patch", "patch-key"), "path": "a.txt", "expected_sha256": digest,
               "hunks": [{"old": "beta", "new": "gamma", "count": 1}]}
    patched = service.patch(request); revision = control.snapshot()["worldRevision"]
    assert service.patch(request) == patched
    assert control.snapshot()["worldRevision"] == revision
    digest = patched["evidence"][0]["afterSha256"]
    moved = service.move({**envelope(control, "move", "move-key"), "source": "a.txt",
                          "destination": "b.txt", "expected_sha256": digest})
    assert moved["accepted"] and (root / "b.txt").exists()
    trashed = service.trash({**envelope(control, "trash", "trash-key"), "path": "b.txt",
                             "expected_sha256": digest})
    assert trashed["evidence"][0]["reversible"] is True and not (root / "b.txt").exists()


def test_idempotency_key_is_bound_to_the_complete_request(runtime):
    _, control, service = runtime
    request = {**envelope(control, "create", "bound-key"), "path": "one.txt", "content": "one"}
    service.write(request)
    with pytest.raises(ValueError, match="different request"):
        service.write({**request, "path": "two.txt", "content": "two"})


def _upload_request(control, *, upload_id, path, payload, offset, chunk, final,
                    action, idem, expected_sha256=None):
    request = {
        **envelope(control, action, idem), "upload_id": upload_id, "path": path,
        "offset": offset, "chunk_base64": base64.b64encode(chunk).decode("ascii"),
        "chunk_sha256": hashlib.sha256(chunk).hexdigest(), "total_size": len(payload),
        "total_sha256": hashlib.sha256(payload).hexdigest(), "final": final,
    }
    if expected_sha256 is not None:
        request["expected_sha256"] = expected_sha256
    return request


def test_chunked_binary_upload_is_replay_safe_atomic_and_human_readable(runtime):
    root, control, service = runtime
    payload = bytes(range(256)) * 8
    first, second = payload[:777], payload[777:]
    request = _upload_request(
        control, upload_id="binary-1", path="artifact.bin", payload=payload,
        offset=0, chunk=first, final=False, action="chunk-1", idem="upload-1",
    )
    # A uint64 zero is omitted by protobuf JSON conversion on the private gRPC
    # path. The first chunk must still be interpreted as offset zero.
    request.pop("offset")
    receipt = service.upload(request)
    assert receipt["startingWorldRevision"] == receipt["endingWorldRevision"] == 0
    assert service.upload(request) == receipt
    assert control.snapshot()["worldRevision"] == 0
    assert not (root / "artifact.bin").exists()
    assert root not in service.upload_dir.parents and service.upload_dir != root

    completed = service.upload(_upload_request(
        control, upload_id="binary-1", path="artifact.bin", payload=payload,
        offset=len(first), chunk=second, final=True, action="chunk-2", idem="upload-2",
    ))
    assert completed["accepted"] and completed["endingWorldRevision"] == 1
    assert (root / "artifact.bin").read_bytes() == payload
    info = (root / "artifact.bin").stat()
    assert info.st_mode & 0o777 == 0o660
    assert (info.st_uid, info.st_gid) == (root.stat().st_uid, root.stat().st_gid)


def test_chunked_upload_rejects_out_of_order_hash_epoch_symlink_and_unbound_replace(runtime, tmp_path):
    root, control, service = runtime
    payload = b"abcdef"
    with pytest.raises(WorkspaceError, match="offset zero"):
        service.upload(_upload_request(
            control, upload_id="ordered", path="ordered.bin", payload=payload,
            offset=3, chunk=payload[3:], final=True, action="ordered", idem="ordered",
        ))
    bad_hash = _upload_request(
        control, upload_id="hash", path="hash.bin", payload=payload,
        offset=0, chunk=payload, final=True, action="hash", idem="hash",
    )
    bad_hash["chunk_sha256"] = "0" * 64
    with pytest.raises(WorkspaceError, match="chunk SHA-256"):
        service.upload(bad_hash)

    first = _upload_request(
        control, upload_id="epoch", path="epoch.bin", payload=payload,
        offset=0, chunk=payload[:3], final=False, action="epoch-1", idem="epoch-1",
    )
    service.upload(first)
    stale_final = _upload_request(
        control, upload_id="epoch", path="epoch.bin", payload=payload,
        offset=3, chunk=payload[3:], final=True, action="epoch-2", idem="epoch-2",
    )
    control.human_takeover()
    with pytest.raises(WorkspaceError, match="human_epoch_changed"):
        service.upload(stale_final)

    outside = tmp_path / "outside-upload"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises((WorkspaceError, OSError)):
        service.upload(_upload_request(
            control, upload_id="symlink", path="escape/file.bin", payload=payload,
            offset=0, chunk=payload, final=True, action="symlink", idem="symlink",
        ))
    assert not (outside / "file.bin").exists()

    existing = service.write({**envelope(control, "existing", "existing"),
                              "path": "existing.bin", "content": "old"})
    current_revision = control.snapshot()["worldRevision"]
    replace = _upload_request(
        control, upload_id="replace", path="existing.bin", payload=payload,
        offset=0, chunk=payload, final=True, action="replace", idem="replace",
    )
    replace["expected_world_revision"] = current_revision
    with pytest.raises(WorkspaceError, match="expected_sha256 is required"):
        service.upload(replace)
    assert (root / "existing.bin").read_bytes() == b"old"
    assert existing["accepted"]


def test_abandoned_upload_staging_is_ttl_cleaned(runtime):
    _, control, service = runtime
    payload = b"abandoned-upload"
    service.upload(_upload_request(
        control, upload_id="abandoned", path="later.bin", payload=payload,
        offset=0, chunk=payload[:5], final=False, action="abandon", idem="abandon",
    ))
    metadata = next(service.upload_dir.glob("*.json"))
    value = json.loads(metadata.read_text())
    value["updatedAt"] = 0
    metadata.write_text(json.dumps(value))
    service._cleanup_uploads()
    assert list(service.upload_dir.iterdir()) == []


def test_control_commit_serializes_human_epoch_advance(tmp_path):
    control = ControlState(tmp_path)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    def commit():
        with control.commit(0, 0):
            entered.set(); release.wait(1)
    def human():
        control.human_takeover(); completed.set()
    a = threading.Thread(target=commit); b = threading.Thread(target=human)
    a.start(); entered.wait(1); b.start(); time.sleep(0.05)
    assert not completed.is_set()
    release.set(); a.join(1); b.join(1)
    assert control.snapshot()["humanEpoch"] == 1
    assert control.snapshot()["worldRevision"] == 2


class FakeInjector:
    def __init__(self):
        self.events = []; self.position = (0, 0)
        self.focused_window = {"window_id": 17, "x": 10, "y": 10,
                               "width": 800, "height": 600}
    def inject(self, event):
        self.events.append(dict(event))
        if event.get("t") == "m": self.position = (event["x"], event["y"])
    def cursor(self): return self.position
    def display_size(self): return (1440, 900)
    def region_digest(self, _x, _y, _width, _height): return "a" * 64
    def focused_window_proof(self):
        return dict(self.focused_window) if self.focused_window else None
    def display_idle_ms(self): return getattr(self, "idle_ms", 9999)


def test_display_human_detector_attributes_owner_by_input_source(tmp_path):
    # A human can drive the desktop through a path the arbiter never sees (raw VNC / console). The
    # X11-idle detector must flip owner -> human for that, keep owner -> agent for the agent's own
    # injection, and decay to idle when nobody acts — so the overlay never mislabels who is driving.
    spec = importlib.util.spec_from_file_location("desktop_input_ws_detect", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    control, injector = ControlState(tmp_path), FakeInjector()
    arbiter = module.InputArbiter(injector, control)

    # 1a. A ONE-SHOT unattributed idle drop is NOT a human: X clients (Chromium among them) call
    # XResetScreenSaver, and a single blip used to mislabel a purely agent-driven desktop as
    # "human" and drop the agent's next events (found live on AWS, 2026-07-11).
    arbiter._last_idle_ms = 5000
    arbiter.detect_display_human_activity(idle_ms=50)
    assert control.snapshot()["owner"] == "idle", "a single idle blip must not read as human"

    # 1b. HUMAN drives directly (agent did NOT inject): a real hand keeps resetting the counter,
    # so a SECOND consecutive edge poll confirms the takeover.
    arbiter._last_idle_ms = 5000
    arbiter.detect_display_human_activity(idle_ms=40)
    assert control.snapshot()["owner"] == "human"

    # 2. AGENT injected just now: the same sustained edges are attributed to the agent, NOT a human.
    control.set_owner("agent")
    arbiter.note_agent_injected()
    for _ in range(3):
        arbiter._last_idle_ms = 5000
        arbiter.detect_display_human_activity(idle_ms=50)
    assert control.snapshot()["owner"] == "agent", "agent's own input must not read as human"

    # 3. Steady-low idle (no NEW edge) after an old agent move must not keep re-flipping to human.
    arbiter._last_idle_ms = 50
    arbiter.detect_display_human_activity(idle_ms=60)  # idle growing, no drop
    assert control.snapshot()["owner"] == "agent"

    # 4. Long idle with nobody acting -> decay to idle.
    arbiter._last_agent_inject = 0.0
    arbiter._last_human = 0.0
    arbiter._last_idle_ms = 5000
    arbiter.detect_display_human_activity(idle_ms=6000)
    assert control.snapshot()["owner"] == "idle"


def target_proof(**updates):
    return {"x": 0, "y": 0, "width": 100, "height": 100,
            "pixel_sha256": "a" * 64,
            "focused_window": {"window_id": 17, "x": 10, "y": 10,
                               "width": 800, "height": 600}, **updates}


class FakeCdpSessionMixin:
    def session(self, websocket):
        parent = self

        class Session:
            def __enter__(self):
                return self
            def __exit__(self, _exc_type, _exc, _traceback):
                return None
            def command(self, method, params=None):
                return parent.command(websocket, method, params or {})

        return Session()


def test_input_receipts_epoch_gate_and_held_cleanup(tmp_path):
    spec = importlib.util.spec_from_file_location("desktop_input_ws", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    control, injector = ControlState(tmp_path), FakeInjector()
    arbiter = module.InputArbiter(injector, control)
    receipt = arbiter.submit("agent", [{"t": "k", "key": "Control", "down": True}],
                             expected_epoch=0, display_revision=0,
                             target_proof=target_proof())
    assert receipt["accepted"] and receipt["acceptedEvents"] == 1
    human = arbiter.submit("human", [{"t": "m", "x": 20, "y": 30}])
    assert human["humanEpoch"] == 1 and human["releasedHeldInputs"] == 1
    assert {"t": "k", "key": "Control", "down": False} in injector.events
    rejected = arbiter.submit("agent", [{"t": "m", "x": 1, "y": 1}], expected_epoch=0,
                              display_revision=control.snapshot()["worldRevision"],
                              target_proof=target_proof())
    assert not rejected["accepted"] and rejected["reason"] == "human_epoch_changed"


def test_agent_raw_input_needs_no_proof_but_still_yields_to_human(tmp_path):
    # The open CUA path (actor="agent_raw"): a stock computer-use loop sends coordinate
    # clicks with NO target_proof / epoch, and they inject — but the human-first cooldown
    # must still block the agent right after any human input.
    spec = importlib.util.spec_from_file_location("desktop_input_ws_raw", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    control, injector = ControlState(tmp_path), FakeInjector()
    arbiter = module.InputArbiter(injector, control)

    # 1. raw agent input works with no proof/epoch at all
    ok = arbiter.submit("agent_raw", [{"t": "m", "x": 300, "y": 400},
                                      {"t": "b", "button": 0, "down": True},
                                      {"t": "b", "button": 0, "down": False}])
    assert ok["accepted"] and ok["acceptedEvents"] == 3
    assert injector.position == (300, 400)

    # 2. a human event preempts and 3. the very next raw agent batch is dropped by cooldown
    arbiter.submit("human", [{"t": "m", "x": 10, "y": 10}])
    blocked = arbiter.submit("agent_raw", [{"t": "b", "button": 0, "down": True},
                                           {"t": "b", "button": 0, "down": False}])
    assert not blocked["accepted"] and blocked["reason"] == "human_active"


def test_agent_physical_input_rejects_changed_or_out_of_target_pixels(tmp_path):
    spec = importlib.util.spec_from_file_location("desktop_input_ws_proof", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    control, injector = ControlState(tmp_path), FakeInjector()
    arbiter = module.InputArbiter(injector, control)
    changed = arbiter.submit(
        "agent", [{"t": "m", "x": 20, "y": 20}], expected_epoch=0, display_revision=0,
        target_proof=target_proof(pixel_sha256="b" * 64),
    )
    assert not changed["accepted"] and changed["reason"] == "target_changed"
    with pytest.raises(ValueError, match="outside its approved target"):
        arbiter.submit(
            "agent", [{"t": "m", "x": 120, "y": 120}], expected_epoch=0,
            display_revision=0,
            target_proof=target_proof(),
        )
    with pytest.raises(ValueError, match="exact target proof"):
        arbiter.submit("agent", [{"t": "k", "key": "a", "down": True}],
                       expected_epoch=0, display_revision=0)


def test_agent_keyboard_input_revalidates_exact_focused_window_for_every_event(tmp_path):
    spec = importlib.util.spec_from_file_location("desktop_input_ws_focus", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    control, injector = ControlState(tmp_path), FakeInjector()
    original_inject = injector.inject

    def change_focus_after_first_key(event):
        original_inject(event)
        if event.get("t") == "k" and event.get("down"):
            injector.focused_window = {**injector.focused_window, "window_id": 18}

    injector.inject = change_focus_after_first_key
    receipt = module.InputArbiter(injector, control).submit(
        "agent",
        [{"t": "k", "key": "a", "down": True},
         {"t": "k", "key": "a", "down": False},
         {"t": "k", "key": "b", "down": True}],
        expected_epoch=0, display_revision=0, target_proof=target_proof(),
    )
    assert receipt["accepted"] is False
    assert receipt["acceptedEvents"] == 1
    assert receipt["reason"] == "focused_window_changed"
    assert receipt["releasedHeldInputs"] == 1


def test_agent_keyboard_input_requires_non_null_focused_window_proof(tmp_path):
    spec = importlib.util.spec_from_file_location("desktop_input_ws_no_focus", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    control, injector = ControlState(tmp_path), FakeInjector()
    receipt = module.InputArbiter(injector, control).submit(
        "agent", [{"t": "k", "key": "a", "down": True}],
        expected_epoch=0, display_revision=0,
        target_proof=target_proof(focused_window=None),
    )
    assert receipt["accepted"] is False
    assert receipt["reason"] == "focused_window_changed"


def test_keyboard_focus_proof_uses_server_input_focus_when_ewmh_is_absent():
    screen = (ROOT / "capsules/computer-use-desktop/rootfs/opt/capsule/observers/screen.py").read_text()
    input_ws = (ROOT / "capsules/computer-use-desktop/rootfs/opt/capsule/input_ws.py").read_text()
    for source in (screen, input_ws):
        assert 'get_input_focus().focus' in source
        assert "focused_id in (0, X.PointerRoot)" in source
        assert "window.query_pointer().child" in source
        assert 'getattr(translated, "x", getattr(translated, "dst_x", None))' in source
        assert source.index('get_input_focus().focus') < source.index('create_resource_object("window", window_id)')

    windows = (ROOT / "capsules/computer-use-desktop/rootfs/opt/capsule/observers/windows_x11.py").read_text()
    assert "window.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)" in windows


def test_screenshot_focus_proof_accepts_python_xlib_translate_xy(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "desktop_screen_focus", RUNTIME / "observers" / "screen.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

    fake_x = SimpleNamespace(AnyPropertyType=0, PointerRoot=1)
    monkeypatch.setitem(sys.modules, "Xlib", SimpleNamespace(X=fake_x))

    window = SimpleNamespace(
        get_geometry=lambda: SimpleNamespace(width=640, height=480),
        translate_coords=lambda *_: SimpleNamespace(x=23, y=41),
    )
    root = SimpleNamespace(
        id=1,
        get_full_property=lambda *_: SimpleNamespace(value=[17]),
    )
    connection = SimpleNamespace(
        screen=lambda: SimpleNamespace(root=root),
        intern_atom=lambda *_args, **_kwargs: 5,
        get_input_focus=lambda: SimpleNamespace(focus=SimpleNamespace(id=17)),
        create_resource_object=lambda kind, ident: window,
    )

    assert module.ScreenObserver._focused_window(connection) == {
        "window_id": 17, "x": 23, "y": 41, "width": 640, "height": 480,
    }


def test_xtest_text_keys_resolve_shifted_ascii_and_punctuation(monkeypatch):
    spec = importlib.util.spec_from_file_location("desktop_input_ws_keys", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

    symbols = {"A": 65, "apostrophe": 39, "exclam": 33, "Shift_L": 0xFFE1, "Return": 0xFF0D}

    class FakeXK:
        @staticmethod
        def string_to_keysym(name):
            return symbols.get(name, ord(name) if len(name) == 1 else 0)

    class FakeDisplay:
        def keysym_to_keycode(self, symbol):
            return {65: 10, 39: 11, 33: 12, 0xFFE1: 50, 0xFF0D: 36}.get(symbol, 0)
        def get_keyboard_mapping(self, keycode, _count):
            return {10: [[97, 65]], 11: [[39, 34]], 12: [[49, 33]], 36: [[0xFF0D]]}[keycode]

    monkeypatch.setitem(sys.modules, "Xlib", type("FakeXlib", (), {"XK": FakeXK}))
    injector = object.__new__(module.XTestInjector)
    injector.display = FakeDisplay()

    assert injector._resolve_key("A") == (10, True)
    assert injector._resolve_key("'") == (11, False)
    assert injector._resolve_key("!") == (12, True)
    # bare X keysym names (what the CUA adapter emits) now resolve — this was the Enter bug
    assert injector._resolve_key("Return") == (36, False)
    with pytest.raises(ValueError, match="unsupported key"):
        injector._resolve_key("🙂")


def test_xtest_synthetic_shift_is_reference_counted_and_respects_explicit_shift(monkeypatch):
    spec = importlib.util.spec_from_file_location("desktop_input_ws_shift", RUNTIME / "input_ws.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

    symbols = {"A": 65, "B": 66, "Shift_L": 0xFFE1}
    emitted = []

    class FakeXK:
        @staticmethod
        def string_to_keysym(name):
            return symbols.get(name, ord(name) if len(name) == 1 else 0)

    class FakeDisplay:
        def keysym_to_keycode(self, symbol):
            return {65: 10, 66: 11, 0xFFE1: 50}.get(symbol, 0)
        def get_keyboard_mapping(self, keycode, _count):
            return {10: [[97, 65]], 11: [[98, 66]], 50: [[0xFFE1, 0]]}[keycode]
        def sync(self):
            pass

    class FakeX:
        KeyPress, KeyRelease = 2, 3

    class FakeXTest:
        @staticmethod
        def fake_input(_display, event_type, keycode, **_kwargs):
            emitted.append((event_type, keycode))

    fake_xlib = type("FakeXlib", (), {"XK": FakeXK, "X": FakeX})
    fake_ext = type("FakeExt", (), {"xtest": FakeXTest})
    monkeypatch.setitem(sys.modules, "Xlib", fake_xlib)
    monkeypatch.setitem(sys.modules, "Xlib.ext", fake_ext)

    injector = object.__new__(module.XTestInjector)
    injector.display = FakeDisplay()
    injector._explicit_shift_down = False
    injector._synthetic_shift_keys = set()
    injector._physical_shift_down = False

    injector.inject({"t": "k", "key": "A", "down": True})
    injector.inject({"t": "k", "key": "B", "down": True})
    injector.inject({"t": "k", "key": "A", "down": False})
    assert emitted == [(2, 50), (2, 10), (2, 11), (3, 10)]
    injector.inject({"t": "k", "key": "Shift", "down": True})
    injector.inject({"t": "k", "key": "B", "down": False})
    assert emitted[-1] == (3, 11)
    assert (3, 50) not in emitted
    injector.inject({"t": "k", "key": "Shift", "down": False})
    assert emitted[-1] == (3, 50)


def test_browser_query_is_policy_scoped_read_only_and_immune_to_main_world_monkeypatches(tmp_path):
    class FakeCdp(FakeCdpSessionMixin):
        def __init__(self):
            self.calls = []
            self.main_world_query_calls = 0
            self.dom_mutations = 0

        def tabs(self):
            return [{"id": "tab-1", "url": "https://retailer.example/product?access_token=secret#otp",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"}]

        def command(self, _websocket, method, params):
            self.calls.append((method, params))
            # Models a hostile main world where document.querySelector/getters mutate the DOM. The old
            # Runtime.evaluate implementation trips this branch; the CDP DOM/AX path must never do so.
            if method.startswith("Runtime."):
                self.main_world_query_calls += 1
                self.dom_mutations += 1
                if "type:" in params.get("expression", ""):
                    return {"result": {"value": {"tag": "body", "type": ""}}}
                return {"result": {"value": {"text": "hostile mutation", "tag": "BODY"}}}
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": 2}
            if method == "DOM.describeNode":
                return {"node": {"nodeId": 2, "backendNodeId": 22,
                                  "nodeName": "BODY", "attributes": []}}
            if method == "Accessibility.getFullAXTree":
                return {"nodes": [
                    {"nodeId": "root", "backendDOMNodeId": 22,
                     "childIds": ["safe", "control", "ignored"], "ignored": False,
                     "role": {"value": "RootWebArea"},
                     "name": {"value": "White Air Jordan results"}},
                    {"nodeId": "safe", "ignored": False, "role": {"value": "StaticText"},
                     "name": {"value": "Size 12 in stock"}},
                    # Live AX values are attacker-/human-controlled and must never reach the model.
                    {"nodeId": "control", "childIds": ["shadow"], "ignored": False,
                     "role": {"value": "textbox"},
                     "name": {"value": "Promo code"}, "value": {"value": "otp-live-secret"}},
                    {"nodeId": "shadow", "parentId": "control", "ignored": False,
                     "role": {"value": "StaticText"}, "name": {"value": "otp-live-secret"}},
                    {"nodeId": "ignored", "ignored": True, "role": {"value": "StaticText"},
                     "name": {"value": "hidden access token"}},
                ]}
            raise AssertionError(f"unexpected CDP method: {method}")

    control = ControlState(tmp_path)
    cdp = FakeCdp()
    grants = TaskDomainGrantStore(["retailer.example"], resolver=lambda _host: ("93.184.216.34",))
    service = BrowserService(control, cdp, task_grants=grants)
    service.task_grants.register("shopping", ["retailer.example"])
    result = service.action({"browser_action": "query", "task_id": "shopping",
                             "tab_id": "tab-1", "selector": "body"})

    assert result["accepted"] is True
    assert result["actuator"] == "browser.cdp.read_only"
    assert "Size 12 in stock" in result["data"]["result"]["text"]
    assert "otp-live-secret" not in result["data"]["result"]["text"]
    assert "hidden access token" not in result["data"]["result"]["text"]
    assert result["data"]["url"] == "https://retailer.example/product"
    assert result["data"]["provenance"] == {
        "source": "web_page", "trust": "untrusted", "url": "https://retailer.example/product"}
    assert result["warnings"] == ["untrusted_web_content"]
    assert cdp.main_world_query_calls == cdp.dom_mutations == 0
    assert all(not method.startswith("Runtime.") for method, _ in cdp.calls)
    assert result["startingWorldRevision"] == result["endingWorldRevision"] == 0
    assert control.snapshot()["worldRevision"] == 0


def test_browser_query_rejects_domains_outside_task_policy(tmp_path):
    class FakeCdp(FakeCdpSessionMixin):
        def tabs(self):
            return [{"id": "tab-1", "url": "https://retailer.example/product",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"}]

    grants = TaskDomainGrantStore(["allowed.example"], resolver=lambda _host: ("93.184.216.34",))
    service = BrowserService(ControlState(tmp_path), FakeCdp(), task_grants=grants)
    service.task_grants.register("scoped", ["allowed.example"])
    with pytest.raises(ValueError, match="outside the active task grant"):
        service.action({"browser_action": "query", "task_id": "scoped",
                        "tab_id": "tab-1", "selector": "body"})


def test_task_domain_grant_cannot_exceed_deployment_ceiling_or_survive_revoke():
    grants = TaskDomainGrantStore(["approved.example"], resolver=lambda _host: ("93.184.216.34",))
    with pytest.raises(ValueError, match="deployment ceiling"):
        grants.register("task", ["attacker.example"])
    grants.register("task", ["shop.approved.example"])
    assert grants.authorize_url("task", "https://shop.approved.example/item")
    grants.revoke("task")
    with pytest.raises(ValueError, match="active task domain grant"):
        grants.authorize_url("task", "https://shop.approved.example/item")


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.8/",
    "http://192.168.1.2/", "http://[fd00::1]/",
])
def test_browser_policy_rejects_metadata_private_and_link_local_literals(target):
    host = urllib.parse.urlparse(target).hostname
    grants = TaskDomainGrantStore("*", resolver=lambda _host: ())
    grants.register("task", [host])
    with pytest.raises(ValueError, match="non-global network"):
        grants.authorize_url("task", target)


def test_browser_allows_confined_workspace_file_and_blocks_escape(tmp_path, monkeypatch):
    # A host that authored a page in the sandbox can open it via file:///home/app/workspace/... — but
    # ONLY inside the confined workspace subtree. Arbitrary file:// and ../ escapes stay blocked.
    ws = tmp_path / "workspace"
    (ws / "sub").mkdir(parents=True)
    (ws / "page.html").write_text("<h1>hi</h1>")
    (ws / "sub" / "app.js").write_text("//")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setenv("PAIRPUTER_WORKSPACE", str(ws))
    monkeypatch.setenv("PAIRPUTER_WORKBENCH_AUTONOMY", "true")  # workbench runs autonomy-on
    grants = TaskDomainGrantStore("*", resolver=lambda _h: ())
    grants._WORKSPACE_ROOT = os.path.realpath(str(ws))
    # allowed: files inside the workspace
    assert grants.authorize_url("t", f"file://{ws}/page.html").endswith("/page.html")
    assert grants.authorize_url("t", f"file://{ws}/sub/app.js").endswith("/app.js")
    # blocked: traversal escape, a sibling outside the workspace, and a bare arbitrary path
    for bad in (f"file://{ws}/../secret.txt", f"file://{tmp_path}/secret.txt", "file:///etc/passwd"):
        with pytest.raises(ValueError, match="confined to the workspace"):
            grants.authorize_url("t", bad)
    # blocked: a remote host in a file:// URL
    with pytest.raises(ValueError, match="may not name a remote host"):
        grants.authorize_url("t", "file://evil.example/home/app/workspace/x")
    # still blocked: every other non-http(s) scheme
    with pytest.raises(ValueError, match="credential-free HTTP"):
        grants.authorize_url("t", "chrome://settings")


def test_browser_policy_rejects_private_or_mixed_dns_and_disallowed_ports():
    answers = {
        "private.example": ("10.0.0.2",),
        "mixed.example": ("93.184.216.34", "127.0.0.1"),
        "public.example": ("93.184.216.34",),
    }
    grants = TaskDomainGrantStore("*", resolver=lambda host: answers[host])
    grants.register("task", answers)
    for host in ("private.example", "mixed.example"):
        with pytest.raises(ValueError, match="non-global network"):
            grants.authorize_url("task", f"https://{host}/")
    with pytest.raises(ValueError, match="remote port"):
        grants.authorize_url("task", "https://public.example:8443/")
    assert grants.authorize_url("task", "https://public.example/")


def _addr(ip, port):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def test_egress_policy_resolves_once_pins_numeric_and_rejects_ssrf_variants():
    calls = []
    answers = {
        "public.example": [_addr("93.184.216.34", 443), _addr("2606:2800:220:1:248:1893:25c8:1946", 443)],
        "private.example": [_addr("10.0.0.8", 443)],
        "mixed.example": [_addr("93.184.216.34", 443), _addr("127.0.0.1", 443)],
        "rebinding.example": [_addr("127.0.0.1", 4173)],
    }

    def resolver(host, port, *_args):
        calls.append((host, port))
        return answers[host]

    policy = EgressPolicy(resolver=resolver, allow_local_preview=True,
                          preview_ports=[4173], public_ports=[80, 443])
    target = policy.resolve("public.example", 443)
    assert target.ip == "93.184.216.34"
    assert target.sockaddr == ("93.184.216.34", 443)
    assert calls == [("public.example", 443)]
    for host in ("private.example", "mixed.example"):
        with pytest.raises(ProxyPolicyDenied, match="non-global"):
            policy.resolve(host, 443)
    with pytest.raises(ProxyPolicyDenied, match="preview grant"):
        policy.resolve("rebinding.example", 4173)
    for host, port in (("169.254.169.254", 80), ("10.0.0.8", 80),
                       ("127.0.0.1", 6905), ("127.0.0.1", 6907),
                       ("127.0.0.1", 9000), ("127.0.0.1", 9222)):
        with pytest.raises(ProxyPolicyDenied):
            policy.resolve(host, port)
    with pytest.raises(ProxyPolicyDenied, match="public target port"):
        policy.resolve("public.example", 8443)


def _proxy_exchange(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
        client.sendall(request)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            # The proxy may reject and close the connection (e.g. 431 on an oversized
            # header) before we half-close our write side; sendall already flushed the
            # request, so an ENOTCONN/EPIPE here is benign — just read the response.
            pass
        response = bytearray()
        while True:
            chunk = client.recv(65536)
            if not chunk:
                return bytes(response)
            response.extend(chunk)


def test_egress_proxy_allows_only_explicit_local_preview_and_bounds_headers():
    class Preview(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"preview-through-pinned-proxy"
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *_args): pass

    preview = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Preview)
    preview_thread = threading.Thread(target=preview.serve_forever, daemon=True); preview_thread.start()
    preview_port = preview.server_address[1]
    token = "a" * 43
    policy = EgressPolicy(
        allow_local_preview=True, preview_ports=[preview_port], public_ports=[80, 443],
        preview_grant_loader=lambda supplied: {
            "token": supplied, "port": preview_port, "expires_at": int(time.time()) + 60,
        } if supplied == token else {},
    )
    proxy = EgressProxyServer(("127.0.0.1", 0), policy=policy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True); proxy_thread.start()
    proxy_port = proxy.server_address[1]
    try:
        response = _proxy_exchange(proxy_port, (
            f"GET http://p-{token}.pairputer-preview.invalid:{preview_port}/asset HTTP/1.1\r\n"
            f"Host: attacker.invalid\r\n"
            f"Connection: close\r\n\r\n"
        ).encode())
        assert response.startswith(b"HTTP/1.0 200")
        assert b"preview-through-pinned-proxy" in response

        redirected = _proxy_exchange(proxy_port, (
            f"GET http://localhost:{preview_port}/mutate HTTP/1.1\r\n"
            f"Host: localhost:{preview_port}\r\n\r\n"
        ).encode())
        assert redirected.startswith(b"HTTP/1.1 403")

        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:6905/private-control",
            "http://127.0.0.1:6907/health",
            "http://127.0.0.1:9222/json",
        ):
            denied = _proxy_exchange(proxy_port, (
                f"GET {url} HTTP/1.1\r\nHost: ignored.invalid\r\n\r\n"
            ).encode())
            assert denied.startswith(b"HTTP/1.1 403")

        oversized = _proxy_exchange(proxy_port, b"GET http://localhost/ HTTP/1.1\r\nX: "
                                    + b"a" * (33 * 1024) + b"\r\n\r\n")
        assert oversized.startswith(b"HTTP/1.1 431")
    finally:
        proxy.shutdown(); proxy.server_close()
        preview.shutdown(); preview.server_close()

    unresolved = TaskDomainGrantStore("*", resolver=lambda _host: ())
    unresolved.register("task", ["missing.example"])
    with pytest.raises(ValueError, match="no addresses"):
        unresolved.authorize_url("task", "https://missing.example/")


def test_egress_proxy_tunnels_websocket_only_to_loopback_preview_grant():
    # A WebSocket upgrade must reach a broker-authorized loopback preview (code-server / HMR) but be
    # refused for every real egress destination — a page must never tunnel a framed protocol off-box.
    ws_upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ws_upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ws_upstream.bind(("127.0.0.1", 0)); ws_upstream.listen(4)
    ws_port = ws_upstream.getsockname()[1]

    def serve_ws():
        # Minimal server: accept the handshake, reply 101, echo one frame back, then close.
        while True:
            try:
                conn, _ = ws_upstream.accept()
            except OSError:
                return
            with conn:
                req = b""
                while b"\r\n\r\n" not in req:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    req += chunk
                if b"upgrade: websocket" not in req.lower():
                    conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                    continue
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                )
                frame = conn.recv(4096)
                if frame:
                    conn.sendall(b"ECHO:" + frame)

    ws_thread = threading.Thread(target=serve_ws, daemon=True); ws_thread.start()

    token = "b" * 43
    # Deterministic resolver: the preview host is validated grant-side (ip pinned to 127.0.0.1 by the
    # policy), and any real host resolves to a fixed PUBLIC address so case (2) never hits live DNS.
    def resolver(host, port, *_a, **_k):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]
    policy = EgressPolicy(
        resolver=resolver,
        allow_local_preview=True, preview_ports=[ws_port], public_ports=[80, 443],
        preview_grant_loader=lambda supplied: {
            "token": supplied, "port": ws_port, "expires_at": int(time.time()) + 60,
        } if supplied == token else {},
    )
    proxy = EgressProxyServer(("127.0.0.1", 0), policy=policy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True); proxy_thread.start()
    proxy_port = proxy.server_address[1]
    try:
        # (1) WS upgrade to the loopback preview grant is forwarded and the frame tunnels both ways.
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=3) as client:
            client.sendall((
                f"GET http://p-{token}.pairputer-preview.invalid:{ws_port}/ HTTP/1.1\r\n"
                f"Host: attacker.invalid\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            handshake = b""
            while b"\r\n\r\n" not in handshake:
                part = client.recv(4096)
                assert part, "proxy closed before completing the WS handshake"
                handshake += part
            assert handshake.startswith(b"HTTP/1.1 101"), handshake[:64]
            client.sendall(b"\x81\x03abc")  # a tiny (unmasked) frame; the fake upstream echoes it
            echoed = client.recv(4096)
            assert echoed.startswith(b"ECHO:"), echoed[:32]

        # (2) The SAME upgrade to a real (public) egress host is rejected — no off-box tunnel.
        denied = _proxy_exchange(proxy_port, (
            "GET http://example.com/ HTTP/1.1\r\n"
            "Host: example.com\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        assert denied.startswith(b"HTTP/1.1 400"), denied[:64]
    finally:
        proxy.shutdown(); proxy.server_close()
        ws_upstream.close()


def test_local_preview_requires_explicit_policy_and_never_exposes_control_ports():
    denied = TaskDomainGrantStore("*", allow_local_preview=False)
    denied.register("preview", ["127.0.0.1"])
    with pytest.raises(ValueError, match="preview is not enabled"):
        denied.authorize_url("preview", "http://127.0.0.1:4173/")

    default_ports = TaskDomainGrantStore("*", allow_local_preview=True)
    default_ports.register("preview", ["127.0.0.1"])
    with pytest.raises(ValueError, match="preview is not enabled"):
        default_ports.authorize_url("preview", "http://127.0.0.1:5901/")

    allowed = TaskDomainGrantStore(
        "*", allow_local_preview=True, preview_ports=[4173, 8000]
    )
    allowed.register("preview", ["localhost", "127.0.0.1"])
    assert allowed.authorize_url("preview", "http://localhost:4173/")
    for port in (5901, 50051, 6901, 6902, 6903, 6904, 6905, 6906, 9000, 9222):
        with pytest.raises(ValueError, match="preview is not enabled"):
            allowed.authorize_url("preview", f"http://127.0.0.1:{port}/")


@pytest.mark.parametrize("attributes", [
    ["type", "password"],
    ["type", "hidden"],
    ["name", "one_time_code"],
    ["id", "access-token"],
    ["autocomplete", "current-password"],
    ["hidden", ""],
])
def test_browser_query_refuses_sensitive_or_hidden_fields(tmp_path, attributes):
    class FakeCdp(FakeCdpSessionMixin):
        def tabs(self):
            return [{"id": "tab-1", "url": "https://bank.example/login",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"}]

        def command(self, _websocket, method, _params):
            if method == "DOM.getDocument":
                return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector":
                return {"nodeId": 2}
            if method == "DOM.describeNode":
                return {"node": {"nodeId": 2, "nodeName": "INPUT", "attributes": attributes}}
            raise AssertionError("sensitive nodes must be rejected before semantic extraction")

    grants = TaskDomainGrantStore(["bank.example"], resolver=lambda _host: ("93.184.216.34",))
    service = BrowserService(ControlState(tmp_path), FakeCdp(), task_grants=grants)
    service.task_grants.register("bank-task", ["bank.example"])
    with pytest.raises(ValueError, match="hidden|protected|sensitive|credential"):
        service.action({"browser_action": "query", "task_id": "bank-task",
                        "tab_id": "tab-1", "selector": "input"})


def test_browser_query_bridge_rejects_fastmcp_compatibility_extras():
    assert strict_browser_query_request({"task_id": "task", "tab_id": "t", "selector": "body"}) == {
        "task_id": "task", "tab_id": "t", "selector": "body", "browser_action": "query"}
    for extra in ({"browser_action": "click"}, {"value": "secret"}, {"action_id": "smuggled"},
                  {"allowed_domains": ["attacker.example"]}):
        with pytest.raises(ValueError, match="unknown browser query fields"):
            strict_browser_query_request({"task_id": "task", "tab_id": "t", "selector": "body", **extra})


def test_browser_query_taints_computed_names_derived_from_form_values(tmp_path):
    class FakeCdp(FakeCdpSessionMixin):
        def tabs(self):
            return [{"id": "tab", "url": "https://bank.example/otp",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/otp"}]

        def command(self, _websocket, method, _params):
            if method == "DOM.getDocument": return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector": return {"nodeId": 2}
            if method == "DOM.describeNode":
                return {"node": {"nodeId": 2, "backendNodeId": 84,
                                  "nodeName": "BUTTON", "attributes": []}}
            if method == "Accessibility.getFullAXTree":
                return {"nodes": [
                    {"nodeId": "root", "backendDOMNodeId": 22,
                     "childIds": ["input", "button", "safe"], "ignored": False,
                     "role": {"value": "RootWebArea"}, "name": {"value": "Verification"}},
                    {"nodeId": "input", "backendDOMNodeId": 42, "childIds": ["shadow"],
                     "ignored": False,
                     "role": {"value": "textbox"}, "name": {"value": "Code"},
                     "value": {"value": "OTP-742913"}},
                    {"nodeId": "shadow", "parentId": "input", "ignored": False,
                     "role": {"value": "StaticText"}, "name": {"value": "OTP-742913"}},
                    {"nodeId": "button", "backendDOMNodeId": 84, "ignored": False,
                     "role": {"value": "button"}, "name": {
                         "value": "Confirm one-time code OTP-742913",
                         "sources": [{"type": "relatedElement", "attribute": "aria-labelledby",
                                      "attributeValue": {"type": "idrefList", "value": "label",
                                          "relatedNodes": [{"backendDOMNodeId": 8,
                                                            "text": "One-time code OTP-742913"}]}}],
                     }},
                    {"nodeId": "safe", "ignored": False, "role": {"value": "StaticText"},
                     "name": {"value": "Verification instructions"}},
                ]}
            raise AssertionError(method)

    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("otp", ["bank.example"])
    result = BrowserService(ControlState(tmp_path), FakeCdp(), task_grants=grants).action({
        "browser_action": "query", "task_id": "otp", "tab_id": "tab", "selector": "#confirm",
    })
    observed = result["data"]["result"]["text"]
    assert "OTP-742913" not in observed and "Confirm one-time code" not in observed


def test_browser_click_and_fill_use_cdp_dom_input_without_main_world_javascript(tmp_path):
    class FakeCdp(FakeCdpSessionMixin):
        def __init__(self): self.calls = []
        def tabs(self):
            return [{"id": "tab", "url": "https://public.example/form?token=hidden#otp",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"}]
        def command(self, _websocket, method, params):
            self.calls.append((method, dict(params)))
            if method.startswith("Runtime."):
                raise AssertionError("semantic effects must not execute page JavaScript")
            if method == "DOM.getDocument": return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector": return {"nodeId": 2}
            if method == "DOM.describeNode":
                return {"node": {"nodeId": 2, "nodeName": "INPUT",
                                  "attributes": ["type", "text", "name", "search"]}}
            if method == "DOM.scrollIntoViewIfNeeded": return {}
            if method == "DOM.getBoxModel":
                return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
            if method in {"Input.dispatchMouseEvent", "DOM.focus",
                          "Input.dispatchKeyEvent", "Input.insertText"}:
                return {}
            raise AssertionError(f"unexpected CDP method {method}")

    control, cdp = ControlState(tmp_path), FakeCdp()
    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("task", ["public.example"])
    service = BrowserService(control, cdp, task_grants=grants)
    clicked = service.action({
        **envelope(control, "click", "click"), "task_id": "task", "tab_id": "tab",
        "browser_action": "click", "selector": "#search",
    })
    filled = service.action({
        **envelope(control, "fill", "fill"), "task_id": "task", "tab_id": "tab",
        "browser_action": "fill", "selector": "#search", "value": "white Jordan size 12",
    })
    assert clicked["data"]["result"]["clicked"] is True
    assert filled["data"]["result"] == {"filled": True, "insertedCharacters": 20}
    assert clicked["data"]["url"] == filled["data"]["url"] == "https://public.example/form"
    assert all(not method.startswith("Runtime.") for method, _ in cdp.calls)
    assert any(method == "Input.dispatchMouseEvent" for method, _ in cdp.calls)
    assert any(method == "Input.insertText" for method, _ in cdp.calls)
    assert clicked["endingWorldRevision"] == 1
    assert filled["startingWorldRevision"] == 1
    assert filled["endingWorldRevision"] == control.snapshot()["worldRevision"] == 2


def test_browser_focus_uses_dom_focus_and_never_dispatches_a_click(tmp_path):
    class FakeCdp(FakeCdpSessionMixin):
        def __init__(self): self.calls = []
        def tabs(self):
            return [{"id": "tab", "url": "https://public.example/product",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/1"}]
        def command(self, _websocket, method, params):
            self.calls.append((method, dict(params)))
            if method == "DOM.getDocument": return {"root": {"nodeId": 1}}
            if method == "DOM.querySelector": return {"nodeId": 2}
            if method == "DOM.describeNode":
                return {"node": {"nodeId": 2, "nodeName": "BUTTON", "attributes": []}}
            if method in {"DOM.scrollIntoViewIfNeeded", "DOM.focus"}: return {}
            raise AssertionError(f"focus must not call {method}")

    control, cdp = ControlState(tmp_path), FakeCdp()
    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("task", ["public.example"])
    result = BrowserService(control, cdp, task_grants=grants).action({
        **envelope(control, "focus", "focus"), "task_id": "task", "tab_id": "tab",
        "browser_action": "focus", "selector": "#purchase",
    })
    assert result["data"]["result"] == {"focused": True}
    methods = [method for method, _params in cdp.calls]
    assert "DOM.focus" in methods
    assert "Input.dispatchMouseEvent" not in methods


def test_browser_open_redacts_query_and_fragment_from_receipt(tmp_path):
    class FakeCdp:
        def new_tab(self, target): return {"id": "tab", "url": target}
        def tabs(self): return []

    control = ControlState(tmp_path)
    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("task", ["public.example"])
    result = BrowserService(control, FakeCdp(), task_grants=grants).open({
        **envelope(control), "task_id": "task",
        "url": "https://public.example/path?access_token=secret#otp",
    })
    encoded = json.dumps(result)
    assert result["data"]["url"] == "https://public.example/path"
    assert "access_token" not in encoded and "secret" not in encoded and "otp" not in encoded


def test_browser_open_recovers_exact_new_target_after_unknown_timeout_without_replay(tmp_path):
    target = "https://public.example/product"

    class FakeCdp:
        def __init__(self): self.created = False; self.calls = 0
        def tabs(self):
            old = {"id": "old", "type": "page", "url": target}
            new = {"id": "new", "type": "page", "url": target}
            return [old, new] if self.created else [old]
        def new_tab(self, requested):
            assert requested == target
            self.calls += 1
            self.created = True
            raise TimeoutError("receipt timed out")

    cdp, control = FakeCdp(), ControlState(tmp_path)
    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("task", ["public.example"])
    result = BrowserService(control, cdp, task_grants=grants).open({
        **envelope(control), "task_id": "task", "url": target,
    })
    assert result["accepted"] is True
    assert result["data"] == {
        "tabId": "new", "url": target, "recoveredUnknownOutcome": True,
    }
    assert cdp.calls == 1
    assert control.snapshot()["worldRevision"] == 1


@pytest.mark.parametrize("created", [
    [{"id": "redirected", "type": "page", "url": "https://public.example/other"}],
    [
        {"id": "new-a", "type": "page", "url": "https://public.example/product"},
        {"id": "new-b", "type": "page", "url": "https://public.example/product"},
    ],
])
def test_browser_open_unknown_timeout_fails_closed_on_redirect_or_ambiguity(tmp_path, created):
    target = "https://public.example/product"

    class FakeCdp:
        def __init__(self): self.did_create = False; self.calls = 0
        def tabs(self): return list(created) if self.did_create else []
        def new_tab(self, _requested):
            self.calls += 1
            self.did_create = True
            raise TimeoutError("receipt timed out")

    cdp, control = FakeCdp(), ControlState(tmp_path)
    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("task", ["public.example"])
    with pytest.raises(TimeoutError, match="receipt timed out"):
        BrowserService(control, cdp, task_grants=grants).open({
            **envelope(control), "task_id": "task", "url": target,
        })
    assert cdp.calls == 1
    assert control.snapshot()["worldRevision"] == 0


def test_browser_query_retries_only_bounded_transient_readiness_failures(tmp_path):
    class FakeCdp:
        def tabs(self):
            return [{"id": "tab", "type": "page", "url": "https://public.example/product",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/tab"}]

    class SettlingBrowser(BrowserService):
        _QUERY_RETRY_DELAYS = (0, 0)
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self.queries = 0
        def _query(self, _tab, _selector):
            self.queries += 1
            if self.queries < 3:
                raise ValueError("browser selector has no accessibility node")
            return {"text": "White Metallic $215", "tag": "MAIN"}

    control = ControlState(tmp_path)
    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("task", ["public.example"])
    service = SettlingBrowser(control, FakeCdp(), task_grants=grants)
    result = service.action({
        "browser_action": "query", "task_id": "task", "tab_id": "tab", "selector": "main",
    })
    assert result["data"]["result"]["text"] == "White Metallic $215"
    assert service.queries == 3
    assert control.snapshot()["worldRevision"] == 0


def test_browser_query_does_not_retry_protected_or_other_non_transient_failures(tmp_path):
    class FakeCdp:
        def tabs(self):
            return [{"id": "tab", "type": "page", "url": "https://public.example/account",
                     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/tab"}]

    class ProtectedBrowser(BrowserService):
        _QUERY_RETRY_DELAYS = (0, 0)
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self.queries = 0
        def _query(self, _tab, _selector):
            self.queries += 1
            raise ValueError("browser selector resolves to a protected input field")

    grants = TaskDomainGrantStore("*", resolver=lambda _host: ("93.184.216.34",))
    grants.register("task", ["public.example"])
    service = ProtectedBrowser(ControlState(tmp_path), FakeCdp(), task_grants=grants)
    with pytest.raises(ValueError, match="protected input"):
        service.action({
            "browser_action": "query", "task_id": "task", "tab_id": "tab",
            "selector": "#password",
        })
    assert service.queries == 1


def test_process_job_is_tracked_and_bounded(runtime):
    _, control, workspace = runtime
    processes = ProcessService(workspace, control)
    started = processes.start({**envelope(control), "argv": ["/bin/sh", "-c", "printf process-ok"],
                               "cwd": ".", "takeover_policy": "continue_background"})
    job = started["data"]["jobId"]
    deadline = time.time() + 3
    status = processes.status(job)
    while status["state"] == "running" and time.time() < deadline:
        time.sleep(0.02); status = processes.status(job)
    assert status["exitCode"] == 0 and "process-ok" in status["output"]


def test_process_output_is_redacted(runtime):
    _, control, workspace = runtime
    processes = ProcessService(workspace, control)
    started = processes.start({**envelope(control), "argv": ["/bin/sh", "-c",
        "printf 'Authorization: Bearer top-secret-token'"], "cwd": ".",
        "takeover_policy": "continue_background"})
    job = started["data"]["jobId"]
    deadline = time.time() + 3
    status = processes.status(job)
    while status["state"] == "running" and time.time() < deadline:
        time.sleep(0.02); status = processes.status(job)
    assert "top-secret-token" not in status["output"]
    assert "[REDACTED]" in status["output"]


def test_process_shell_ignores_profiles_and_rejects_home_override(runtime):
    root, control, workspace = runtime
    marker = root.parent / "profile-ran"
    (root.parent / ".bash_profile").write_text(f"touch {marker}\n")
    processes = ProcessService(workspace, control)
    with pytest.raises(ValueError, match="environment key not allowed"):
        processes.start({**envelope(control, "bad-env", "bad-env"), "argv": ["/bin/true"],
                         "environment": {"HOME": str(root)}, "cwd": "."})
    started = processes.start({**envelope(control, "shell", "shell"), "shell": "printf safe",
        "explicit_shell_mode": True, "cwd": ".", "takeover_policy": "continue_background"})
    job = started["data"]["jobId"]
    deadline = time.time() + 3
    while processes.status(job)["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert not marker.exists()


def test_process_job_environment_never_inherits_x11_authority(runtime, monkeypatch):
    _, control, workspace = runtime
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.setenv("XAUTHORITY", "/run/pairputer/xauthority")
    processes = ProcessService(workspace, control)
    environment = processes._environment(None)
    assert environment["DISPLAY"] == ""
    assert environment["XAUTHORITY"] == "/dev/null"
    with pytest.raises(ValueError, match="environment key not allowed"):
        processes._environment({"XAUTHORITY": "/run/pairputer/xauthority"})
    with pytest.raises(ValueError, match="environment key not allowed"):
        processes._environment({"DISPLAY": ":1"})


def test_coding_job_and_opt_in_localhost_preview_share_one_broker(runtime):
    root, control, workspace = runtime
    made = workspace.mkdir({
        **envelope(control, "mkdir-site", "mkdir-site"),
        "path": "site", "parents": True,
    })
    assert made["accepted"] and (root / "site").is_dir()
    written = workspace.write({
        **envelope(control, "write-site", "write-site"), "path": "site/index.html",
        "content": "<!doctype html><title>Workbench Preview</title><h1>preview-ok</h1>",
    })
    assert written["accepted"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    processes = ProcessService(workspace, control)
    started = processes.start({
        **envelope(control, "serve-site", "serve-site"),
        "argv": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        "cwd": "site", "takeover_policy": "stop_on_handoff",
    })
    job_id = started["data"]["jobId"]
    url = f"http://127.0.0.1:{port}/index.html"
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                if b"preview-ok" in response.read():
                    break
        except OSError:
            time.sleep(0.02)
    else:
        pytest.fail("tracked localhost preview did not become reachable")

    class PreviewCdp:
        def __init__(self): self.items = []
        def new_tab(self, target):
            with urllib.request.urlopen(target, timeout=1) as response:
                assert b"preview-ok" in response.read()
            item = {"id": "preview-tab", "url": target,
                    "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/preview"}
            self.items.append(item)
            return item
        def tabs(self): return list(self.items)

    grants = TaskDomainGrantStore(["localhost", "127.0.0.1"],
                                   allow_local_preview=True, preview_ports=[port])
    grants.register("task-preview", ["127.0.0.1"])
    class TestPreviewGrants:
        def issue(self, _task_id, target): return target
        def original_url(self, _task_id, _target): return None
        def revoke(self, _task_id): pass

    browser = BrowserService(control, PreviewCdp(), task_grants=grants,
                             preview_grants=TestPreviewGrants())
    opened = browser.open({**envelope(control, "preview", "preview"),
                           "task_id": "task-preview", "url": url})
    assert opened["accepted"] and opened["data"]["tabId"] == "preview-tab"
    assert control.snapshot()["worldRevision"] == 4
    canceled = processes.cancel({**envelope(control, "stop-preview", "stop-preview"),
                                 "job_id": job_id})
    assert canceled["accepted"]


def test_screenshot_is_discarded_when_human_epoch_changes(tmp_path):
    control = ControlState(tmp_path / "control")
    target = tmp_path / "screen.png"

    class Observer:
        def capture(self, *_):
            target.write_bytes(b"png")
            control.human_takeover()
            return {"path": str(target), "sha256": "a" * 64, "size": 3}
        def discard(self, result):
            Path(result["path"]).unlink(missing_ok=True)

    result = ScreenshotService(control, Observer()).capture({**envelope(control), "width": 1, "height": 1})
    assert result["accepted"] is False and result["reason"] == "human_epoch_changed"
    assert not target.exists()


def test_cdp_endpoint_must_resolve_only_to_loopback():
    with pytest.raises(ValueError, match="loopback"):
        CdpClient("http://8.8.8.8:9222")
    client = CdpClient("http://127.0.0.1:9222")
    assert client.endpoint == "http://127.0.0.1:9222"


def test_cdp_target_creation_uses_a_separate_cold_microvm_timeout(monkeypatch):
    seen = {}
    client = CdpClient("http://127.0.0.1:9222", timeout=2)
    monkeypatch.setattr(client, "_json", lambda path, method="GET", timeout=None:
                        seen.update(path=path, method=method, timeout=timeout) or {"id": "new"})
    assert client.new_tab("https://public.example/") == {"id": "new"}
    assert seen["method"] == "PUT" and seen["timeout"] == 10.0


def test_protocol_and_runtime_bindings_are_private_and_typed():
    proto = (CAPSULE / "proto/pairputer/desktop/v1/desktop.proto").read_text()
    desktopd = (RUNTIME / "desktopd.py").read_text()
    chromium = (CAPSULE / "rootfs/usr/local/bin/pairputer-chromium").read_text()
    assert "oneof action" in proto and "expected_human_epoch" in proto and "expected_world_revision" in proto
    assert 'address = "127.0.0.1:50051"' in desktopd
    assert "--remote-debugging-address=127.0.0.1" in chromium
    assert "--remote-allow-origins=http://127.0.0.1" in chromium
    assert "--remote-allow-origins=*" not in chromium
    assert "--no-sandbox" not in chromium
    assert "UNAUTHENTICATED" in desktopd and "PAIRPUTER_DESKTOP_AGENT_KEY_FILE" in desktopd
