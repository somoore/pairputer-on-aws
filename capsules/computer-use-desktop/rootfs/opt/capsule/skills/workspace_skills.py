"""Directory-fd-confined, hash-checked, atomic workspace skills."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import inspect
import os
import stat
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .base import BaseSkill, PreparedEffect, RawResult, SkillContext, SkillDefinition, Verification

MAX_READ_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 8 * 1024 * 1024
_LIBC = ctypes.CDLL(None, use_errno=True)


async def _adapter_call(context: SkillContext, method: str, *args: Any, **kwargs: Any) -> Any:
    adapter = context.services.get("workspace")
    if adapter is None or not hasattr(adapter, method):
        raise WorkspaceViolation(f"workspace adapter does not provide {method}")
    result = getattr(adapter, method)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _brokered(context: SkillContext) -> bool:
    return context.services.get("workspace") is not None


def _effect_kwargs(context: SkillContext, lease: Any) -> dict[str, Any]:
    return {
        "task_id": context.task_id, "step_id": context.step_id,
        "action_id": context.action_id,
        "expected_human_epoch": lease.human_epoch,
        "expected_world_revision": lease.world_revision,
    }


def _workspace_root(context: SkillContext) -> Path:
    return Path(str(context.services.get("workspace_root", "/home/app/workspace"))).resolve(strict=True)


class _DirRef:
    def __init__(self, path_value: Any, context: SkillContext):
        self.root = _workspace_root(context)
        raw = Path(str(path_value or "."))
        if raw.is_absolute():
            try:
                relative = raw.relative_to(self.root)
            except ValueError as exc:
                raise WorkspaceViolation("directory escapes the workspace") from exc
        else:
            relative = PurePosixPath(str(path_value or "."))
        parts = () if str(relative) in {"", "."} else tuple(relative.parts)
        if any(part in {"", ".", ".."} for part in parts):
            raise WorkspaceViolation("directory must be a normalized workspace path")
        self.relative = "/".join(parts) or "."
        root_dev = self.root.stat().st_dev
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
                if os.fstat(next_fd).st_dev != root_dev:
                    os.close(next_fd)
                    raise WorkspaceViolation("mount traversal is not permitted")
                os.close(self.fd)
                self.fd = next_fd
        except BaseException:
            self.close()
            raise

    @property
    def proc_path(self) -> str:
        if os.path.exists("/proc/self/fd"):
            return f"/proc/self/fd/{self.fd}"
        return fcntl.fcntl(self.fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Ref:
    """An opened, no-follow parent plus a workspace-relative leaf name."""

    def __init__(self, path_value: Any, context: SkillContext, *, must_exist: bool):
        self.root = _workspace_root(context)
        raw = Path(str(path_value))
        if raw.is_absolute():
            try:
                relative = raw.relative_to(self.root)
            except ValueError as exc:
                raise WorkspaceViolation("path escapes the workspace") from exc
        else:
            relative = PurePosixPath(str(path_value))
        parts = tuple(relative.parts)
        if (not parts or any(part in {"", ".", ".."} for part in parts) or
                parts[0] in {".pairputer-internal", ".Trash"}):
            raise WorkspaceViolation("path must be a normalized non-reserved workspace path")
        self.relative = "/".join(parts)
        self.name = parts[-1]
        self.root_dev = self.root.stat().st_dev
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.parent_fd = os.dup(self.root_fd)
        try:
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                      dir_fd=self.parent_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise WorkspaceViolation("symlink traversal is not permitted") from exc
                    raise
                info = os.fstat(next_fd)
                if info.st_dev != self.root_dev:
                    os.close(next_fd)
                    raise WorkspaceViolation("mount traversal is not permitted")
                os.close(self.parent_fd)
                self.parent_fd = next_fd
            self.assert_confined()
            if must_exist:
                self.stat()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for name in ("parent_fd", "root_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def assert_confined(self) -> None:
        try:
            if os.path.exists("/proc/self/fd"):
                raw = os.readlink(f"/proc/self/fd/{self.parent_fd}")
            else:
                raw = fcntl.fcntl(self.parent_fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode()
            actual = Path(raw).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise WorkspaceViolation("workspace directory identity is unavailable") from exc
        if actual != self.root and self.root not in actual.parents:
            raise WorkspaceViolation("workspace directory moved outside the root")

    def stat(self):
        try:
            info = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != self.root_dev or
                info.st_nlink != 1):
            raise WorkspaceViolation("artifact must be one regular, unlinked file on the workspace device")
        return info

    def exists(self) -> bool:
        try:
            self.stat()
            return True
        except FileNotFoundError:
            return False

    def hash(self) -> str | None:
        try:
            fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_dev != self.root_dev or
                    info.st_nlink != 1 or info.st_size > MAX_WRITE_BYTES):
                raise WorkspaceViolation("artifact is not a bounded single-link workspace file")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    return digest.hexdigest()
                total += len(chunk)
                if total > MAX_WRITE_BYTES:
                    raise WorkspaceViolation("artifact exceeds bounded size")
                digest.update(chunk)
        finally:
            os.close(fd)

    def read(self) -> tuple[bytes, str]:
        fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_dev != self.root_dev or
                    info.st_nlink != 1 or info.st_size > MAX_WRITE_BYTES):
                raise WorkspaceViolation("artifact is not a bounded single-link workspace file")
            data = bytearray()
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
                digest.update(chunk)
            return bytes(data), digest.hexdigest()
        finally:
            os.close(fd)


def _open_trash(root: Path) -> tuple[int, int]:
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(".Trash", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        try:
            trash_fd = os.open(".Trash", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise WorkspaceViolation("trash directory must not be a symlink") from exc
            raise
        if os.fstat(trash_fd).st_dev != os.fstat(root_fd).st_dev:
            os.close(trash_fd)
            raise WorkspaceViolation("trash directory is outside the workspace device")
        return root_fd, trash_fd
    except BaseException:
        os.close(root_fd)
        raise


def _rename_noreplace(source: str, destination: str, source_fd: int, destination_fd: int) -> None:
    renameat2 = getattr(_LIBC, "renameat2", None)
    if renameat2 is not None:
        result = renameat2(source_fd, os.fsencode(source), destination_fd,
                           os.fsencode(destination), 1)
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise WorkspaceConflict("destination already exists")
        if error not in {errno.ENOSYS, errno.EINVAL}:
            raise OSError(error, os.strerror(error))
    try:
        os.link(source, destination, src_dir_fd=source_fd, dst_dir_fd=destination_fd,
                follow_symlinks=False)
    except FileExistsError as exc:
        raise WorkspaceConflict("destination already exists") from exc
    os.unlink(source, dir_fd=source_fd)


def _temporary(parent_fd: int, content: bytes) -> str:
    name = f".pairputer-{time.time_ns()}-{uuid.uuid4().hex}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(content)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return name


def _hash(path: Any, context: SkillContext) -> str | None:
    with _Ref(path, context, must_exist=False) as ref:
        return ref.hash()


def _trash_hash(context: SkillContext, relative: str) -> str | None:
    prefix = ".Trash/"
    if not isinstance(relative, str) or not relative.startswith(prefix) or "/" in relative[len(prefix):]:
        raise WorkspaceViolation("invalid trash reference")
    root_fd, trash_fd = _open_trash(_workspace_root(context))
    name = relative[len(prefix):]
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=trash_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_WRITE_BYTES:
                raise WorkspaceViolation("trash artifact is not a bounded single-link file")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
        finally:
            os.close(fd)
    except FileNotFoundError:
        return None
    finally:
        os.close(trash_fd); os.close(root_fd)


class InspectArtifactSkill(BaseSkill):
    definition = SkillDefinition("workspace.inspect", "workspace.read", "workspace_read", "read_only",
        "interruptible", "retryable", ("fast", "visible", "hybrid"), ("artifact_inspected",), 10, "safe")

    async def execute(self, prepared: PreparedEffect, lease, context: SkillContext) -> RawResult:
        context.control.checkpoint(lease)
        if _brokered(context):
            observed = dict(await _adapter_call(context, "read", str(prepared.action["path"])))
            return RawResult(False, observed)
        with _Ref(prepared.action["path"], context, must_exist=True) as ref:
            data, digest = ref.read()
            bounded = data[:MAX_READ_BYTES]
            return RawResult(False, {"path": ref.relative, "sha256": digest, "size": len(data),
                "content": bounded.decode("utf-8", errors="replace"), "truncated": len(data) > len(bounded)})

    async def verify(self, prepared, raw, snapshot, context):
        digest = (await _adapter_call(context, "hash", raw.result["path"])
                  if _brokered(context) else _hash(raw.result["path"], context))
        verified = digest == raw.result.get("sha256")
        return Verification(verified, {"artifact_inspected": verified}, raw.result, "artifact read and hash observed")


class CreateArtifactSkill(BaseSkill):
    definition = SkillDefinition("workspace.create", "workspace.write", "workspace_write", "local_reversible",
        "atomic_commit", "stable_key", ("fast", "hybrid"), ("artifact_created",), 15, "expected_hash")

    async def inspect(self, args, snapshot, context):
        if _brokered(context):
            if await _adapter_call(context, "exists", str(args["path"])):
                raise WorkspaceConflict("create target already exists")
            return {"path_absent": True, "parent": str(PurePosixPath(str(args["path"])).parent)}
        with _Ref(args["path"], context, must_exist=False) as ref:
            if ref.exists():
                raise WorkspaceConflict("create target already exists")
            return {"path_absent": True, "parent": "/".join(ref.relative.split("/")[:-1]) or "."}

    async def execute(self, prepared, lease, context):
        content = str(prepared.action.get("content", "")).encode()
        if len(content) > MAX_WRITE_BYTES:
            raise WorkspaceViolation("write exceeds bounded artifact size")
        if _brokered(context):
            context.control.checkpoint(lease)
            result = await _adapter_call(
                context, "write", str(prepared.action["path"]), content.decode(), None,
                **_effect_kwargs(context, lease),
            )
            return RawResult(True, dict(result), retry_safety="inspect_before_retry")
        with _Ref(prepared.action["path"], context, must_exist=False) as ref:
            temporary = _temporary(ref.parent_fd, content)
            committed = False
            try:
                def commit():
                    nonlocal committed
                    ref.assert_confined()
                    _rename_noreplace(temporary, ref.name, ref.parent_fd, ref.parent_fd)
                    committed = True
                    try:
                        ref.assert_confined()
                    except BaseException:
                        os.unlink(ref.name, dir_fd=ref.parent_fd)
                        raise
                    os.fsync(ref.parent_fd)
                context.control.atomic_commit(lease, commit)
            finally:
                if not committed:
                    try: os.unlink(temporary, dir_fd=ref.parent_fd)
                    except FileNotFoundError: pass
            digest = hashlib.sha256(content).hexdigest()
            return RawResult(True, {"path": ref.relative, "before_sha256": None,
                                    "after_sha256": digest, "size": len(content)})

    async def verify(self, prepared, raw, snapshot, context):
        digest = (await _adapter_call(context, "hash", raw.result["path"])
                  if _brokered(context) else _hash(raw.result["path"], context))
        verified = digest == raw.result["after_sha256"]
        return Verification(verified, {"artifact_created": verified},
                            {**raw.result, "observed_sha256": digest}, "created artifact hash observed")


class CreateDirectorySkill(BaseSkill):
    definition = SkillDefinition(
        "workspace.mkdir", "workspace.write", "workspace_write", "local_reversible",
        "atomic_commit", "stable_key", ("fast", "hybrid"),
        ("directory_created",), 15, "inspect_directory",
    )

    async def inspect(self, args, snapshot, context):
        raw = PurePosixPath(str(args.get("path") or ""))
        parts = tuple(raw.parts)
        if (raw.is_absolute() or not parts or len(parts) > 32
                or any(part in {"", ".", ".."} for part in parts)):
            raise WorkspaceViolation("directory must be a normalized bounded workspace path")
        if not _brokered(context):
            raise WorkspaceViolation("directory creation requires the shared workspace broker")
        return {"path": "/".join(parts), "parents": bool(args.get("parents", True))}

    async def execute(self, prepared, lease, context):
        context.control.checkpoint(lease)
        result = await _adapter_call(
            context, "mkdir", str(prepared.action["path"]),
            bool(prepared.action.get("parents", True)),
            **_effect_kwargs(context, lease),
        )
        return RawResult(True, dict(result), retry_safety="inspect_before_retry")

    async def verify(self, prepared, raw, snapshot, context):
        path = str(raw.result.get("path") or prepared.action["path"])
        verified = bool(await _adapter_call(context, "directory_exists", path))
        return Verification(
            verified, {"directory_created": verified},
            {**raw.result, "directory_exists": verified},
            "workspace directory observed through the shared broker",
        )


class PatchArtifactSkill(BaseSkill):
    definition = SkillDefinition("workspace.patch", "workspace.write", "workspace_write", "local_reversible",
        "atomic_commit", "stable_key_expected_hash", ("fast", "hybrid"), ("artifact_patched",), 15, "expected_hash")

    async def inspect(self, args, snapshot, context):
        current = (await _adapter_call(context, "hash", str(args["path"]))
                   if _brokered(context) else _hash(args["path"], context))
        expected = str(args.get("expected_sha256") or "")
        if not expected or current != expected:
            raise WorkspaceConflict("patch requires the current expected SHA-256")
        return {"before_sha256": current, "path": str(args["path"])}

    async def execute(self, prepared, lease, context):
        content = str(prepared.action.get("content", "")).encode()
        if len(content) > MAX_WRITE_BYTES:
            raise WorkspaceViolation("patch exceeds bounded artifact size")
        expected = str(prepared.action["expected_sha256"])
        if _brokered(context):
            context.control.checkpoint(lease)
            result = await _adapter_call(
                context, "write", str(prepared.action["path"]), content.decode(), expected,
                **_effect_kwargs(context, lease),
            )
            return RawResult(True, dict(result), retry_safety="inspect_before_retry")
        with _Ref(prepared.action["path"], context, must_exist=True) as ref:
            temporary = _temporary(ref.parent_fd, content)
            backup = f".pairputer-backup-{uuid.uuid4().hex}"
            try:
                def commit():
                    ref.assert_confined()
                    if ref.hash() != expected:
                        raise WorkspaceConflict("artifact changed before atomic rename")
                    os.link(ref.name, backup, src_dir_fd=ref.parent_fd, dst_dir_fd=ref.parent_fd,
                            follow_symlinks=False)
                    os.replace(temporary, ref.name, src_dir_fd=ref.parent_fd, dst_dir_fd=ref.parent_fd)
                    try:
                        ref.assert_confined()
                    except BaseException:
                        os.replace(backup, ref.name, src_dir_fd=ref.parent_fd, dst_dir_fd=ref.parent_fd)
                        raise
                    os.unlink(backup, dir_fd=ref.parent_fd)
                    os.fsync(ref.parent_fd)
                context.control.atomic_commit(lease, commit)
            finally:
                for name in (temporary, backup):
                    try: os.unlink(name, dir_fd=ref.parent_fd)
                    except FileNotFoundError: pass
            digest = hashlib.sha256(content).hexdigest()
            return RawResult(True, {"path": ref.relative, "before_sha256": expected,
                                    "after_sha256": digest, "size": len(content)})

    async def verify(self, prepared, raw, snapshot, context):
        digest = (await _adapter_call(context, "hash", raw.result["path"])
                  if _brokered(context) else _hash(raw.result["path"], context))
        verified = digest == raw.result["after_sha256"]
        return Verification(verified, {"artifact_patched": verified},
                            {**raw.result, "observed_sha256": digest}, "patched artifact hash observed")


class MoveArtifactSkill(BaseSkill):
    definition = SkillDefinition("workspace.move", "workspace.write", "workspace_write", "local_reversible",
        "atomic_commit", "stable_key_expected_hash", ("fast", "hybrid"), ("artifact_moved",), 15, "expected_hash")

    async def inspect(self, args, snapshot, context):
        expected = str(args.get("expected_sha256") or "")
        source_hash = (await _adapter_call(context, "hash", str(args["source"]))
                       if _brokered(context) else _hash(args["source"], context))
        if not expected or source_hash != expected:
            raise WorkspaceConflict("move requires the source expected SHA-256")
        if _brokered(context):
            if await _adapter_call(context, "exists", str(args["target"])):
                raise WorkspaceConflict("move target already exists")
            return {"source_sha256": expected, "target_absent": True}
        with _Ref(args["target"], context, must_exist=False) as target:
            if target.exists():
                raise WorkspaceConflict("move target already exists")
        return {"source_sha256": expected, "target_absent": True}

    async def execute(self, prepared, lease, context):
        expected = str(prepared.action["expected_sha256"])
        if _brokered(context):
            context.control.checkpoint(lease)
            result = await _adapter_call(
                context, "move", str(prepared.action["source"]),
                str(prepared.action["target"]), expected,
                **_effect_kwargs(context, lease),
            )
            return RawResult(True, dict(result), retry_safety="inspect_before_retry")
        with _Ref(prepared.action["source"], context, must_exist=True) as source, \
             _Ref(prepared.action["target"], context, must_exist=False) as target:
            def commit():
                source.assert_confined(); target.assert_confined()
                if source.hash() != expected or target.exists():
                    raise WorkspaceConflict("move precondition changed")
                _rename_noreplace(source.name, target.name, source.parent_fd, target.parent_fd)
                try:
                    source.assert_confined(); target.assert_confined()
                except BaseException:
                    _rename_noreplace(target.name, source.name, target.parent_fd, source.parent_fd)
                    raise
                os.fsync(source.parent_fd); os.fsync(target.parent_fd)
            context.control.atomic_commit(lease, commit)
            return RawResult(True, {"source": source.relative, "target": target.relative, "sha256": expected})

    async def verify(self, prepared, raw, snapshot, context):
        if _brokered(context):
            source = await _adapter_call(context, "hash", raw.result["source"])
            target = await _adapter_call(context, "hash", raw.result["target"])
        else:
            source = _hash(raw.result["source"], context)
            target = _hash(raw.result["target"], context)
        verified = source is None and target == raw.result["sha256"]
        return Verification(verified, {"artifact_moved": verified}, raw.result,
                            "move source absence and target hash observed")


class TrashArtifactSkill(BaseSkill):
    definition = SkillDefinition("workspace.trash", "workspace.write", "workspace_trash", "local_reversible",
        "atomic_commit", "stable_key_expected_hash", ("fast", "hybrid"), ("artifact_trashed",), 15, "expected_hash")

    async def inspect(self, args, snapshot, context):
        expected = str(args.get("expected_sha256") or "")
        current = (await _adapter_call(context, "hash", str(args["path"]))
                   if _brokered(context) else _hash(args["path"], context))
        if not expected or current != expected:
            raise WorkspaceConflict("trash requires the current expected SHA-256")
        return {"sha256": expected}

    async def execute(self, prepared, lease, context):
        expected = str(prepared.action["expected_sha256"])
        if _brokered(context):
            context.control.checkpoint(lease)
            result = await _adapter_call(
                context, "trash", str(prepared.action["path"]), expected,
                **_effect_kwargs(context, lease),
            )
            return RawResult(True, dict(result), retry_safety="inspect_before_retry")
        with _Ref(prepared.action["path"], context, must_exist=True) as source:
            root_fd, trash_fd = _open_trash(source.root)
            target = f"{time.time_ns()}-{uuid.uuid4().hex}-{source.name}"
            try:
                def commit():
                    source.assert_confined()
                    if source.hash() != expected:
                        raise WorkspaceConflict("artifact changed before trash commit")
                    _rename_noreplace(source.name, target, source.parent_fd, trash_fd)
                    try:
                        source.assert_confined()
                        trash_path = Path(os.readlink(f"/proc/self/fd/{trash_fd}")).resolve(strict=True)
                        if trash_path != source.root / ".Trash":
                            raise WorkspaceViolation("trash directory moved")
                    except BaseException:
                        _rename_noreplace(target, source.name, trash_fd, source.parent_fd)
                        raise
                    os.fsync(source.parent_fd); os.fsync(trash_fd)
                context.control.atomic_commit(lease, commit)
                return RawResult(True, {"path": source.relative, "trash_path": f".Trash/{target}", "sha256": expected})
            finally:
                os.close(trash_fd); os.close(root_fd)

    async def verify(self, prepared, raw, snapshot, context):
        if _brokered(context):
            source_absent = await _adapter_call(context, "hash", raw.result["path"]) is None
            verified = source_absent and bool(raw.result.get("trash_id")) and bool(raw.result.get("sha256"))
            return Verification(verified, {"artifact_trashed": verified}, raw.result,
                                "original absence and broker trash evidence observed")
        source_absent = _hash(raw.result["path"], context) is None
        # Reserved trash paths are intentionally not accepted by the public path parser.
        digest = _trash_hash(context, str(raw.result["trash_path"]))
        verified = source_absent and digest == raw.result["sha256"]
        return Verification(verified, {"artifact_trashed": verified}, raw.result,
                            "original absence and trash hash observed")


class WorkspaceViolation(PermissionError):
    pass


class WorkspaceConflict(RuntimeError):
    pass
