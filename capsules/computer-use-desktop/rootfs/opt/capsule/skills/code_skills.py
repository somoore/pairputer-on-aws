"""Tracked, bounded command execution abstraction."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping

from control_client import ControlError
from .base import BaseSkill, PreparedEffect, RawResult, SkillContext, SkillDefinition, Verification
from .workspace_skills import _DirRef

MAX_OUTPUT_BYTES = 128 * 1024
MAX_TOTAL_OUTPUT_BYTES = 16 * 1024 * 1024
ALLOWED_ENV = frozenset({"PATH", "LANG", "LC_ALL", "TERM", "CI", "NODE_ENV", "PYTHONPATH"})


class RunCommandSkill(BaseSkill):
    definition = SkillDefinition(
        "code.run_command", "process.exec", "process_start", "local_reversible",
        "interruptible", "inspect_before_retry", ("visible", "hybrid"),
        ("command_completed",), 300, "exit_and_effect_verification",
    )

    def canonical_action(self, args):
        clean = {key: value for key, value in args.items() if key not in {
            "effect", "kind", "risk", "risk_class", "capability", "requires_approval",
        }}
        # Approval must describe the effective command, including execution
        # defaults that would otherwise remain implicit at the point of risk.
        clean.setdefault("cwd", ".")
        clean.setdefault("shell", False)
        clean.setdefault("timeout_seconds", 60)
        clean.setdefault("expected_exit_code", 0)
        clean.setdefault("handoff_policy", "stop_on_handoff")
        executable = ""
        if isinstance(args.get("argv"), (list, tuple)) and args["argv"]:
            executable = os.path.basename(str(args["argv"][0])).lower()
        derived: dict[str, Any] = {}
        argv = tuple(str(item) for item in (args.get("argv") or ()))
        if args.get("shell") or executable in {"sh", "bash", "zsh", "dash"}:
            kind = "process_shell"
        elif executable in {"rm", "shred"}:
            kind = "permanent_delete"
            target = next((item for item in argv[1:] if not item.startswith("-")), "")
            if target:
                derived["path"] = str(Path(str(args.get("cwd") or ".")) / target)
        elif executable in {"chmod", "chown", "sudo", "su", "mount", "umount"}:
            kind = "permission_change"
        elif executable in {"curl", "wget"}:
            url = next((item for item in argv[1:] if item.startswith(("http://", "https://"))), "")
            kind = "external_submit" if url else "unknown"
            if url:
                derived["url"] = url
        elif executable in {"ssh", "scp", "sftp"}:
            kind = "credential_entry"
        else:
            kind = "process_start"
        return {"kind": kind, "capability": self.definition.capability, **clean, **derived}

    async def inspect(self, args, snapshot, context):
        argv = args.get("argv")
        shell = bool(args.get("shell", False))
        if shell:
            if "process.shell" not in context.services.get("allowed_capabilities", ()):
                raise CommandViolation("shell mode requires the explicit process.shell capability")
            if not isinstance(args.get("command"), str) or not args["command"].strip():
                raise CommandViolation("shell mode requires a non-empty command")
        elif not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise CommandViolation("argument-vector execution is required by default")
        with _DirRef(args.get("cwd", "."), context) as cwd:
            return {"cwd": cwd.relative, "shell": shell}

    async def execute(self, prepared, lease, context):
        action = prepared.action
        adapter = context.services.get("processes")
        if adapter is not None:
            if not hasattr(adapter, "run"):
                raise CommandViolation("process adapter does not provide tracked execution")
            context.control.checkpoint(lease)
            result = adapter.run(
                dict(action), task_id=context.task_id, step_id=context.step_id,
                action_id=context.action_id, expected_human_epoch=lease.human_epoch,
                expected_world_revision=lease.world_revision,
                presentation_mode=str(action.get("presentation_mode") or "hybrid"),
            )
            if hasattr(result, "__await__"):
                result = await result
            result = dict(result or {})
            return RawResult(
                True, result,
                retry_safety="safe" if result.get("exit_code") not in {0, None}
                else "inspect_before_retry",
            )
        cwd = _DirRef(action.get("cwd", "."), context)
        env = {key: value for key, value in os.environ.items() if key in ALLOWED_ENV}
        env.update({"HOME": str(cwd.root.parent), "USER": os.environ.get("USER", "agent")})
        for key, value in dict(action.get("env") or {}).items():
            if key not in ALLOWED_ENV:
                raise CommandViolation(f"environment variable is not allowed: {key}")
            env[key] = str(value)
        context.control.checkpoint(lease)
        def spawn() -> subprocess.Popen[bytes]:
            command: tuple[str, ...]
            command = (("/bin/bash", "--noprofile", "--norc", "-c", str(action["command"]))
                       if action.get("shell") else tuple(action["argv"]))
            return subprocess.Popen(
                command, cwd=cwd.proc_path, env=env, shell=False, pass_fds=(cwd.fd,),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
        try:
            process = context.control.atomic_commit(lease, spawn)
        finally:
            cwd.close()
        job_id = f"job_{uuid.uuid4().hex}"
        timeout = min(max(float(action.get("timeout_seconds", 60)), 0.1), self.definition.timeout_seconds)
        def drain(stream):
            tail, total = bytearray(), 0
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                tail.extend(chunk)
                if len(tail) > MAX_OUTPUT_BYTES:
                    del tail[:-MAX_OUTPUT_BYTES]
                if total > MAX_TOTAL_OUTPUT_BYTES:
                    try: os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                    raise CommandViolation("command output exceeded the bounded total")
            return bytes(tail), total
        stdout_task = asyncio.create_task(asyncio.to_thread(drain, process.stdout))
        stderr_task = asyncio.create_task(asyncio.to_thread(drain, process.stderr))
        wait_task = asyncio.create_task(asyncio.to_thread(process.wait))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while not wait_task.done():
                await asyncio.sleep(min(0.05, max(0.001, deadline - loop.time())))
                context.control.checkpoint(lease)
                if loop.time() >= deadline:
                    raise asyncio.TimeoutError
            await wait_task
            (stdout, stdout_total), (stderr, stderr_total) = await asyncio.gather(stdout_task, stderr_task)
        except (ControlError, asyncio.TimeoutError, CommandViolation):
            if process.returncode is None and str(action.get("handoff_policy", "stop_on_handoff")) == "stop_on_handoff":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 2)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for task in (stdout_task, stderr_task, wait_task):
                task.cancel()
            raise
        return RawResult(True, {
            "job_id": job_id, "pid": process.pid, "exit_code": process.returncode,
            "stdout_tail": stdout.decode(errors="replace"), "stderr_tail": stderr.decode(errors="replace"),
            "output_truncated": stdout_total > len(stdout) or stderr_total > len(stderr),
        }, retry_safety="safe" if process.returncode != 0 else "inspect_before_retry")

    async def verify(self, prepared, raw, snapshot, context):
        expected = int(prepared.action.get("expected_exit_code", 0))
        verified = raw.result.get("exit_code") == expected
        return Verification(
            verified, {"command_completed": verified}, raw.result,
            f"tracked job exited with {raw.result.get('exit_code')}; expected {expected}",
        )


class CommandViolation(PermissionError):
    pass
