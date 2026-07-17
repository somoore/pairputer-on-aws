#!/usr/bin/env python3.11
"""Loopback-only typed desktop service."""

from __future__ import annotations

import json
import hmac
import os
import time
from concurrent import futures
from datetime import datetime, timezone

import grpc
from google.protobuf.json_format import MessageToDict

from desktopgen.pairputer.desktop.v1 import desktop_pb2, desktop_pb2_grpc
from observers.atspi import AtspiObserver
from observers.browser_cdp import CdpClient
from observers.screen import ScreenObserver
from observers.windows_x11 import X11WindowObserver
from services.accessibility_service import AccessibilityService
from services.app_service import AppService
from services.artifact_service import ArtifactService
from services.browser_service import BrowserService, TaskDomainGrantStore
from services.common import MAX_RESULT_BYTES
from services.control_state import ControlState
from services.process_service import ProcessService
from services.screenshot_service import ScreenshotService
from services.workspace_service import WorkspaceService

SERVICE_VERSION = "0.1.0"
MAX_REQUEST_BYTES = int(os.environ.get("PAIRPUTER_GRPC_MAX_REQUEST_BYTES", "1048576"))
WORKSPACE = os.environ.get("PAIRPUTER_WORKSPACE", "/home/app/workspace")
STATE_DIR = os.environ.get("PAIRPUTER_STATE_DIR", "/home/app/.local/state/pairputer")
DISPLAY_WIDTH = int(os.environ.get("PAIRPUTER_DISPLAY_WIDTH", "1440"))
DISPLAY_HEIGHT = int(os.environ.get("PAIRPUTER_DISPLAY_HEIGHT", "900"))
AGENT_KEY_FILE = os.environ.get("PAIRPUTER_DESKTOP_AGENT_KEY_FILE", "/run/pairputer/desktop-agent.key")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class DesktopRuntime:
    def __init__(self):
        os.makedirs(WORKSPACE, mode=0o700, exist_ok=True)
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        self.control = ControlState(os.environ.get("PAIRPUTER_CONTROL_STATE_DIR", "/run/pairputer"))
        self.workspace = WorkspaceService(WORKSPACE, self.control, STATE_DIR)
        self.processes = ProcessService(self.workspace, self.control, os.path.join(STATE_DIR, "terminal.log"))
        self.windows = X11WindowObserver(os.environ.get("DISPLAY", ":1"))
        self.apps = AppService(self.control, self.windows)
        self.atspi_observer = AtspiObserver()
        self.accessibility = AccessibilityService(self.control, self.atspi_observer)
        self.cdp = CdpClient(os.environ.get("PAIRPUTER_CDP_ENDPOINT", "http://127.0.0.1:9222"))
        self.task_domains = TaskDomainGrantStore()
        self.browser = BrowserService(self.control, self.cdp, task_grants=self.task_domains)
        self.screen_observer = ScreenObserver(os.path.join(STATE_DIR, "evidence"),
            os.environ.get("DISPLAY", ":1") + (".0" if "." not in os.environ.get("DISPLAY", ":1") else ""),
            DISPLAY_WIDTH, DISPLAY_HEIGHT)
        self.screenshots = ScreenshotService(self.control, self.screen_observer)
        self.artifacts = ArtifactService(self.workspace, self.control)

    def display(self):
        state = self.control.snapshot()
        return desktop_pb2.DisplayMetadata(display_revision=state["worldRevision"],
            logical_width=DISPLAY_WIDTH, logical_height=DISPLAY_HEIGHT,
            encoded_width=DISPLAY_WIDTH, encoded_height=DISPLAY_HEIGHT,
            device_scale=1.0, rotation=0, observed_at=now_iso())


def _dict(message):
    return MessageToDict(message, preserving_proto_field_name=True)


def _request(envelope, action):
    value = _dict(action)
    value.update({
        "task_id": envelope.task_id,
        "step_id": envelope.step_id,
        "action_id": envelope.action_id,
        "expected_world_revision": envelope.expected_world_revision,
        "expected_human_epoch": envelope.expected_human_epoch,
        "idempotency_key": envelope.idempotency_key,
        "presentation_mode": envelope.presentation_mode or "hybrid",
        "deadline_unix_ms": envelope.deadline_unix_ms,
    })
    return value


def _action_pb(result):
    return desktop_pb2.ActionResult(
        accepted=bool(result.get("accepted", result.get("ok", False))),
        reason=str(result.get("reason", "")), action_id=str(result.get("actionId", "")),
        starting_world_revision=int(result.get("startingWorldRevision", result.get("worldRevision", 0))),
        ending_world_revision=int(result.get("endingWorldRevision", result.get("worldRevision", 0))),
        human_epoch=int(result.get("humanEpoch", 0)), actuator=str(result.get("actuator", "observer")),
        presentation_method=str(result.get("presentationMethod", "semantic")),
        summary=str(result.get("summary", "observation completed"))[:500],
        data_json=json.dumps(result.get("data", result), sort_keys=True, separators=(",", ":")),
        postconditions_json=json.dumps(result.get("postconditions", []), separators=(",", ":")),
        evidence_json=json.dumps(result.get("evidence", []), separators=(",", ":")),
        retry_safety=str(result.get("retrySafety", "safe")), warnings=list(result.get("warnings", [])))


class DesktopAgent(desktop_pb2_grpc.DesktopAgentServicer):
    def __init__(self, runtime=None):
        self.runtime = runtime or DesktopRuntime()

    @staticmethod
    def _authorize(context):
        try:
            expected = open(AGENT_KEY_FILE, encoding="utf-8").read().strip()
        except OSError:
            context.abort(grpc.StatusCode.UNAVAILABLE, "desktop agent capability is unavailable")
        supplied = dict(context.invocation_metadata()).get("authorization", "")
        if not expected or not hmac.compare_digest(supplied, f"Bearer {expected}"):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "desktop agent capability is required")

    def GetCapabilities(self, request, context):
        self._authorize(context)
        try:
            self.runtime.cdp.tabs()
            cdp_ready = True
        except Exception:
            cdp_ready = False
        return desktop_pb2.Capabilities(
            protocol_version="pairputer.desktop.v1", service_version=SERVICE_VERSION,
            display=self.runtime.display(),
            observers=["workspace", "process", "windows_x11", "browser_cdp", "atspi", "screen"],
            effectors=["workspace_atomic", "workspace_chunked_upload", "process_pty", "application",
                       "window_ewmh", "browser_cdp", "atspi", "artifact"],
            installed_apps=list(self.runtime.apps.allowed_apps),
            supported_actions=["workspace_list", "workspace_describe", "workspace_read", "workspace_write",
                "workspace_mkdir", "workspace_upload", "workspace_patch", "workspace_move", "workspace_trash", "process_start", "process_status",
                "process_cancel", "app_open", "window_list", "window_focus", "browser_open",
                "browser_observe", "browser_action", "accessibility_tree", "accessibility_action",
                "screenshot", "artifact_export", "task_domain_grant", "task_domain_revoke",
                "task_domain_clear"],
            presentation_modes=["fast", "visible", "hybrid"],
            risk_classes=["read_only", "local_reversible", "local_destructive", "external_commit", "unknown"],
            limits={"request_bytes": MAX_REQUEST_BYTES, "result_bytes": MAX_RESULT_BYTES,
                    "workspace_file_bytes": self.runtime.workspace.max_file_bytes,
                    "workspace_upload_chunk_bytes": self.runtime.workspace.MAX_UPLOAD_CHUNK,
                    "workspace_upload_total_bytes": self.runtime.workspace.max_file_bytes,
                    "terminal_tail_bytes": self.runtime.processes.MAX_TAIL,
                    "accessibility_nodes": self.runtime.atspi_observer.max_nodes},
            browser_cdp_ready=cdp_ready, accessibility_ready=self.runtime.atspi_observer.available())

    def Observe(self, request, context):
        self._authorize(context)
        state = self.runtime.control.snapshot()
        notices, windows, browser, accessibility, workspace = [], [], [], {}, {}
        if request.include_windows:
            windows, window_warnings = self.runtime.windows.list_windows(limit=request.limit or 100)
            notices.extend(window_warnings)
        if request.include_browser:
            try:
                browser = self.runtime.browser.observe({"task_id": request.task_id})["tabs"]
            except Exception:
                notices.append("browser_scope_required_or_not_observed")
        if request.include_accessibility:
            accessibility = self.runtime.atspi_observer.tree(request.accessibility_app)
            if accessibility.get("truncated"):
                notices.append("accessibility_truncated")
        if request.workspace_path:
            try:
                workspace = self.runtime.workspace.list(request.workspace_path, request.limit or 100)
            except Exception:
                notices.append("workspace_not_observed")
        with self.runtime.processes._lock:
            jobs = [{"jobId": key, "state": value["state"], "exitCode": value["exitCode"]}
                    for key, value in list(self.runtime.processes._jobs.items())[-32:]]
        return desktop_pb2.DesktopSnapshot(
            world_revision=state["worldRevision"], human_epoch=state["humanEpoch"],
            observed_at=now_iso(), display=self.runtime.display(), control_owner=state["owner"],
            active_window_json=json.dumps(windows[0] if windows else {}),
            windows_json=json.dumps(windows), accessibility_json=json.dumps(accessibility),
            browser_tabs_json=json.dumps(browser), workspace_json=json.dumps(workspace),
            running_jobs_json=json.dumps(jobs), truncation_notices=notices)

    def Execute(self, request, context):
        self._authorize(context)
        kind = request.action.WhichOneof("action")
        if not kind:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "typed action is required")
        action = getattr(request.action, kind)
        body = _request(request.envelope, action)
        try:
            if kind == "workspace_list":
                result = self.runtime.workspace.list(body.get("path", "."), body.get("limit", 200))
            elif kind == "workspace_describe": result = self.runtime.workspace.describe(body["path"])
            elif kind == "workspace_read": result = self.runtime.workspace.read(body["path"], body.get("offset", 0), body.get("length", 1048576))
            elif kind == "workspace_write": result = self.runtime.workspace.write(body)
            elif kind == "workspace_mkdir": result = self.runtime.workspace.mkdir(body)
            elif kind == "workspace_upload": result = self.runtime.workspace.upload(body)
            elif kind == "workspace_patch": result = self.runtime.workspace.patch(body)
            elif kind == "workspace_move": result = self.runtime.workspace.move(body)
            elif kind == "workspace_trash": result = self.runtime.workspace.trash(body)
            elif kind == "process_start": result = self.runtime.processes.start(body)
            elif kind == "process_status": result = self.runtime.processes.status(body["job_id"], body.get("tail_bytes", 65536))
            elif kind == "process_cancel": result = self.runtime.processes.cancel(body)
            elif kind == "app_open": result = self.runtime.apps.open(body)
            elif kind == "window_list": result = self.runtime.apps.list_windows()
            elif kind == "window_focus": result = self.runtime.apps.focus_window(body)
            elif kind == "browser_open": result = self.runtime.browser.open(body)
            elif kind == "browser_observe": result = self.runtime.browser.observe(body)
            elif kind == "browser_action": result = self.runtime.browser.action(body)
            elif kind == "accessibility_tree": result = self.runtime.accessibility.tree(body.get("app_name", ""), body.get("role", ""), body.get("name", ""))
            elif kind == "accessibility_action": result = self.runtime.accessibility.action(body)
            elif kind == "screenshot": result = self.runtime.screenshots.capture(body)
            elif kind == "artifact_export": result = self.runtime.artifacts.export(body)
            elif kind == "task_domain_grant":
                result = {**self.runtime.task_domains.register(
                    body.get("task_id", ""), body.get("allowed_domains", ())),
                    **self.runtime.control.snapshot()}
                print("[desktopd] task-domain grant registered", {
                    "task": str(body.get("task_id", ""))[:24],
                    "domains": len(body.get("allowed_domains", ())),
                }, flush=True)
            elif kind == "task_domain_revoke":
                result = {**self.runtime.task_domains.revoke(body.get("task_id", "")),
                          **self.runtime.control.snapshot()}
            elif kind == "task_domain_clear":
                result = {**self.runtime.task_domains.clear(), **self.runtime.control.snapshot()}
            else: context.abort(grpc.StatusCode.UNIMPLEMENTED, "unsupported action")
            if "accepted" not in result:
                result = {"accepted": True, "summary": f"{kind} observation completed", "data": result,
                          "humanEpoch": result.get("humanEpoch", 0), "worldRevision": result.get("worldRevision", 0)}
            return _action_pb(result)
        except (ValueError, KeyError) as exc:
            state = self.runtime.control.snapshot()
            return _action_pb({"accepted": False, "reason": "invalid_request", "summary": str(exc)[:500],
                               "humanEpoch": state["humanEpoch"], "worldRevision": state["worldRevision"],
                               "retrySafety": "safe"})
        except Exception as exc:
            state = self.runtime.control.snapshot()
            return _action_pb({"accepted": False, "reason": "effect_failed", "summary": str(exc)[:500],
                               "humanEpoch": state["humanEpoch"], "worldRevision": state["worldRevision"],
                               "retrySafety": "unknown_outcome"})

    def _brain_unavailable(self, context):
        self._authorize(context)
        context.abort(grpc.StatusCode.UNIMPLEMENTED, "task brain is not enabled in the deterministic service")

    def SubmitTask(self, request, context): return self._brain_unavailable(context)
    def ContinueTask(self, request, context): return self._brain_unavailable(context)
    def GetTask(self, request, context): return self._brain_unavailable(context)
    def CancelTask(self, request, context): return self._brain_unavailable(context)
    def WatchEvents(self, request, context): return self._brain_unavailable(context)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16), options=[
        ("grpc.max_receive_message_length", MAX_REQUEST_BYTES),
        ("grpc.max_send_message_length", MAX_RESULT_BYTES),
    ])
    desktop_pb2_grpc.add_DesktopAgentServicer_to_server(DesktopAgent(), server)
    address = "127.0.0.1:50051"
    if server.add_insecure_port(address) != 50051:
        raise RuntimeError("failed to bind loopback gRPC service")
    server.start()
    print(f"[desktopd] loopback gRPC ready at {address}", flush=True)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
