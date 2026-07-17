"""Durable per-tenant workspace (control-plane S3 sync): the fail-closed safety units.

The design keeps the MicroVM credential-free — AgentCore mirrors ONLY workspace/persistent/ to a
per-tenant S3 prefix through the bounded agent-bridge tools. These tests pin the two properties the
security model depends on: restore paths can never escape persistent/, and the S3 prefix is derived
from the JWT-derived tenant hash so one tenant can never address another's objects.
"""
import ast
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "substrate/mcp-server/server.py").read_text()


def load_persist_helpers():
    tree = ast.parse(SERVER)
    names = {"_persist_safe_relpath", "_persist_tenant_prefix"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"PERSIST_S3_PREFIX": "tenant-storage", "CallerIdentity": object}
    exec(compile(module, "server.py:persist-helpers", "exec"), namespace)
    return namespace


def test_restore_paths_cannot_escape_the_persistent_subtree():
    safe = load_persist_helpers()["_persist_safe_relpath"]
    assert safe("notes/todo.txt") == "notes/todo.txt"
    assert safe("./a//b/./c.bin") == "a/b/c.bin"  # normalized, still confined
    for hostile in ("../etc/passwd", "a/../../b", "/etc/shadow", "~/x", "a/..", "..",
                    "", "x" * 513, "a\x00b"):
        with pytest.raises(ValueError):
            safe(hostile)


def test_tenant_prefixes_are_disjoint_and_caller_underived():
    prefix = load_persist_helpers()["_persist_tenant_prefix"]
    a = prefix(types.SimpleNamespace(tenant_id="a" * 64), "computer-use-desktop")
    b = prefix(types.SimpleNamespace(tenant_id="b" * 64), "computer-use-desktop")
    assert a != b and a.startswith("tenant-storage/") and a.endswith("/computer-use-desktop/")
    # No tenant's prefix is a prefix of another's (S3 ListObjects can never leak across).
    assert not a.startswith(b) and not b.startswith(a)
    # Distinct capsules of one tenant are also disjoint.
    other = prefix(types.SimpleNamespace(tenant_id="a" * 64), "agent-doom")
    assert other != a and not a.startswith(other) and not other.startswith(a)


def load_persistent_storage_tool(fake_s3, live_running=False):
    tree = ast.parse(SERVER)
    names = {"persistent_storage", "_persist_safe_relpath", "_persist_tenant_prefix"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    tool = next(node for node in functions if node.name == "persistent_storage")
    tool.decorator_list = []
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    identity = types.SimpleNamespace(tenant_id="t" * 64)
    import base64 as b64
    import hashlib as hl
    namespace = {
        "Context": object, "CallerIdentity": object,
        "PERSIST_S3_PREFIX": "tenant-storage", "PERSIST_DIR": "persistent",
        "PERSIST_BUCKET": "test-bucket", "PERSIST_MAX_FILE_BYTES": 1024, "REGION": "us-east-1",
        "boto3": types.SimpleNamespace(client=lambda *_a, **_k: fake_s3),
        "_caller_identity": lambda _ctx: identity,
        "_resolve_image_id": lambda v: v or "capsule-x",
        "_persist_enabled": lambda _image: True,
        "_discover_vm": lambda *_a: ({}, {"state": "RUNNING" if live_running else "STOPPED",
                                          "id": "vm-1" if live_running else ""}),
        "_persist_bridge_upload": lambda *_a: True,
        "_persist_bridge_data": lambda *_a, **_k: {"sha256": "", "humanEpoch": 0, "worldRevision": 0},
        "_persist_mark_applied": lambda _s3, key, sha: fake_s3.applied.__setitem__(key, sha),
        "_persist_restore_async": lambda *_a: {"enabled": True, "started": True},
        "log": types.SimpleNamespace(warning=lambda *_a: None),
        "_client_error_code": lambda exc: getattr(exc, "code", ""),
        "base64": b64, "hashlib": hl, "uuid": __import__("uuid"),
    }
    exec(compile(module, "server.py:persistent-storage", "exec"), namespace)
    return namespace["persistent_storage"]


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.applied = {}  # key -> sha stamped by _persist_mark_applied

    def put_object(self, Bucket, Key, Body, Metadata=None):
        self.objects[Key] = Body
        self.metadata = getattr(self, "metadata", {})
        self.metadata[Key] = dict(Metadata or {})

    def get_object(self, Bucket, Key):
        import datetime, io
        if Key not in self.objects:
            exc = Exception("missing"); exc.code = "NoSuchKey"; raise exc
        return {"Body": io.BytesIO(self.objects[Key]),
                "LastModified": datetime.datetime(2026, 7, 11)}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            exc = Exception("missing"); exc.code = "404"; raise exc
        return {"Metadata": dict(getattr(self, "metadata", {}).get(Key) or {})}

    def copy_object(self, Bucket, Key, CopySource, Metadata=None, MetadataDirective=None):
        src = CopySource["Key"]
        if src not in self.objects:
            exc = Exception("missing"); exc.code = "NoSuchKey"; raise exc
        self.objects[Key] = self.objects[src]
        self.metadata = getattr(self, "metadata", {})
        self.metadata[Key] = dict(Metadata or {})

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):
        import datetime
        return {"Contents": [{"Key": k, "Size": len(v), "LastModified": datetime.datetime(2026, 7, 11)}
                             for k, v in sorted(self.objects.items()) if k.startswith(Prefix)]}


def test_persistent_storage_tool_is_tenant_scoped_and_roundtrips_without_a_vm():
    import base64 as b64
    s3 = FakeS3()
    # Another tenant's object must be invisible regardless of what the caller does.
    s3.objects["tenant-storage/" + "e" * 64 + "/capsule-x/secret.txt"] = b"other tenant"
    tool = load_persistent_storage_tool(s3, live_running=False)
    w = tool(None, "write", "capsule-x", "notes/a.txt", b64.b64encode(b"hello").decode())
    assert w["wroteSnapshot"] is True and w["wroteLiveVm"] is False and w["liveVmRunning"] is False
    assert ("tenant-storage/" + "t" * 64 + "/capsule-x/notes/a.txt") in s3.objects
    r = tool(None, "read", "capsule-x", "notes/a.txt")
    assert b64.b64decode(r["content_base64"]) == b"hello" and r["source"] == "snapshot"
    listing = tool(None, "list", "capsule-x")
    assert [e["path"] for e in listing["entries"]] == ["notes/a.txt"]  # other tenant invisible
    d = tool(None, "delete", "capsule-x", "notes/a.txt")
    assert d["deletedSnapshot"] is True
    assert tool(None, "list", "capsule-x")["count"] == 0
    # Traversal through the TOOL surface fails closed.
    with pytest.raises(ValueError):
        tool(None, "read", "capsule-x", "../" + "e" * 64 + "/capsule-x/secret.txt")
    with pytest.raises(ValueError):
        tool(None, "purge", "capsule-x")


def test_persistent_storage_write_converges_into_a_running_vm():
    import base64 as b64
    tool = load_persistent_storage_tool(FakeS3(), live_running=True)
    w = tool(None, "write", "capsule-x", "b.txt", b64.b64encode(b"x").decode())
    assert w["wroteSnapshot"] is True and w["wroteLiveVm"] is True and w["liveVmRunning"] is True


def test_export_is_hooked_before_suspend_and_terminate_and_restore_on_play():
    # Structural pins: the sync must ride freeze (before suspend), trash (before terminate),
    # and play (restore). A refactor that drops a hook fails here, not in production.
    assert SERVER.count("_persist_export(identity, image_id)") == 2  # freeze + trash
    assert "_persist_restore_async(identity, image_id)" in SERVER
    freeze_body = SERVER.split("def freeze(", 1)[1].split("\ndef ", 1)[0]
    assert "_persist_export" in freeze_body
    assert freeze_body.index("_persist_export") < freeze_body.index("suspend_microvm")
    trash_body = SERVER.split("def _trash_microvm(", 1)[1].split("\ndef ", 1)[0]
    assert trash_body.index("_persist_export") < trash_body.index("terminate_microvm")


def load_persist_export(fake_s3, vm_files):
    """Extract _persist_export + _persist_mark_applied over a fake S3 and a fake VM file set.

    ``vm_files`` is {relpath: bytes} — what _persist_walk/_persist_read_file report from the VM."""
    tree = ast.parse(SERVER)
    names = {"_persist_export", "_persist_mark_applied", "_persist_tenant_prefix"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "CallerIdentity": object,
        "PERSIST_S3_PREFIX": "tenant-storage", "PERSIST_BUCKET": "test-bucket",
        "PERSIST_MAX_TOTAL_BYTES": 1024 * 1024, "REGION": "us-east-1",
        "boto3": types.SimpleNamespace(client=lambda *_a, **_k: fake_s3),
        "_persist_enabled": lambda _image: True,
        "_persist_walk": lambda *_a: [{"relpath": rel, "size": len(body)}
                                      for rel, body in sorted(vm_files.items())],
        "_persist_read_file": lambda _i, _img, rel, _size: vm_files[rel],
        "log": types.SimpleNamespace(info=lambda *_a: None, warning=lambda *_a: None),
        "hashlib": __import__("hashlib"),
    }
    exec(compile(module, "server.py:persist-export", "exec"), namespace)
    return namespace["_persist_export"]


def test_export_mirror_delete_never_destroys_a_pending_upload():
    # Live-QA 2026-07-13 (wall #29): widget upload -> live push silently failed -> freeze.
    # The export mirror deleted the S3-only upload (the ONLY copy) because it wasn't on the VM.
    # Mirror-delete must be applied-gated: only content the VM demonstrably had may be dropped.
    import hashlib as hl
    identity = types.SimpleNamespace(tenant_id="t" * 64)
    prefix = "tenant-storage/" + hl.sha256(("t" * 64).encode()).hexdigest()[:0]  # placeholder, unused
    s3 = FakeS3()
    vm_files = {"kept.txt": b"vm content"}
    export = load_persist_export(s3, vm_files)
    # Derive the real prefix the code uses.
    tenant_prefix = load_persist_helpers()["_persist_tenant_prefix"](identity, "capsule-x")

    # A PENDING upload: in S3 (unapplied metadata), absent from the VM. Must SURVIVE the export.
    pending_key = tenant_prefix + "uploaded-while-push-failed.bin"
    s3.put_object("test-bucket", pending_key, b"precious", Metadata={"sha256": hl.sha256(b"precious").hexdigest()})

    # An APPLIED file the user then deleted inside the VM: absent from the VM, applied == sha.
    deleted_sha = hl.sha256(b"old").hexdigest()
    deleted_key = tenant_prefix + "deleted-in-vm.txt"
    s3.put_object("test-bucket", deleted_key, b"old",
                  Metadata={"sha256": deleted_sha, "applied-sha256": deleted_sha})

    result = export(identity, "capsule-x")
    assert result["ok"] is True and result["files"] == 1
    assert pending_key in s3.objects, "pending upload was mirror-deleted — data loss regression"
    assert deleted_key not in s3.objects, "in-VM delete must still propagate for applied content"
    # Content exported FROM the VM is stamped applied at put time.
    kept_meta = s3.metadata[tenant_prefix + "kept.txt"]
    assert kept_meta["applied-sha256"] == kept_meta["sha256"] == hl.sha256(b"vm content").hexdigest()


def test_persistent_storage_write_marks_applied_on_push_and_reschedules_on_failure():
    import base64 as b64
    import hashlib as hl
    # Successful live push -> object stamped applied (mirror-deletable later).
    s3 = FakeS3()
    tool = load_persistent_storage_tool(s3, live_running=True)
    w = tool(None, "write", "capsule-x", "b.txt", b64.b64encode(b"x").decode())
    assert w["wroteLiveVm"] is True
    key = next(k for k in s3.objects if k.endswith("/b.txt"))
    assert s3.applied.get(key) == hl.sha256(b"x").hexdigest()

    # Failed live push -> NOT stamped, and the reconcile-restore is scheduled (the widget's
    # "syncing into the running desktop…" message must be true).
    s3 = FakeS3()
    scheduled = []
    tool = load_persistent_storage_tool(s3, live_running=True)
    # rebind the fakes captured by the loader: force the push to fail and record the reschedule
    tool.__globals__["_persist_bridge_upload"] = lambda *_a: False
    tool.__globals__["_persist_restore_async"] = lambda *_a: scheduled.append(True)
    w = tool(None, "write", "capsule-x", "c.txt", b64.b64encode(b"y").decode())
    assert w["wroteLiveVm"] is False
    assert not s3.applied, "a failed push must not stamp applied"
    assert scheduled, "failed live push must schedule the reconcile-restore"
