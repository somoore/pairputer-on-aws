"""Brain adapters for the authenticated, shared ``desktopd`` mutation broker.

The task brain runs as an unprivileged process.  It must not grow a second set
of filesystem, process, application, or browser effectors: all production
effects cross the same key-authenticated loopback gRPC boundary used by the
manifest tools, and therefore share one human epoch and world revision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import PurePosixPath
from typing import Any, Mapping

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict

from control_client import HumanPreempted, WorldChanged
from desktopgen.pairputer.desktop.v1 import desktop_pb2, desktop_pb2_grpc


class DesktopdRejected(RuntimeError):
    """A broker request failed without proving that a mutation committed."""


def _plain_message(message: Any) -> dict[str, Any]:
    value = MessageToDict(
        message, preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )
    for encoded, decoded in (
        ("data_json", "data"), ("evidence_json", "evidence"),
        ("postconditions_json", "postconditions"),
    ):
        raw = value.pop(encoded, "")
        try:
            value[decoded] = json.loads(raw) if raw else ([] if decoded != "data" else {})
        except json.JSONDecodeError as exc:
            raise DesktopdRejected(f"desktopd returned invalid {encoded}") from exc
    return value


class DesktopdClient:
    """Small authenticated client; the capability key never enters a request body."""

    def __init__(
        self,
        target: str = "127.0.0.1:50051",
        key_file: str | None = None,
        *,
        timeout: float = 15,
    ):
        self.target = target
        self.key_file = key_file or os.environ.get(
            "PAIRPUTER_DESKTOP_AGENT_KEY_FILE", "/run/pairputer/desktop-agent.key"
        )
        self.timeout = float(timeout)

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        try:
            key = open(self.key_file, encoding="utf-8").read().strip()
        except OSError as exc:
            raise DesktopdRejected("desktopd capability is unavailable") from exc
        if not key:
            raise DesktopdRejected("desktopd capability is empty")
        return (("authorization", "Bearer " + key),)

    def _call(self, method: str, request: Any, timeout: float | None = None) -> Any:
        with grpc.insecure_channel(
            self.target,
            options=(("grpc.max_send_message_length", 1024 * 1024),
                     ("grpc.max_receive_message_length", 2 * 1024 * 1024)),
        ) as channel:
            stub = desktop_pb2_grpc.DesktopAgentStub(channel)
            try:
                return getattr(stub, method)(
                    request, timeout=timeout or self.timeout, metadata=self._metadata()
                )
            except grpc.RpcError as exc:
                detail = (exc.details() or exc.code().name)[:500]
                raise DesktopdRejected(f"desktopd {method} failed: {detail}") from exc

    def observe(self) -> Mapping[str, Any]:
        response = self._call("Observe", desktop_pb2.ObserveRequest(
            include_windows=True, include_browser=True, workspace_path=".", limit=100,
        ), timeout=5)
        value = MessageToDict(response, preserving_proto_field_name=True)
        for field in (
            "active_window_json", "windows_json", "browser_tabs_json",
            "workspace_json", "running_jobs_json",
        ):
            raw = value.pop(field, "")
            try:
                value[field.removesuffix("_json")] = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise DesktopdRejected(f"desktopd returned invalid {field}") from exc
        return value

    def execute(
        self,
        kind: str,
        body: Mapping[str, Any],
        *,
        task_id: str = "",
        step_id: str = "",
        action_id: str = "",
        expected_human_epoch: int = 0,
        expected_world_revision: int = 0,
        idempotency_key: str = "",
        presentation_mode: str = "hybrid",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        envelope = {
            "task_id": task_id, "step_id": step_id, "action_id": action_id,
            "expected_human_epoch": int(expected_human_epoch),
            "expected_world_revision": int(expected_world_revision),
            "idempotency_key": idempotency_key,
            "presentation_mode": presentation_mode,
        }
        request = ParseDict(
            {"envelope": envelope, "action": {kind: dict(body)}},
            desktop_pb2.ExecuteRequest(), ignore_unknown_fields=False,
        )
        result = _plain_message(self._call("Execute", request, timeout=timeout))
        if not result.get("accepted"):
            reason = str(result.get("reason") or "desktopd_rejected")
            if reason == "human_epoch_changed":
                raise HumanPreempted(reason)
            if reason == "world_revision_changed":
                raise WorldChanged(reason)
            raise DesktopdRejected(f"{reason}: {str(result.get('summary') or '')[:300]}")
        return result


def _envelope(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    task_id = str(kwargs.get("task_id") or "")
    action_id = str(kwargs.get("action_id") or "")
    return {
        "task_id": task_id,
        "step_id": str(kwargs.get("step_id") or ""),
        "action_id": action_id,
        "expected_human_epoch": int(kwargs.get("expected_human_epoch", 0)),
        "expected_world_revision": int(kwargs.get("expected_world_revision", 0)),
        "idempotency_key": str(kwargs.get("idempotency_key") or hashlib.sha256(
            f"{task_id}:{action_id}".encode()
        ).hexdigest()),
        "presentation_mode": str(kwargs.get("presentation_mode") or "hybrid"),
    }


class DesktopdWorkspaceAdapter:
    def __init__(self, client: DesktopdClient):
        self.client = client

    async def read(self, path: str) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "workspace_read",
            {"path": path, "offset": 0, "length": 1024 * 1024}, timeout=10,
        )
        return dict(result["data"])

    async def describe(self, path: str) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "workspace_describe", {"path": path}, timeout=5,
        )
        return dict(result["data"])

    async def hash(self, path: str) -> str | None:
        try:
            return str((await self.describe(path)).get("sha256") or "") or None
        except DesktopdRejected:
            return None

    async def exists(self, path: str) -> bool:
        return await self.hash(path) is not None

    async def directory_exists(self, path: str) -> bool:
        try:
            return str((await self.describe(path)).get("type") or "") == "directory"
        except DesktopdRejected:
            return False

    async def mkdir(self, path: str, parents: bool = True,
                    **kwargs: Any) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "workspace_mkdir",
            {"path": path, "parents": bool(parents)},
            **_envelope(kwargs), timeout=15,
        )
        data = dict(result.get("data") or {})
        return {
            **data,
            "path": str(data.get("path") or path),
            "ending_world_revision": int(result.get("ending_world_revision", 0)),
            "human_epoch": int(result.get("human_epoch", 0)),
        }

    @staticmethod
    def _write_result(result: Mapping[str, Any], path: str, content: str,
                      before: str | None) -> dict[str, Any]:
        evidence = next((item for item in result.get("evidence", ())
                         if item.get("kind") == "file_hash"), {})
        return {
            "path": path, "before_sha256": before,
            "after_sha256": str(evidence.get("afterSha256") or hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()),
            "size": len(content.encode("utf-8")),
            "ending_world_revision": int(result.get("ending_world_revision", 0)),
            "human_epoch": int(result.get("human_epoch", 0)),
        }

    async def write(self, path: str, content: str, expected_sha256: str | None = None,
                    **kwargs: Any) -> Mapping[str, Any]:
        body: dict[str, Any] = {"path": path, "content": content, "encoding": "utf-8"}
        if expected_sha256 is not None:
            body["expected_sha256"] = expected_sha256
        result = await asyncio.to_thread(
            self.client.execute, "workspace_write", body, **_envelope(kwargs), timeout=15,
        )
        return self._write_result(result, path, content, expected_sha256)

    async def move(self, source: str, target: str, expected_sha256: str,
                   **kwargs: Any) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "workspace_move",
            {"source": source, "destination": target,
             "expected_sha256": expected_sha256},
            **_envelope(kwargs), timeout=15,
        )
        return {"source": source, "target": target, "sha256": expected_sha256,
                "ending_world_revision": int(result.get("ending_world_revision", 0))}

    async def trash(self, path: str, expected_sha256: str,
                    **kwargs: Any) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "workspace_trash",
            {"path": path, "expected_sha256": expected_sha256},
            **_envelope(kwargs), timeout=15,
        )
        evidence = next((item for item in result.get("evidence", ())
                         if item.get("kind") == "file_trashed"), {})
        return {"path": path, "trash_id": str(evidence.get("trashId") or ""),
                "sha256": expected_sha256,
                "ending_world_revision": int(result.get("ending_world_revision", 0))}


class DesktopdProcessAdapter:
    def __init__(self, client: DesktopdClient):
        self.client = client

    async def run(self, action: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        body: dict[str, Any] = {
            "cwd": str(action.get("cwd") or "."),
            "takeover_policy": str(action.get("handoff_policy") or "stop_on_handoff"),
            "environment": dict(action.get("env") or {}),
        }
        if action.get("shell"):
            body.update({"shell": str(action.get("command") or ""),
                         "explicit_shell_mode": True})
        else:
            body["argv"] = list(action.get("argv") or ())
        started = await asyncio.to_thread(
            self.client.execute, "process_start", body, **_envelope(kwargs), timeout=15,
        )
        job_id = str(started.get("data", {}).get("jobId") or "")
        if not job_id:
            raise DesktopdRejected("desktopd omitted the tracked job id")
        timeout = min(max(float(action.get("timeout_seconds", 60)), 0.1), 300.0)
        deadline = time.monotonic() + timeout
        while True:
            status_result = await asyncio.to_thread(
                self.client.execute, "process_status",
                {"job_id": job_id, "tail_bytes": 128 * 1024}, timeout=10,
            )
            status = dict(status_result["data"])
            if int(status.get("humanEpoch", kwargs.get("expected_human_epoch", 0))) != int(
                kwargs.get("expected_human_epoch", 0)
            ):
                raise HumanPreempted("human epoch changed while command was running")
            if status.get("state") != "running":
                return {
                    "job_id": job_id, "exit_code": status.get("exitCode"),
                    "stdout_tail": str(status.get("output") or ""), "stderr_tail": "",
                    "output_truncated": bool(status.get("outputTruncated")),
                    "state": status.get("state"),
                }
            if time.monotonic() >= deadline:
                # The same desktopd service owns both the job and the cancellation.
                current_revision = int(status.get("worldRevision", 0))
                await asyncio.to_thread(
                    self.client.execute, "process_cancel", {"job_id": job_id},
                    **{**_envelope(kwargs), "expected_world_revision": current_revision,
                       "idempotency_key": hashlib.sha256(
                           f"cancel:{kwargs.get('task_id')}:{job_id}".encode()
                       ).hexdigest()}, timeout=10,
                )
                raise TimeoutError("tracked command exceeded its bounded timeout")
            await asyncio.sleep(0.05)


class DesktopdBrowserAdapter:
    def __init__(self, client: DesktopdClient):
        self.client = client
        self._last_url = ""
        self._last_tab = ""

    async def navigate(self, *, url: str, **kwargs: Any) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "browser_open", {"url": url},
            **_envelope(kwargs), timeout=30,
        )
        self._last_url = str(result.get("data", {}).get("url") or url)
        self._last_tab = str(result.get("data", {}).get("tabId") or "")
        return dict(result.get("data") or {})

    async def state(self, *, task_id: str = "", **_: Any) -> Mapping[str, Any]:
        observed = await asyncio.to_thread(
            self.client.execute, "browser_observe", {}, task_id=task_id, timeout=10
        )
        tabs = list((observed.get("data") or {}).get("tabs") or ())
        tab = next((item for item in tabs if item.get("id") == self._last_tab), None)
        if tab is None and self._last_url:
            tab = next((item for item in tabs if item.get("url") == self._last_url), None)
        if tab is None and tabs:
            tab = tabs[-1]
        return {"url": str((tab or {}).get("url") or ""), "loaded": tab is not None,
                "tab_id": str((tab or {}).get("id") or "")}

    async def query(self, *, selector: str, task_id: str = "", **kwargs: Any) -> Mapping[str, Any]:
        state = await self.state(task_id=task_id)
        tab_id = str(state.get("tab_id") or self._last_tab)
        result = await asyncio.to_thread(
            self.client.execute, "browser_action",
            {"tab_id": tab_id, "browser_action": "query", "selector": selector},
            task_id=task_id, timeout=15,
        )
        value = dict(result.get("data") or {}).get("result")
        return {"matches": [] if value is None else [value], "tab_id": tab_id,
                "url": str((result.get("data") or {}).get("url") or state.get("url") or "")}

    async def interact(self, *, operation: str, selector: str, value: Any = None,
                       **kwargs: Any) -> Mapping[str, Any]:
        state = await self.state(task_id=str(kwargs.get("task_id") or ""))
        action = {"focus": "focus", "click": "click", "fill": "fill"}.get(operation)
        if action is None:
            raise DesktopdRejected(
                "desktopd semantic adapter supports focus/click/fill; external submit must be a visible click"
            )
        body = {"tab_id": str(state.get("tab_id") or self._last_tab),
                "browser_action": action, "selector": selector}
        if value is not None:
            body["value"] = str(value)
        result = await asyncio.to_thread(
            self.client.execute, "browser_action", body, **_envelope(kwargs), timeout=20,
        )
        return dict(result.get("data") or {})

    async def verify(self, *, selector: str, expected: Any = None,
                     task_id: str = "", **kwargs: Any) -> Mapping[str, Any]:
        observed = await self.query(selector=selector, task_id=task_id, **kwargs)
        matches = observed.get("matches") or []
        if expected is None:
            verified = bool(matches)
        else:
            verified = any(str(expected) in json.dumps(item, sort_keys=True) for item in matches)
        return {"verified": verified, **observed}


class DesktopdAppsAdapter:
    def __init__(self, client: DesktopdClient):
        self.client = client
        self._opened: dict[str, Mapping[str, Any]] = {}

    async def open(self, app: str, **kwargs: Any) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "app_open", {"app_id": app},
            **_envelope(kwargs), timeout=20,
        )
        self._opened[app] = dict(result.get("data") or {})
        return self._opened[app]

    async def state(self, app: str) -> Mapping[str, Any]:
        result = await asyncio.to_thread(self.client.execute, "window_list", {}, timeout=10)
        windows = list((result.get("data") or {}).get("windows") or ())
        # A process id is broker evidence; a visible top-level window is separately
        # observed.  Do not report success from the launch result alone.
        opened = self._opened.get(app) or {}
        identities = {
            "browser": ("chromium",), "editor": ("gnome-text-editor", "text editor"),
            "terminal": ("xterm", "workbench terminal"), "files": ("nautilus", "files"),
        }.get(app, (app.lower(),))
        top_level = any(
            any(marker in (str(item.get("appIdentity", "")) + " " +
                           str(item.get("title", ""))).lower() for marker in identities)
            for item in windows
        )
        return {"process_running": bool(opened.get("ready")) and bool(opened.get("pid")),
                "top_level_window": top_level, "windows": windows}


class DesktopdCrossAppAdapter:
    """Provenance-preserving transfer to a workspace artifact.

    Arbitrary application targets remain unsupported until desktopd exposes a
    typed, verifiable document insertion primitive; falling back to keystrokes
    here would lose provenance and exact-target guarantees.
    """

    def __init__(self, workspace: DesktopdWorkspaceAdapter):
        self.workspace = workspace
        self._records: dict[str, Mapping[str, Any]] = {}

    async def copy_fact(self, action: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        target = dict(action.get("target") or {})
        if target.get("type") != "workspace":
            raise DesktopdRejected("cross-app application targets require a typed desktopd adapter")
        path = str(target.get("path") or "")
        if not path or PurePosixPath(path).is_absolute():
            raise DesktopdRejected("cross-app workspace target must be relative")
        record = {
            "fact_key": str(action.get("fact_key") or ""),
            "value": action.get("value"),
            "provenance": str(action.get("provenance") or ""),
            "source_digest": str(action.get("source_digest") or ""),
        }
        content = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        existing = await self.workspace.hash(path)
        written = await self.workspace.write(path, content, existing, **kwargs)
        self._records[path] = {**record, **written}
        return self._records[path]

    async def verify_fact(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        path = str(dict(action.get("target") or {}).get("path") or "")
        try:
            raw = await self.workspace.read(path)
            value = json.loads(str(raw.get("content") or "{}"))
        except (DesktopdRejected, json.JSONDecodeError):
            return {"verified": False}
        return {"verified": value == {
            "fact_key": str(action.get("fact_key") or ""),
            "value": action.get("value"),
            "provenance": str(action.get("provenance") or ""),
            "source_digest": str(action.get("source_digest") or ""),
        }, **value}


class DesktopdTaskGrantAdapter:
    """Registers Brain-owned task scope inside desktopd.

    The public bridge never exposes these protobuf actions.  BrowserService
    consumes the resulting server-side record; request-body domains are not an
    authority source.
    """

    def __init__(self, client: DesktopdClient):
        self.client = client

    async def register(self, task_id: str, allowed_domains: tuple[str, ...]) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "task_domain_grant",
            {"allowed_domains": list(allowed_domains)}, task_id=task_id, timeout=5,
        )
        return dict(result.get("data") or {})

    async def revoke(self, task_id: str) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "task_domain_revoke", {}, task_id=task_id, timeout=5,
        )
        return dict(result.get("data") or {})

    async def clear(self) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self.client.execute, "task_domain_clear", {}, timeout=5,
        )
        return dict(result.get("data") or {})


def production_services(client: DesktopdClient | None = None) -> dict[str, Any]:
    client = client or DesktopdClient()
    workspace = DesktopdWorkspaceAdapter(client)
    return {
        "workspace_root": os.environ.get("PAIRPUTER_WORKSPACE", "/home/app/workspace"),
        "workspace": workspace,
        "processes": DesktopdProcessAdapter(client),
        "browser": DesktopdBrowserAdapter(client),
        "apps": DesktopdAppsAdapter(client),
        "cross_app": DesktopdCrossAppAdapter(workspace),
        "task_grants": DesktopdTaskGrantAdapter(client),
        "desktopd": client,
    }
