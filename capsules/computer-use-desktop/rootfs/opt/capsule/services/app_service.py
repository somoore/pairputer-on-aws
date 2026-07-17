from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .common import action_result, evidence, require_action_envelope
from .control_state import LeaseRejected


DEFAULT_APPS = {
    # The browser is not running until asked. apps_open("browser") launches it on demand; it's
    # single-instance, so a repeat open raises the existing window. _wait_for_browser verifies it.
    "browser": ["/usr/local/bin/pairputer-chromium"],
    "editor": ["gnome-text-editor"],
    "files": ["nautilus", "--no-desktop"],
    "terminal": ["xterm", "-e", "/opt/capsule/terminal-session.sh"],
}

APP_PRINCIPALS = {
    "browser": ("app", "/home/app", "/run/user/1000"),
    "editor": ("app", "/home/app", "/run/user/1000"),
    "files": ("app", "/home/app", "/run/user/1000"),
    "terminal": ("terminal", "/home/terminal", "/run/user/1001"),
}


def browser_launch_argv(display: str = ":1") -> list[str]:
    """Module-level browser launch argv (same principal/env as AppService._launch_argv).

    Shared with browser_service's on-demand launch: the browser is intentionally NOT running until
    a human or the model opens it, so browser_open must be able to start it when CDP is down."""
    user, home, runtime_dir = APP_PRINCIPALS["browser"]
    environment = {
        "HOME": home, "USER": user, "LOGNAME": user,
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
        "DISPLAY": display, "XAUTHORITY": "/run/pairputer/xauthority",
        "XDG_RUNTIME_DIR": runtime_dir, "XDG_SESSION_TYPE": "x11",
        "GDK_BACKEND": "x11", "GSK_RENDERER": "cairo",
        "LIBGL_ALWAYS_SOFTWARE": "1", "NO_AT_BRIDGE": "0",
        "GTK_MODULES": ":atk-bridge",
    }
    environment.update(AppService._session_environment(runtime_dir))
    assignments = [f"{key}={value}" for key, value in sorted(environment.items())]
    return ["runuser", "-u", user, "--", "env", "-i", *assignments, *DEFAULT_APPS["browser"]]


class AppService:
    """Launch allowlisted GUI applications under their unprivileged desktop UID.

    ``desktopd`` is intentionally root so it can enforce the shared mutation
    boundary.  That authority must never leak into a launched application, nor
    may a short-lived launcher PID be mistaken for a healthy application.
    """

    def __init__(self, control, windows, allowed_apps=None, display=":1", *,
                 launcher=None, browser_probe=None, pid_probe=None,
                 sleep=None, monotonic=None, launch_timeout=12.0):
        self.control, self.windows = control, windows
        self.allowed_apps = dict(allowed_apps or DEFAULT_APPS)
        self.display = display
        self.processes = {}
        self._launcher = launcher or subprocess.Popen
        self._browser_probe = browser_probe or self._default_browser_probe
        self._pid_probe = pid_probe or self._default_browser_pid
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self.launch_timeout = float(launch_timeout)

    def list_apps(self):
        return {"ok": True, "apps": [{"id": key, "argv": value,
                 "installed": os.path.exists(value[0]) if value[0].startswith("/") else True}
                 for key, value in self.allowed_apps.items()], **self.control.snapshot()}

    @staticmethod
    def _session_environment(runtime_dir: str) -> dict[str, str]:
        result: dict[str, str] = {}
        path = Path(runtime_dir) / "session.env"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return result
        for line in lines:
            key, separator, value = line.partition("=")
            if separator and key in {"DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS"} \
                    and value and "\x00" not in value and "\n" not in value:
                result[key] = value
        return result

    def _launch_argv(self, app: str) -> list[str]:
        user, home, runtime_dir = APP_PRINCIPALS.get(app, ("app", "/home/app", "/run/user/1000"))
        environment = {
            "HOME": home, "USER": user, "LOGNAME": user,
            "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "DISPLAY": self.display, "XAUTHORITY": "/run/pairputer/xauthority",
            "XDG_RUNTIME_DIR": runtime_dir, "XDG_SESSION_TYPE": "x11",
            "GDK_BACKEND": "x11", "GSK_RENDERER": "cairo",
            "LIBGL_ALWAYS_SOFTWARE": "1", "NO_AT_BRIDGE": "0",
            "GTK_MODULES": ":atk-bridge",
        }
        environment.update(self._session_environment(runtime_dir))
        assignments = [f"{key}={value}" for key, value in sorted(environment.items())]
        return ["runuser", "-u", user, "--", "env", "-i", *assignments,
                *self.allowed_apps[app]]

    @staticmethod
    def _default_browser_probe() -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=.5) as response:
                value = json.loads(response.read(65536))
            endpoint = str(value.get("webSocketDebuggerUrl") or "")
            return endpoint.startswith("ws://127.0.0.1:9222/")
        except Exception:
            return False

    @staticmethod
    def _default_browser_pid() -> int | None:
        prefix = b"/opt/chromium/chrome\0"
        marker = b"--user-data-dir=/home/app/.config/chromium\0"
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != 1000:
                    continue
                command = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if command.startswith(prefix) and marker in command:
                return int(entry.name)
        return None

    def _browser_window_visible(self) -> bool:
        try:
            windows, _ = self.windows.list_windows()
        except Exception:
            return False
        return any("chrom" in (str(item.get("appIdentity", "")) + " " +
                               str(item.get("title", ""))).lower()
                   for item in windows)

    def _wait_for_browser(self, launcher) -> int | None:
        deadline = self._monotonic() + self.launch_timeout
        while self._monotonic() < deadline:
            pid = self._pid_probe()
            if pid and self._browser_probe() and self._browser_window_visible():
                return int(pid)
            # A launcher may exit successfully after handing off to an already
            # starting singleton. Keep polling for the verified main process.
            if launcher.poll() not in {None, 0}:
                break
            self._sleep(.1)
        return None

    @staticmethod
    def _stop_launcher(proc) -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def open(self, request: dict):
        action_id, epoch, revision, _ = require_action_envelope(request)
        app = str(request.get("app_id", ""))
        if app not in self.allowed_apps:
            raise ValueError("application is not allowed")
        try:
            with self.control.commit(epoch, revision) as state:
                # Never inherit root HOME, credentials, or service authority.
                proc = self._launcher(
                    self._launch_argv(app),
                    env={"PATH": "/usr/sbin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                    stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                )
                self.processes[app] = proc
                starting_state = dict(state)
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="application.launch", summary="application launch rejected", reason=exc.reason)

        if app == "browser":
            verified_pid = self._wait_for_browser(proc)
            if verified_pid is None:
                current = self.control.snapshot()
                # Do not kill anything after a human takeover; the human may
                # have adopted or replaced the process while readiness waited.
                if current["humanEpoch"] == epoch:
                    self._stop_launcher(proc)
                self.processes.pop(app, None)
                return action_result(
                    accepted=False, action_id=action_id, state=current,
                    actuator="application.launch", summary="browser failed verified startup",
                    reason="effect_failed", retry_safety="safe",
                )
            data = {"appId": app, "pid": verified_pid, "ready": True,
                    "cdpReady": True, "topLevelWindow": True}
        else:
            self._sleep(.05)
            if proc.poll() not in {None, 0}:
                current = self.control.snapshot()
                self.processes.pop(app, None)
                return action_result(accepted=False, action_id=action_id, state=current,
                                     actuator="application.launch", summary=f"{app} failed startup",
                                     reason="effect_failed", retry_safety="safe")
            data = {"appId": app, "pid": proc.pid, "ready": proc.poll() is None}

        return action_result(
            accepted=True, action_id=action_id, state=starting_state,
            actuator="application.launch", summary=f"opened {app}", data=data,
            evidence_items=[evidence("application_process", **data)],
        )

    def list_windows(self):
        windows, warnings = self.windows.list_windows()
        return {"ok": True, "windows": windows, "warnings": warnings, **self.control.snapshot()}

    def focus_window(self, request: dict):
        action_id, epoch, revision, _ = require_action_envelope(request)
        window_id = str(request.get("window_id", ""))
        current, _ = self.windows.list_windows()
        if window_id not in {item["windowId"] for item in current}:
            raise ValueError("window selector did not resolve uniquely")
        try:
            with self.control.commit(epoch, revision) as state:
                self.windows.focus(window_id)
                return action_result(accepted=True, action_id=action_id, state=state,
                    actuator="ewmh.focus", summary="focused window", data={"windowId": window_id},
                    evidence_items=[evidence("window_focused", windowId=window_id)])
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="ewmh.focus", summary="focus rejected", reason=exc.reason)
