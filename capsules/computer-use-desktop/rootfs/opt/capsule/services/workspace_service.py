"""Confined, hash-preconditioned, atomic workspace operations."""

from __future__ import annotations

import base64
import errno
import ctypes
import fcntl
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from .common import (IdempotencyStore, MAX_TEXT_BYTES, action_result, evidence,
                     mime_for, require_action_envelope, sha256_bytes, sha256_file)
from .control_state import ControlState, LeaseRejected


class WorkspaceError(ValueError):
    pass


class WorkspaceService:
    RESERVED = {".pairputer-trash", ".pairputer-internal"}
    _libc = ctypes.CDLL(None, use_errno=True)
    _UPLOAD_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
    MAX_UPLOAD_CHUNK = 512 * 1024
    MAX_DIRECTORY_DEPTH = 32

    def __init__(self, root: str | os.PathLike[str], control: ControlState,
                 state_dir: str | os.PathLike[str] | None = None,
                 max_file_bytes: int = 8 * 1024 * 1024):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspaceError("workspace root is not a directory")
        self.root_dev = self.root.stat().st_dev
        root_stat = self.root.stat()
        self.workspace_uid = root_stat.st_uid
        self.workspace_gid = root_stat.st_gid
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.control = control
        self.max_file_bytes = max_file_bytes
        self.state_dir = Path(state_dir or (self.root.parent / ".pairputer-internal"))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.idempotency = IdempotencyStore(self.state_dir)
        self.upload_dir = self.state_dir / "upload-staging"
        self.upload_dir.mkdir(mode=0o700, exist_ok=True)
        self.upload_dir.chmod(0o700)
        self.upload_ttl_seconds = max(60, min(int(os.environ.get(
            "PAIRPUTER_UPLOAD_TTL_SECONDS", "3600"
        )), 24 * 3600))
        self._upload_lock = threading.RLock()
        self._cleanup_uploads()

    def close(self):
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def _upload_paths(self, upload_id: str) -> tuple[Path, Path]:
        if not self._UPLOAD_ID.fullmatch(str(upload_id)):
            raise WorkspaceError("invalid upload_id")
        stem = sha256_bytes(str(upload_id).encode("utf-8"))
        return self.upload_dir / f"{stem}.part", self.upload_dir / f"{stem}.json"

    def _cleanup_uploads(self) -> None:
        cutoff = time.time() - self.upload_ttl_seconds
        try:
            metadata_files = tuple(self.upload_dir.glob("*.json"))
        except OSError:
            return
        for metadata_path in metadata_files:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                stale = float(metadata.get("updatedAt", 0)) < cutoff
            except (OSError, ValueError, json.JSONDecodeError):
                stale = True
            if not stale:
                continue
            data_path = metadata_path.with_suffix(".part")
            data_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
        for data_path in tuple(self.upload_dir.glob("*.part")):
            metadata_path = data_path.with_suffix(".json")
            try:
                orphaned = not metadata_path.exists() and data_path.stat().st_mtime < cutoff
            except OSError:
                orphaned = True
            if orphaned:
                data_path.unlink(missing_ok=True)

    @staticmethod
    def _write_upload_metadata(path: Path, value: dict) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _upload_receipt(action_id: str, state: dict, *, upload_id: str,
                        offset: int, received: int, total: int, complete: bool) -> dict:
        return {
            "accepted": True, "actionId": action_id, "reason": "",
            "humanEpoch": int(state["humanEpoch"]),
            "startingWorldRevision": int(state["worldRevision"]),
            "endingWorldRevision": int(state["worldRevision"]),
            "actuator": "workspace.upload_staging", "presentationMethod": "semantic",
            "summary": f"staged upload bytes {offset}..{offset + received}",
            "data": {"uploadId": upload_id, "offset": offset,
                     "receivedBytes": received, "stagedBytes": offset + received,
                     "totalSize": total, "complete": complete},
            "evidence": [evidence("upload_chunk", uploadId=upload_id, offset=offset,
                                  size=received, totalSize=total)],
            "retrySafety": "safe", "warnings": [],
        }

    def _parts(self, raw: str, *, allow_root: bool = False) -> tuple[str, ...]:
        if not isinstance(raw, str) or "\x00" in raw or len(raw) > 4096:
            raise WorkspaceError("invalid path")
        path = PurePosixPath(raw)
        if path.is_absolute() or any(p in ("", ".", "..") for p in path.parts):
            if allow_root and raw in ("", "."):
                return ()
            raise WorkspaceError("path must be a normalized workspace-relative path")
        if path.parts and path.parts[0] in self.RESERVED:
            raise WorkspaceError("path is reserved")
        return tuple(path.parts)

    def _open_dir(self, parts: tuple[str, ...], *, create: bool = False):
        # create=True: auto-make missing parent dirs (so write/upload "just works" without a
        # separate mkdir), staying fully confined — each new dir is created and re-opened with
        # O_NOFOLLOW under the trusted parent fd and re-checked for a mount escape, exactly like
        # traversal. This never widens where a write can land; it only saves the mkdir round-trip.
        fd = os.dup(self.root_fd)
        try:
            for part in parts:
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o770, dir_fd=fd)
                    if os.geteuid() == 0:
                        try:
                            dfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            try:
                                os.fchown(dfd, self.workspace_uid, self.workspace_gid)
                            finally:
                                os.close(dfd)
                        except OSError:
                            pass
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                st = os.fstat(nxt)
                if st.st_dev != self.root_dev:
                    os.close(nxt)
                    raise WorkspaceError("mount escape rejected")
                os.close(fd)
                fd = nxt
            return fd
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise WorkspaceError("symlink or non-directory path component rejected") from exc
            raise

    def _assert_fd_confined(self, fd: int) -> None:
        try:
            if os.path.exists("/proc/self/fd"):
                raw = os.readlink(f"/proc/self/fd/{fd}")
            else:
                raw = fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode()
            actual = Path(raw).resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise WorkspaceError("workspace directory identity is unavailable") from exc
        if actual != self.root and self.root not in actual.parents:
            raise WorkspaceError("workspace directory moved outside the root")

    def _assert_directory_fd(self, fd: int) -> None:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != self.root_dev:
            raise WorkspaceError("directory is outside the workspace device")
        self._assert_fd_confined(fd)

    @classmethod
    def _remove_tree_at(cls, parent_fd: int, name: str) -> None:
        """Remove only a no-follow directory tree created by this broker."""
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        try:
            for entry in tuple(os.scandir(fd)):
                if entry.is_dir(follow_symlinks=False):
                    cls._remove_tree_at(fd, entry.name)
                else:
                    os.unlink(entry.name, dir_fd=fd)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent_fd)

    @classmethod
    def _rename_noreplace(cls, source: str, destination: str, src_fd: int, dst_fd: int) -> None:
        renameat2 = getattr(cls._libc, "renameat2", None)
        renameatx_np = getattr(cls._libc, "renameatx_np", None)
        if renameat2 is None and renameatx_np is not None:
            # Darwin's RENAME_EXCL is the directory-safe equivalent of Linux
            # RENAME_NOREPLACE and keeps local validation faithful to prod.
            result = renameatx_np(
                src_fd, os.fsencode(source), dst_fd, os.fsencode(destination), 0x00000004,
            )
            if result == 0:
                return
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise WorkspaceError("destination already exists")
            if code not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
                raise OSError(code, os.strerror(code))
        if renameat2 is None:
            source_info = os.stat(source, dir_fd=src_fd, follow_symlinks=False)
            if stat.S_ISDIR(source_info.st_mode):
                raise WorkspaceError("safe no-replace directory rename is unavailable")
            try:
                os.link(source, destination, src_dir_fd=src_fd, dst_dir_fd=dst_fd,
                        follow_symlinks=False)
            except FileExistsError as exc:
                raise WorkspaceError("destination already exists") from exc
            os.unlink(source, dir_fd=src_fd)
            return
        result = renameat2(src_fd, os.fsencode(source), dst_fd, os.fsencode(destination), 1)
        if result != 0:
            code = ctypes.get_errno()
            if code == errno.EEXIST:
                raise WorkspaceError("destination already exists")
            raise OSError(code, os.strerror(code))

    def _open_file(self, parts: tuple[str, ...], flags=os.O_RDONLY):
        if not parts:
            raise WorkspaceError("file path required")
        parent = self._open_dir(parts[:-1])
        try:
            fd = os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=parent)
            st = os.fstat(fd)
            if st.st_dev != self.root_dev or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                os.close(fd)
                raise WorkspaceError("only single-link regular workspace files are allowed")
            return fd
        finally:
            os.close(parent)

    def _file_bytes(self, parts: tuple[str, ...], limit: int | None = None) -> bytes:
        fd = self._open_file(parts)
        try:
            limit = min(limit or self.max_file_bytes, self.max_file_bytes)
            st = os.fstat(fd)
            if st.st_size > limit:
                raise WorkspaceError("file exceeds operation limit")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > limit:
                raise WorkspaceError("file exceeds operation limit")
            return bytes(data)
        finally:
            os.close(fd)

    def list(self, path: str = ".", limit: int = 200) -> dict:
        parts = self._parts(path, allow_root=True)
        limit = max(1, min(int(limit), 1000))
        fd = self._open_dir(parts)
        try:
            entries = []
            for name in sorted(os.listdir(fd)):
                if name in self.RESERVED:
                    continue
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                kind = "symlink" if stat.S_ISLNK(st.st_mode) else "directory" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
                entries.append({"name": name, "type": kind, "size": st.st_size,
                                "modifiedNs": st.st_mtime_ns})
                if len(entries) >= limit:
                    break
            state = self.control.snapshot()
            return {"ok": True, "path": "/".join(parts) or ".", "entries": entries,
                    "truncated": len(os.listdir(fd)) > len(entries), **state}
        finally:
            os.close(fd)

    def describe(self, path: str) -> dict:
        parts = self._parts(path)
        parent = self._open_dir(parts[:-1])
        try:
            st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise WorkspaceError("symlink targets are not describable")
            result = {"ok": True, "path": "/".join(parts), "size": st.st_size,
                      "modifiedNs": st.st_mtime_ns, "mode": stat.S_IMODE(st.st_mode),
                      "type": "directory" if stat.S_ISDIR(st.st_mode) else "file"}
            if stat.S_ISREG(st.st_mode):
                result.update({"sha256": sha256_bytes(self._file_bytes(parts)),
                               "mimeType": mime_for(parts[-1])})
            result.update(self.control.snapshot())
            return result
        finally:
            os.close(parent)

    def read(self, path: str, offset: int = 0, length: int = MAX_TEXT_BYTES) -> dict:
        parts = self._parts(path)
        offset = max(0, int(offset))
        length = max(1, min(int(length), MAX_TEXT_BYTES))
        all_data = self._file_bytes(parts)
        chunk = all_data[offset:offset + length]
        try:
            text = chunk.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            import base64
            text = base64.b64encode(chunk).decode("ascii")
            encoding = "base64"
        return {"ok": True, "path": "/".join(parts), "content": text,
                "encoding": encoding, "offset": offset, "returnedBytes": len(chunk),
                "size": len(all_data), "sha256": sha256_bytes(all_data),
                "truncated": offset + len(chunk) < len(all_data), **self.control.snapshot()}

    def _existing_hash(self, parts: tuple[str, ...]) -> str | None:
        try:
            return sha256_bytes(self._file_bytes(parts))
        except FileNotFoundError:
            return None

    def _write_atomic(self, request: dict, content: bytes) -> dict:
        action_id, epoch, revision, idem = require_action_envelope(request)
        cached = self.idempotency.get(idem, request)
        if cached is not None:
            return cached
        if len(content) > self.max_file_bytes:
            raise WorkspaceError("content exceeds write limit")
        parts = self._parts(str(request.get("path", "")))
        # Auto-create missing parent dirs (confined) so write/upload works without a prior mkdir.
        parent = self._open_dir(parts[:-1], create=True)
        tmp_name = f".pairputer-tmp-{os.getpid()}-{time.time_ns()}"
        expected = request.get("expected_sha256")
        before = self._existing_hash(parts)
        if before is not None and not expected:
            os.close(parent)
            raise WorkspaceError("expected_sha256 is required when replacing a file")
        if expected is not None and before != expected:
            os.close(parent)
            raise WorkspaceError("expected_sha256 mismatch")
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            # Files created by the root-owned semantic broker remain usable by
            # the unprivileged desktop/job account that owns the workspace.
            # Deriving ownership from the already-open, trusted root keeps the
            # service portable in tests and avoids a name-based UID lookup.
            os.fchmod(fd, 0o660)
            if os.geteuid() == 0:
                os.fchown(fd, self.workspace_uid, self.workspace_gid)
            view = memoryview(content)
            while view:
                wrote = os.write(fd, view)
                view = view[wrote:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            with self.control.commit(epoch, revision) as state:
                # Recheck after preparation and while human input is excluded.
                current = self._existing_hash(parts)
                if current != before:
                    raise WorkspaceError("target changed during preparation")
                self._assert_fd_confined(parent)
                os.replace(tmp_name, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                try:
                    self._assert_fd_confined(parent)
                except WorkspaceError:
                    os.unlink(parts[-1], dir_fd=parent)
                    os.fsync(parent)
                    raise
                os.fsync(parent)
                after = sha256_bytes(content)
                result = action_result(
                    accepted=True, action_id=action_id, state=state, actuator="workspace.atomic_replace",
                    summary=f"atomically wrote {'/'.join(parts)}", data={"path": "/".join(parts)},
                    evidence_items=[evidence("file_hash", path="/".join(parts), beforeSha256=before or "",
                                             afterSha256=after, size=len(content), mimeType=mime_for(parts[-1]))])
            self.idempotency.put(idem, request, result)
            return result
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="workspace.atomic_replace", summary="write rejected",
                                 reason=exc.reason)
        finally:
            try:
                os.unlink(tmp_name, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)

    def write(self, request: dict) -> dict:
        content = request.get("content", "")
        encoding = request.get("encoding", "utf-8")
        if encoding == "utf-8":
            data = str(content).encode("utf-8")
        elif encoding == "base64":
            import base64
            data = base64.b64decode(str(content), validate=True)
        else:
            raise WorkspaceError("encoding must be utf-8 or base64")
        return self._write_atomic(request, data)

    def mkdir(self, request: dict) -> dict:
        """Create an app-owned directory suffix under one trusted parent.

        A missing nested suffix is assembled behind a root-only random name and
        then attached with a no-replace rename.  Human input is excluded for the
        bounded attach quantum, while every existing component is opened with
        ``O_NOFOLLOW`` and checked against the workspace device.
        """

        action_id, epoch, revision, idem = require_action_envelope(request)
        cached = self.idempotency.get(idem, request)
        if cached is not None:
            return cached
        parts = self._parts(str(request.get("path", "")))
        if len(parts) > self.MAX_DIRECTORY_DEPTH:
            raise WorkspaceError("directory path exceeds the depth limit")
        parents = request.get("parents", True)
        if not isinstance(parents, bool):
            raise WorkspaceError("parents must be boolean")

        parent_fd = os.dup(self.root_fd)
        missing_index: int | None = None
        try:
            for index, part in enumerate(parts):
                try:
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    missing_index = index
                    break
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise WorkspaceError(
                            "directory path contains a symlink or file collision"
                        ) from exc
                    raise
                try:
                    self._assert_directory_fd(next_fd)
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(parent_fd)
                parent_fd = next_fd

            if missing_index is None:
                try:
                    with self.control.commit(epoch, revision) as state:
                        self._assert_directory_fd(parent_fd)
                        observed_mode = f"{stat.S_IMODE(os.fstat(parent_fd).st_mode):04o}"
                        result = action_result(
                            accepted=True, action_id=action_id, state=state,
                            actuator="workspace.mkdir",
                            summary=f"directory already exists: {'/'.join(parts)}",
                            data={"path": "/".join(parts), "created": False,
                                  "createdDepth": 0, "mode": observed_mode},
                            evidence_items=[evidence(
                                "directory_created", path="/".join(parts),
                                created=False, createdDepth=0, mode=observed_mode,
                            )],
                        )
                except LeaseRejected as exc:
                    return action_result(
                        accepted=False, action_id=action_id, state=exc.state,
                        actuator="workspace.mkdir", summary="directory creation rejected",
                        reason=exc.reason,
                    )
                self.idempotency.put(idem, request, result)
                return result

            missing = parts[missing_index:]
            if not parents and len(missing) != 1:
                raise WorkspaceError("parent directory does not exist")
            temporary = f".pairputer-mkdir-{os.getpid()}-{uuid.uuid4().hex}"
            created_fds: list[int] = []
            renamed = False
            try:
                with self.control.commit(epoch, revision) as state:
                    self._assert_directory_fd(parent_fd)
                    try:
                        os.stat(missing[0], dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise WorkspaceError("directory target changed during preparation")

                    os.mkdir(temporary, mode=0o700, dir_fd=parent_fd)
                    current_fd = os.open(
                        temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    created_fds.append(current_fd)
                    for component in missing[1:]:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                        next_fd = os.open(
                            component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=current_fd,
                        )
                        self._assert_directory_fd(next_fd)
                        created_fds.append(next_fd)
                        current_fd = next_fd

                    # Nested children can receive final ownership while the
                    # root-only staging directory keeps the complete suffix
                    # inaccessible to the desktop account.
                    for directory_fd in created_fds[1:]:
                        os.fchmod(directory_fd, 0o770)
                        if os.geteuid() == 0:
                            os.fchown(directory_fd, self.workspace_uid, self.workspace_gid)
                        os.fsync(directory_fd)
                    for directory_fd in reversed(created_fds):
                        os.fsync(directory_fd)

                    self._rename_noreplace(
                        temporary, missing[0], parent_fd, parent_fd,
                    )
                    renamed = True
                    try:
                        attached_fd = os.open(
                            missing[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent_fd,
                        )
                        try:
                            if (os.fstat(attached_fd).st_ino != os.fstat(created_fds[0]).st_ino
                                    or os.fstat(attached_fd).st_dev != self.root_dev):
                                raise WorkspaceError("attached directory identity changed")
                            self._assert_directory_fd(parent_fd)
                            self._assert_directory_fd(attached_fd)
                            os.fchmod(attached_fd, 0o770)
                            if os.geteuid() == 0:
                                os.fchown(attached_fd, self.workspace_uid, self.workspace_gid)
                            os.fsync(attached_fd)
                            os.fsync(parent_fd)
                        finally:
                            os.close(attached_fd)
                    except BaseException:
                        # Keep a failed post-attach identity/ownership check from
                        # leaving an unjournaled directory visible.
                        try:
                            os.rename(
                                missing[0], temporary,
                                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                            )
                            renamed = False
                        except OSError:
                            pass
                        raise
                    result = action_result(
                        accepted=True, action_id=action_id, state=state,
                        actuator="workspace.mkdir",
                        summary=f"created directory {'/'.join(parts)}",
                        data={"path": "/".join(parts), "created": True,
                              "createdDepth": len(missing), "mode": "0770"},
                        evidence_items=[evidence(
                            "directory_created", path="/".join(parts),
                            created=True, createdDepth=len(missing), mode="0770",
                        )],
                    )
            except LeaseRejected as exc:
                return action_result(
                    accepted=False, action_id=action_id, state=exc.state,
                    actuator="workspace.mkdir", summary="directory creation rejected",
                    reason=exc.reason,
                )
            finally:
                for directory_fd in reversed(created_fds):
                    try:
                        os.close(directory_fd)
                    except OSError:
                        pass
                if not renamed:
                    self._remove_tree_at(parent_fd, temporary)
            self.idempotency.put(idem, request, result)
            return result
        finally:
            os.close(parent_fd)

    def upload(self, request: dict) -> dict:
        """Stage one verified binary chunk and atomically commit a complete upload."""

        action_id, epoch, revision, idem = require_action_envelope(request)
        cached = self.idempotency.get(idem, request)
        if cached is not None:
            return cached
        upload_id = str(request.get("upload_id") or "")
        data_path, metadata_path = self._upload_paths(upload_id)
        parts = self._parts(str(request.get("path", "")))
        path = "/".join(parts)
        try:
            # Protobuf JSON omits a scalar uint64 when its value is zero.  A
            # first upload chunk therefore arrives through the private gRPC
            # path without an explicit ``offset`` key even though the typed
            # request carried offset=0.  Zero is the only safe default; later
            # chunks remain bound to the staged size below.
            offset = int(request.get("offset", 0))
            total_size = int(request.get("total_size"))
        except (TypeError, ValueError) as exc:
            raise WorkspaceError("offset and total_size must be integers") from exc
        total_sha256 = str(request.get("total_sha256") or "").lower()
        chunk_sha256 = str(request.get("chunk_sha256") or "").lower()
        if (offset < 0 or total_size < 0 or total_size > self.max_file_bytes or
                not re.fullmatch(r"[0-9a-f]{64}", total_sha256) or
                not re.fullmatch(r"[0-9a-f]{64}", chunk_sha256)):
            raise WorkspaceError("invalid upload offset, size, or SHA-256")
        try:
            chunk = base64.b64decode(str(request.get("chunk_base64") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkspaceError("chunk_base64 is invalid") from exc
        if not chunk or len(chunk) > self.MAX_UPLOAD_CHUNK:
            raise WorkspaceError("upload chunk must contain at most 512 KiB")
        if sha256_bytes(chunk) != chunk_sha256:
            raise WorkspaceError("upload chunk SHA-256 mismatch")
        if offset + len(chunk) > total_size:
            raise WorkspaceError("upload chunk exceeds declared total_size")
        # "Just works": the caller need not pass final=true — an upload whose staged bytes
        # reach total_size IS complete, so auto-commit then. An explicit final=true still works
        # and forces the completeness check. (A short chunk that doesn't reach total_size stays
        # staged, exactly as before, so multi-chunk uploads are unaffected.)
        final = bool(request.get("final", False)) or (offset + len(chunk) >= total_size)
        expected_sha256 = request.get("expected_sha256")
        binding = {
            "uploadId": upload_id, "path": path, "totalSize": total_size,
            "totalSha256": total_sha256,
            "expectedSha256": str(expected_sha256) if expected_sha256 is not None else None,
        }
        with self._upload_lock:
            self._cleanup_uploads()
            state = self.control.snapshot()
            if int(epoch) != state["humanEpoch"]:
                raise WorkspaceError("human_epoch_changed")
            if int(revision) != state["worldRevision"]:
                raise WorkspaceError("world_revision_changed")
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise WorkspaceError("upload metadata is corrupt") from exc
                if {key: metadata.get(key) for key in binding} != binding:
                    raise WorkspaceError("upload_id is bound to a different destination or digest")
            else:
                if offset != 0:
                    raise WorkspaceError("first upload chunk must start at offset zero")
                metadata = {**binding, "createdAt": time.time(), "updatedAt": time.time()}
                fd = os.open(data_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                self._write_upload_metadata(metadata_path, metadata)
            try:
                current_size = data_path.stat(follow_symlinks=False).st_size
            except OSError as exc:
                raise WorkspaceError("upload staging object is unavailable") from exc
            if not stat.S_ISREG(data_path.stat(follow_symlinks=False).st_mode):
                raise WorkspaceError("upload staging object is not a regular file")
            if offset < current_size:
                if offset + len(chunk) > current_size:
                    raise WorkspaceError("upload chunk overlaps staged data")
                fd = os.open(data_path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.lseek(fd, offset, os.SEEK_SET)
                    existing = os.read(fd, len(chunk))
                finally:
                    os.close(fd)
                if existing != chunk:
                    raise WorkspaceError("upload replay content differs from staged data")
            elif offset > current_size:
                raise WorkspaceError("upload chunk offset is out of order")
            else:
                fd = os.open(data_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                try:
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                current_size += len(chunk)
            metadata["updatedAt"] = time.time()
            self._write_upload_metadata(metadata_path, metadata)
            if not final:
                receipt = self._upload_receipt(
                    action_id, state, upload_id=upload_id, offset=offset,
                    received=len(chunk), total=total_size, complete=False,
                )
                self.idempotency.put(idem, request, receipt)
                return receipt
            if current_size != total_size:
                raise WorkspaceError("final upload chunk does not complete total_size")
            if sha256_file(data_path, limit=self.max_file_bytes) != total_sha256:
                raise WorkspaceError("final upload SHA-256 mismatch")
            payload = data_path.read_bytes()
            final_request = dict(request)
            final_request["path"] = path
            # The final request's idempotency key binds the complete upload
            # metadata and chunk, while _write_atomic owns the sole world commit.
            result = self._write_atomic(final_request, payload)
            data_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            return result

    def patch(self, request: dict) -> dict:
        _, _, _, idem = require_action_envelope(request)
        cached = self.idempotency.get(idem, request)
        if cached is not None:
            return cached
        parts = self._parts(str(request.get("path", "")))
        data = self._file_bytes(parts)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("structured patch requires UTF-8 text") from exc
        hunks = request.get("hunks")
        if not isinstance(hunks, list) or not hunks or len(hunks) > 100:
            raise WorkspaceError("hunks must be a non-empty bounded list")
        for hunk in hunks:
            if set(hunk) - {"old", "new", "count"}:
                raise WorkspaceError("unknown patch hunk field")
            old, new = str(hunk.get("old", "")), str(hunk.get("new", ""))
            count = int(hunk.get("count", 1))
            if not old or count < 1 or count > 100 or text.count(old) != count:
                raise WorkspaceError("patch precondition failed")
            text = text.replace(old, new, count)
        patched = dict(request)
        return self._write_atomic(patched, text.encode("utf-8"))

    def move(self, request: dict) -> dict:
        action_id, epoch, revision, idem = require_action_envelope(request)
        cached = self.idempotency.get(idem, request)
        if cached is not None:
            return cached
        src = self._parts(str(request.get("source", "")))
        dst = self._parts(str(request.get("destination", "")))
        src_parent, dst_parent = self._open_dir(src[:-1]), self._open_dir(dst[:-1])
        try:
            before = self._existing_hash(src)
            if before is None or request.get("expected_sha256") != before:
                raise WorkspaceError("source expected_sha256 mismatch")
            try:
                os.stat(dst[-1], dir_fd=dst_parent, follow_symlinks=False)
                raise WorkspaceError("destination already exists")
            except FileNotFoundError:
                pass
            with self.control.commit(epoch, revision) as state:
                if self._existing_hash(src) != before:
                    raise WorkspaceError("source changed during preparation")
                self._assert_fd_confined(src_parent); self._assert_fd_confined(dst_parent)
                self._rename_noreplace(src[-1], dst[-1], src_parent, dst_parent)
                os.fsync(src_parent); os.fsync(dst_parent)
                result = action_result(accepted=True, action_id=action_id, state=state,
                    actuator="workspace.atomic_rename", summary=f"moved {'/'.join(src)} to {'/'.join(dst)}",
                    data={"source": "/".join(src), "destination": "/".join(dst)},
                    evidence_items=[evidence("file_move", source="/".join(src), destination="/".join(dst), sha256=before)])
            self.idempotency.put(idem, request, result)
            return result
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="workspace.atomic_rename", summary="move rejected", reason=exc.reason)
        finally:
            os.close(src_parent); os.close(dst_parent)

    def trash(self, request: dict) -> dict:
        source = self._parts(str(request.get("path", "")))
        trash_fd = None
        try:
            os.mkdir(".pairputer-trash", mode=0o700, dir_fd=self.root_fd)
        except FileExistsError:
            pass
        trash_fd = self._open_dir((".pairputer-trash",))
        # Reuse move semantics but keep the reserved destination private.
        destination = f"{time.time_ns()}-{uuid.uuid4().hex}-{source[-1]}"
        action_id, epoch, revision, idem = require_action_envelope(request)
        cached = self.idempotency.get(idem, request)
        if cached is not None:
            os.close(trash_fd)
            return cached
        parent = self._open_dir(source[:-1])
        try:
            before = self._existing_hash(source)
            if before is None or request.get("expected_sha256") != before:
                raise WorkspaceError("target expected_sha256 mismatch")
            with self.control.commit(epoch, revision) as state:
                if self._existing_hash(source) != before:
                    raise WorkspaceError("target changed during preparation")
                self._assert_fd_confined(parent); self._assert_fd_confined(trash_fd)
                self._rename_noreplace(source[-1], destination, parent, trash_fd)
                os.fsync(parent); os.fsync(trash_fd)
                result = action_result(accepted=True, action_id=action_id, state=state,
                    actuator="workspace.trash", summary=f"trashed {'/'.join(source)}",
                    data={"path": "/".join(source), "trashId": destination},
                    evidence_items=[evidence("file_trashed", path="/".join(source), sha256=before,
                                             reversible=True, trashId=destination)])
            self.idempotency.put(idem, request, result)
            return result
        except LeaseRejected as exc:
            return action_result(accepted=False, action_id=action_id, state=exc.state,
                                 actuator="workspace.trash", summary="trash rejected", reason=exc.reason)
        finally:
            os.close(parent); os.close(trash_fd)
