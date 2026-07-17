"""Bounded argv-first jobs with PTYs, process groups, and takeover policies."""

from __future__ import annotations

import collections
import ctypes
import os
import pty
import pwd
import select
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .common import action_result, evidence, require_action_envelope
from .control_state import ControlState, LeaseRejected
from evidence import redact_text


class ProcessService:
    MAX_JOBS = 32
    MAX_ARGV = 128
    MAX_ARG = 8192
    MAX_TAIL = 256 * 1024
    MAX_LOG_BYTES = 8 * 1024 * 1024
    ENV_ALLOW = {"LANG", "LC_ALL", "TERM", "NO_COLOR", "CI", "PATH"}
    JOB_USER = os.environ.get("PAIRPUTER_JOB_USER", "job")
    EMPTY_X11_DIR = os.environ.get("PAIRPUTER_JOB_EMPTY_X11_DIR", "/run/pairputer/job-empty-x11")
    JOB_CGROUP_PATH = os.environ.get("PAIRPUTER_JOB_CGROUP_PATH", "/sys/fs/cgroup/pairputer-jobs")

    def __init__(self, workspace, control: ControlState, terminal_log: str | os.PathLike[str] | None = None):
        self.workspace = workspace
        self.control = control
        self.terminal_log = Path(terminal_log or (workspace.root.parent / ".pairputer-internal" / "terminal.log"))
        self.terminal_log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.terminal_log.touch(mode=0o600, exist_ok=True)
        self._jobs: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._epoch = control.snapshot()["humanEpoch"]
        threading.Thread(target=self._takeover_watcher, daemon=True, name="job-takeover-watcher").start()

    def _cwd(self, raw: str):
        parts = self.workspace._parts(raw or ".", allow_root=True)
        fd = self.workspace._open_dir(parts)
        prefix = "/proc/self/fd" if os.path.exists("/proc/self/fd") else "/dev/fd"
        return f"{prefix}/{fd}", fd

    def _environment(self, overrides: dict | None) -> dict:
        env = {key: value for key, value in os.environ.items() if key in self.ENV_ALLOW}
        # Never copy the broker's trusted X11 cookie into an untrusted job.
        # The mount namespace and Xvnc transport setting enforce isolation;
        # these explicit values also make accidental credential inheritance
        # fail closed for X11 clients launched by project code.
        env.update({"HOME": f"/home/{self.JOB_USER}", "USER": self.JOB_USER,
                    "LOGNAME": self.JOB_USER, "TERM": "xterm-256color",
                    "DISPLAY": "", "XAUTHORITY": "/dev/null"})
        for key, value in (overrides or {}).items():
            if key not in self.ENV_ALLOW or not isinstance(value, str) or len(value) > 4096:
                raise ValueError(f"environment key not allowed: {key}")
            env[key] = value
        return env

    @staticmethod
    def _require_job_sandbox() -> bool:
        return os.environ.get("PAIRPUTER_REQUIRE_JOB_SANDBOX", "false").lower() in {
            "1", "true", "yes"
        }

    @classmethod
    def _isolate_job(cls) -> None:
        """Create a private mount view with no desktop socket and no privilege gain."""
        libc = ctypes.CDLL(None, use_errno=True)
        libc.unshare.argtypes = [ctypes.c_int]
        libc.unshare.restype = ctypes.c_int
        libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                               ctypes.c_ulong, ctypes.c_void_p]
        libc.mount.restype = ctypes.c_int
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                               ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        clone_newns, clone_newnet = 0x00020000, 0x40000000
        flags = clone_newns
        if os.environ.get("PAIRPUTER_ISOLATE_JOB_NETWORK", "false").lower() == "true":
            flags |= clone_newnet
        ms_bind, ms_rec, ms_private = 4096, 16384, 1 << 18
        if libc.unshare(flags) != 0:
            raise OSError(ctypes.get_errno(), "unshare(CLONE_NEWNS) failed")
        if libc.mount(None, b"/", None, ms_rec | ms_private, None) != 0:
            raise OSError(ctypes.get_errno(), "making job mounts private failed")
        source = os.fsencode(cls.EMPTY_X11_DIR)
        if libc.mount(source, b"/tmp/.X11-unix", None, ms_bind | ms_rec, None) != 0:
            raise OSError(ctypes.get_errno(), "hiding the X11 socket failed")
        # Prevent setuid/file-capability binaries in a project dependency from
        # regaining authority after the UID transition.
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")

    @classmethod
    def _child_setup(cls, cwd_fd: int) -> None:
        os.fchdir(cwd_fd)
        if os.geteuid() == 0:
            if cls._require_job_sandbox():
                cls._isolate_job()
            cgroup_path = Path(cls.JOB_CGROUP_PATH) / "cgroup.procs"
            if cgroup_path.exists():
                try:
                    cgroup_path.write_text(f"{os.getpid()}\n")
                except OSError as exc:
                    raise PermissionError("required job cgroup is unavailable") from exc
            elif (os.environ.get("PAIRPUTER_ALLOW_UID_FIREWALL", "false").lower() != "true"
                  and os.environ.get("PAIRPUTER_ISOLATE_JOB_NETWORK", "false").lower() != "true"):
                raise PermissionError("required job cgroup is unavailable")
            account = pwd.getpwnam(cls.JOB_USER)
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            os.umask(0o007)
        elif cls._require_job_sandbox():
            raise PermissionError("required job sandbox needs a root broker")

    def start(self, request: dict) -> dict:
        action_id, epoch, revision, _ = require_action_envelope(request)
        argv = request.get("argv")
        shell = request.get("shell")
        if shell is not None:
            if not request.get("explicit_shell_mode") or not isinstance(shell, str) or len(shell) > 32768:
                raise ValueError("shell commands require explicit_shell_mode and a bounded command")
            command = ["/bin/bash", "--noprofile", "--norc", "-c", shell]
        else:
            if not isinstance(argv, list) or not argv or len(argv) > self.MAX_ARGV:
                raise ValueError("argv must be a non-empty bounded list")
            if any(not isinstance(arg, str) or not arg or len(arg) > self.MAX_ARG for arg in argv):
                raise ValueError("invalid argv entry")
            command = list(argv)
        with self._lock:
            if sum(1 for job in self._jobs.values() if job["state"] == "running") >= self.MAX_JOBS:
                raise ValueError("job limit reached")
        cwd, cwd_fd = self._cwd(str(request.get("cwd", ".")))
        takeover = str(request.get("takeover_policy", "stop_on_handoff"))
        if takeover not in {"stop_on_handoff", "continue_background"}:
            raise ValueError("invalid takeover_policy")
        master, slave = pty.openpty()
        job_id = str(uuid.uuid4())
        try:
            with self.control.commit(epoch, revision) as state:
                # fchdir uses the already no-follow-validated directory descriptor;
                # a path swap cannot redirect the child outside the workspace.
                proc = subprocess.Popen(command, cwd=None, env=self._environment(request.get("environment")),
                                        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                        close_fds=True, pass_fds=(cwd_fd,),
                                        preexec_fn=lambda: self._child_setup(cwd_fd))
                job = {"jobId": job_id, "process": proc, "master": master, "state": "running",
                       "argv": command, "cwd": cwd, "startedAt": time.time(), "endedAt": None,
                       "exitCode": None, "tail": collections.deque(), "tailBytes": 0,
                       "takeoverPolicy": takeover, "startEpoch": epoch, "cancelReason": ""}
                with self._lock:
                    self._jobs[job_id] = job
                threading.Thread(target=self._read_job, args=(job,), daemon=True,
                                 name=f"job-reader-{job_id[:8]}").start()
                return action_result(accepted=True, action_id=action_id, state=state,
                    actuator="process.pty", summary=f"started tracked job {job_id}",
                    data={"jobId": job_id, "pid": proc.pid, "state": "running"},
                    evidence_items=[evidence("process_started", jobId=job_id, pid=proc.pid,
                                             cwd=str(request.get("cwd", ".")), argv=command)])
        except LeaseRejected as exc:
            os.close(master)
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="process.pty", summary="job start rejected", reason=exc.reason)
        finally:
            os.close(slave)
            os.close(cwd_fd)

    def _append(self, job: dict, data: bytes):
        if not data:
            return
        with self._lock:
            job["tail"].append(data)
            job["tailBytes"] += len(data)
            while job["tailBytes"] > self.MAX_TAIL and job["tail"]:
                job["tailBytes"] -= len(job["tail"].popleft())

    def _write_terminal_summary(self, job: dict) -> None:
        try:
            with self._lock:
                raw = b"".join(job["tail"])[-self.MAX_TAIL:]
                if self.terminal_log.stat().st_size >= self.MAX_LOG_BYTES:
                    rotated = self.terminal_log.with_suffix(self.terminal_log.suffix + ".1")
                    rotated.unlink(missing_ok=True)
                    os.replace(self.terminal_log, rotated)
                    self.terminal_log.touch(mode=0o600)
                with self.terminal_log.open("ab") as log:
                    clean = redact_text(raw.decode("utf-8", "replace"), limit=self.MAX_TAIL)
                    log.write(f"\n[job {job['jobId']}] {clean}".encode())
        except OSError:
            pass

    def _read_job(self, job: dict):
        master, proc = job["master"], job["process"]
        try:
            while proc.poll() is None:
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        self._append(job, os.read(master, 65536))
                    except OSError:
                        break
            while True:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self._append(job, data)
        finally:
            with self._lock:
                job["exitCode"] = proc.wait()
                job["state"] = "canceled" if job["cancelReason"] else "completed"
                job["endedAt"] = time.time()
            self._write_terminal_summary(job)
            try:
                os.close(master)
            except OSError:
                pass

    def _takeover_watcher(self):
        while not self._stop.wait(0.05):
            current = self.control.snapshot()["humanEpoch"]
            if current == self._epoch:
                continue
            self._epoch = current
            with self._lock:
                jobs = list(self._jobs.values())
            for job in jobs:
                if job["state"] == "running" and job["takeoverPolicy"] == "stop_on_handoff":
                    self._terminate(job, "human_takeover")

    def _terminate(self, job: dict, reason: str):
        proc = job["process"]
        job["cancelReason"] = reason
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 1.0
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def status(self, job_id: str, tail_bytes: int = 65536) -> dict:
        tail_bytes = max(0, min(int(tail_bytes), self.MAX_TAIL))
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError("unknown job")
            job = self._jobs[job_id]
            raw = b"".join(job["tail"])[-tail_bytes:]
            return {"ok": True, "jobId": job_id, "state": job["state"],
                    "exitCode": job["exitCode"], "startedAt": job["startedAt"],
                    "endedAt": job["endedAt"], "takeoverPolicy": job["takeoverPolicy"],
                    "output": redact_text(raw.decode("utf-8", "replace"), limit=tail_bytes),
                    "outputTruncated": job["tailBytes"] > len(raw), **self.control.snapshot()}

    def cancel(self, request: dict) -> dict:
        action_id, epoch, revision, _ = require_action_envelope(request)
        with self._lock:
            job = self._jobs.get(str(request.get("job_id", "")))
        if not job:
            raise ValueError("unknown job")
        try:
            with self.control.commit(epoch, revision) as state:
                if job["state"] == "running":
                    self._terminate(job, "explicit_cancel")
                return action_result(accepted=True, action_id=action_id, state=state,
                    actuator="process.signal", summary=f"canceled job {job['jobId']}",
                    data={"jobId": job["jobId"], "state": job["state"]},
                    evidence_items=[evidence("process_canceled", jobId=job["jobId"])])
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="process.signal", summary="cancel rejected", reason=exc.reason)
