import ast
import base64
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import types
import threading
import uuid
from decimal import Decimal

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "substrate/mcp-server/server.py").read_text()


def load_release_resolver(values, *, image_arn="arn:aws:lambda:us-east-1:1:microvm-image:fixture"):
    tree = ast.parse(SERVER)
    names = {"_decode_manifest_parameter", "_sha256_text", "_canonical_object_digest",
             "_release_for", "_expand_chunked_manifest"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "LOCAL_MODE": False,
        "MAX_CAPSULE_MANIFEST_BYTES": 1024 * 1024,
        "_MANIFEST_CHUNK_HEADER": "chunked:v1:",
        "_MAX_MANIFEST_PARTS": 16,
        "re": __import__("re"),
        "base64": base64,
        "gzip": gzip,
        "hashlib": hashlib,
        "io": __import__("io"),
        "json": json,
        "time": types.SimpleNamespace(time=lambda: 1000),
        "_release_cache": {},
        "_RELEASE_TTL_S": 30,
        "_effective_registry": lambda: {"fixture": {
            "arn": image_arn,
            "releaseSsm": "/pairputer/capsules/fixture/current",
        }},
        "_ssm_parameter_value": lambda name: values[name],
    }
    exec(compile(module, "server.py:release-resolver", "exec"), namespace)
    return namespace["_release_for"]


def fixture_release(chunked=False):
    manifest = json.dumps({"capsule": {"id": "fixture", "tools": []}}, separators=(",", ":"))
    parts = []
    if chunked:
        # A tiny chunk size proves reassembly across many parts without a giant fixture.
        parts = [manifest[i:i + 16] for i in range(0, len(manifest), 16)]
        primary = "chunked:v1:%d:%s" % (len(parts), hashlib.sha256(manifest.encode()).hexdigest())
    else:
        primary = manifest
    manifest_digest = "sha256:" + hashlib.sha256(primary.encode()).hexdigest()
    manifest_parameter = "/pairputer/capsules/fixture/manifests/sha256-" + manifest_digest.split(":", 1)[1]
    release = {
        "schemaVersion": 1,
        "capsuleId": "fixture",
        "imageArn": "arn:aws:lambda:us-east-1:1:microvm-image:fixture",
        "imageVersion": "42",
        "manifestParameter": manifest_parameter,
        "manifestDigest": manifest_digest,
        "contextSha256": "a" * 64,
        "contextUri": "s3://fixture/context.tar",
    }
    release_digest = "sha256:" + hashlib.sha256(json.dumps(
        release, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    release["releaseDigest"] = release_digest
    release_parameter = "/pairputer/capsules/fixture/releases/sha256-" + release_digest.split(":", 1)[1]
    pointer = {
        "schemaVersion": 1,
        "capsuleId": "fixture",
        "releaseParameter": release_parameter,
        "releaseDigest": release_digest,
    }
    values = {
        "/pairputer/capsules/fixture/current": json.dumps(pointer, sort_keys=True, separators=(",", ":")),
        release_parameter: json.dumps(release, sort_keys=True, separators=(",", ":")),
        manifest_parameter: primary,
    }
    for i, part in enumerate(parts):
        values[f"{manifest_parameter}/part{i}"] = part
    return values, release_parameter, manifest_parameter


def load_tool_compatibility():
    tree = ast.parse(SERVER)
    names = {
        "_sha256_text", "_canonical_object_digest", "_canonical_bridge_settings",
        "_manifest_tool_safety_contract", "_release_has_compatible_tool",
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "_MANIFEST_TOOL_METADATA_FIELDS": (
            "effects", "riskClass", "approvalPolicy", "interruptibility", "idempotency",
            "presentationModes", "timeoutClass", "capabilityScopes",
        ),
        "hashlib": hashlib,
        "hmac": __import__("hmac"),
        "json": json,
    }
    exec(compile(module, "server.py:tool-compatibility", "exec"), namespace)
    return namespace["_release_has_compatible_tool"]


def load_bound_bridge_call(current_bridge):
    tree = ast.parse(SERVER)
    names = {"_sha256_text", "_canonical_object_digest", "_bridge"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)

    class Response:
        status = 200

        @staticmethod
        def getheader(_name):
            return None

        @staticmethod
        def read(_limit):
            return b"{}"

    class Connection:
        def __init__(self, endpoint, port, timeout):
            self.endpoint, self.port, self.timeout = endpoint, port, timeout
            self.requests = []

        def request(self, *args, **kwargs):
            self.requests.append((args, kwargs))

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            return None

    connections = []

    def connect(endpoint, port, timeout):
        connection = Connection(endpoint, port, timeout)
        connections.append(connection)
        return connection

    namespace = {
        "CallerIdentity": object, "LOCAL_MODE": True, "LOCAL_BRIDGE_PORT": 6905,
        "_resolve_image_id": lambda image_id: image_id,
        "_agent_interact_for": lambda _image_id: True,
        "_discover_vm": lambda _identity, _image_id: ({}, {
            "state": "RUNNING", "id": "vm-1", "endpoint": "capsule.local",
            "bridge_capability": "x" * 32,
        }),
        "_require_session_release_current": lambda _item, _image_id: None,
        "_bridge_settings_for": lambda _image_id: (current_bridge["protocol"], current_bridge["port"]),
        "http": types.SimpleNamespace(client=types.SimpleNamespace(HTTPConnection=connect)),
        "log": types.SimpleNamespace(info=lambda *_args, **_kwargs: None),
        "BRIDGE_REQUEST_MAX_BYTES": 1024, "BRIDGE_RESPONSE_MAX_BYTES": 1024,
        "hashlib": hashlib, "hmac": hmac, "json": json, "os": os,
    }
    exec(compile(module, "server.py:bound-bridge-call", "exec"), namespace)
    return namespace["_bridge"], connections


def load_capsule_approve(preview):
    tree = ast.parse(SERVER)
    names = {"_sha256_text", "_approval_preview_digest", "capsule_approve"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    approve = next(node for node in functions if node.name == "capsule_approve")
    approve.decorator_list = []
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    identity = types.SimpleNamespace(tenant_id="tenant-a", client_id="client-a")
    item = {
        "status": "REQUESTED", "action_digest": "d" * 64,
        "expires_at": Decimal("2000"), "preview": preview,
    }
    approvals = {"approval-1": dict(item)}
    namespace = {
        "Context": object, "CallToolResult": dict, "LOCAL_MODE": True,
        "hosts": types.SimpleNamespace(native_approval_enforced_for_client_id=lambda _client: True),
        "_caller_identity": lambda _ctx: identity,
        "_load_exact_approval": lambda _identity, _approval_id: dict(item),
        "_approval_sign": lambda _payload: "signed-token",
        "_LOCAL_APPROVALS": approvals, "_LOCAL_APPROVAL_LOCK": threading.RLock(),
        "_now": lambda: 1000, "_compact_agent_result": lambda value: value,
        "hashlib": hashlib, "hmac": __import__("hmac"), "json": json, "uuid": uuid,
        "Decimal": Decimal, "Any": object,
    }
    exec(compile(module, "server.py:capsule-approve", "exec"), namespace)
    return namespace["capsule_approve"]


def load_advertised_input_schema():
    tree = ast.parse(SERVER)
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_advertised_input_schema")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"json": json}
    exec(compile(module, "server.py:advertised-input-schema", "exec"), namespace)
    return namespace["_advertised_input_schema"]


def load_local_approval_consumer(rows):
    tree = ast.parse(SERVER)
    names = {"_approval_parse", "_consume_exact_approval"}
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "CallerIdentity": object, "LOCAL_MODE": True,
        "_LOCAL_APPROVALS": rows, "_LOCAL_APPROVAL_LOCK": threading.RLock(),
        "_now": lambda: 1000, "hashlib": hashlib, "re": __import__("re"),
        "RuntimeError": RuntimeError,
    }
    exec(compile(module, "server.py:approval-consumer", "exec"), namespace)
    return namespace["_consume_exact_approval"]


def test_resolves_exact_immutable_release_and_manifest():
    values, _, _ = fixture_release()
    release = load_release_resolver(values)("fixture")
    assert release["imageVersion"] == "42"
    assert release["manifest"]["id"] == "fixture"
    assert release["releaseDigest"].startswith("sha256:")


def test_chunked_manifest_reassembles_through_the_same_release_binding():
    values, _, manifest_parameter = fixture_release(chunked=True)
    assert values[manifest_parameter].startswith("chunked:v1:")
    assert manifest_parameter + "/part1" in values  # genuinely multi-part
    release = load_release_resolver(values)("fixture")
    assert release["manifest"]["id"] == "fixture"


@pytest.mark.parametrize("tamper", ["part", "header_count", "header_sha", "missing_part"])
def test_chunked_manifest_fails_closed_on_any_part_tamper(tamper):
    values, _, manifest_parameter = fixture_release(chunked=True)
    if tamper == "part":
        values[manifest_parameter + "/part1"] += " "
    elif tamper == "header_count":
        # Fewer parts than staged: joined payload no longer matches the embedded sha.
        header = values[manifest_parameter]
        prefix, _, rest = header.removeprefix("chunked:v1:").partition(":")
        values[manifest_parameter] = "chunked:v1:%d:%s" % (int(prefix) - 1, rest)
    elif tamper == "header_sha":
        values[manifest_parameter] = values[manifest_parameter][:-1] + (
            "0" if values[manifest_parameter][-1] != "0" else "1")
    else:
        del values[manifest_parameter + "/part1"]
    with pytest.raises(Exception):
        load_release_resolver(values)("fixture")


def test_backward_compatible_args_wrapper_advertises_the_exact_inner_authority_schema():
    declared = {
        "type": "object", "required": ["goal", "allowed_domains"],
        "properties": {
            "goal": {"type": "string"},
            "allowed_domains": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }
    advertised = load_advertised_input_schema()(declared, [])
    wrapped = advertised["properties"]["args"]["anyOf"][0]
    assert wrapped == declared
    assert wrapped["additionalProperties"] is False
    assert "scope" not in wrapped["properties"]
    assert {"required": ["args"]} in advertised["anyOf"]


def test_copy_stable_approval_reference_is_exact_tenant_bound_and_single_use():
    approval_id = "approval_" + "a" * 32
    digest = "d" * 64
    rows = {approval_id: {
        "status": "GRANTED", "tenant_id": "tenant-a", "action_digest": digest,
        "expires_at": 2000, "token_digest": hashlib.sha256(approval_id.encode()).hexdigest(),
    }}
    consume = load_local_approval_consumer(rows)
    identity = types.SimpleNamespace(tenant_id="tenant-a")
    consume(identity, approval_id, digest)
    assert rows[approval_id]["status"] == "CONSUMED"
    with pytest.raises(RuntimeError, match="stale or already consumed"):
        consume(identity, approval_id, digest)

    rows[approval_id]["status"] = "GRANTED"
    with pytest.raises(RuntimeError, match="stale or already consumed"):
        consume(identity, approval_id, "e" * 64)
    rows[approval_id]["status"] = "GRANTED"
    with pytest.raises(RuntimeError, match="stale or already consumed"):
        consume(types.SimpleNamespace(tenant_id="tenant-b"), approval_id, digest)


def test_copy_stable_approval_consumer_has_its_runtime_regex_dependency():
    tree = ast.parse(SERVER)
    imported = {
        alias.asname or alias.name
        for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "re" in imported


def test_warm_named_tool_follows_only_an_identical_safety_contract():
    compatible = load_tool_compatibility()
    bound = {
        "name": "drive_task", "path": "/brain/drive", "label": "old label",
        "description": "old wording", "requiresApproval": True,
        "effects": ["local_mutation"], "riskClass": "local_reversible",
        "timeoutClass": "submission", "inputSchema": {
            "type": "object", "required": ["goal"],
            "properties": {"goal": {"type": "string"}}, "additionalProperties": False,
        },
    }
    current = {**bound, "label": "new label", "description": "better guidance"}
    bound_bridge = {"protocol": "http-json", "port": 6905}
    release = {"manifest": {
        "bridge": {"protocol": "http/json", "port": "6905"},
        "tools": [current], "safety": {"sensitivePatterns": ["secret"]},
    }}
    # Equivalent protocol spelling and integer formatting canonicalize to one transport identity.
    assert compatible(release, bound, ["secret"], bound_bridge) is True

    for changed in (
        {**current, "path": "/other"},
        {**current, "requiresApproval": False},
        {**current, "riskClass": "read_only"},
        {**current, "timeoutSeconds": 1},
        {**current, "inputSchema": {"type": "object", "additionalProperties": True}},
    ):
        assert compatible({"manifest": {"tools": [changed],
                                          "safety": {"sensitivePatterns": ["secret"]}}},
                          bound, ["secret"], bound_bridge) is False
    assert compatible({"manifest": {"tools": []}}, bound, ["secret"], bound_bridge) is False
    assert compatible({"manifest": {"tools": [current, current]}},
                      bound, ["secret"], bound_bridge) is False
    # A warm closure must never retain an older, weaker capsule-wide policy.
    assert compatible({"manifest": {"tools": [current],
                                     "safety": {"sensitivePatterns": ["secret", "purchase"]}}},
                      bound, ["secret"], bound_bridge) is False
    assert compatible({"manifest": {"tools": [current]}}, bound, ["secret"], bound_bridge) is False


@pytest.mark.parametrize("bridge", [
    {"protocol": "http-json", "port": 6906},
    {"protocol": "grpc", "port": 6905},
    {"protocol": "http-json", "port": 0},
])
def test_warm_named_tool_rejects_changed_or_invalid_bridge_binding(bridge):
    compatible = load_tool_compatibility()
    tool = {"name": "drive_task", "path": "/brain/drive", "requiresApproval": False}
    release = {"manifest": {
        "bridge": bridge, "tools": [tool], "safety": {"sensitivePatterns": []},
    }}
    assert compatible(
        release, tool, [], {"protocol": "http-json", "port": 6905},
    ) is False


def test_named_tool_bridge_call_revalidates_binding_immediately_before_connect():
    expected = {"protocol": "http-json", "port": 6905}
    changed_call, changed_connections = load_bound_bridge_call({"protocol": "http-json", "port": 6906})
    with pytest.raises(RuntimeError, match="bridge binding belongs to a superseded"):
        changed_call(object(), "fixture", "POST", "/drive", {}, expected_bridge=expected)
    assert changed_connections == []

    matching_call, matching_connections = load_bound_bridge_call(expected)
    assert matching_call(object(), "fixture", "POST", "/drive", {}, expected_bridge=expected) == {}
    assert len(matching_connections) == 1
    assert matching_connections[0].port == 6905


def test_exact_approval_requires_the_complete_human_preview():
    preview = {
        "tool": "computer_use_desktop__physical_input", "image_id": "computer-use-desktop",
        "reason": "manifest requires approval", "action_digest": "d" * 64,
        "args": {"events": [{"t": "m", "x": 400, "y": 80}]},
    }
    approve = load_capsule_approve(preview)
    with pytest.raises(PermissionError, match="exact displayed action"):
        approve(None, "approval-1", "d" * 64, {
            "tool": preview["tool"], "image_id": preview["image_id"],
            "action_digest": preview["action_digest"],
        })
    result = approve(None, "approval-1", "d" * 64, preview)
    assert result["approved"] is True and result["single_use"] is True
    assert result["approval_token"] == "approval-1"
    assert result["expires_at"] == 2000 and isinstance(result["expires_at"], int)


def test_exact_approval_canonicalizes_nested_dynamodb_decimals_without_losing_exactness():
    stored = {
        "tool": "computer_use_desktop__physical_input",
        "args": {
            "expected_human_epoch": Decimal("7"),
            "expected_world_revision": Decimal("19"),
            "events": [{"t": "m", "x": Decimal("400"), "y": Decimal("80")}],
            "calibration": {"scale": Decimal("1.25")},
        },
    }
    confirmed = {
        "tool": "computer_use_desktop__physical_input",
        "args": {
            "expected_human_epoch": 7,
            "expected_world_revision": 19,
            "events": [{"t": "m", "x": 400, "y": 80}],
            "calibration": {"scale": 1.25},
        },
    }
    approve = load_capsule_approve(stored)
    assert approve(None, "approval-1", "d" * 64, confirmed)["approved"] is True

    tampered = json.loads(json.dumps(confirmed))
    tampered["args"]["events"][0]["x"] = 401
    with pytest.raises(PermissionError, match="exact displayed action"):
        load_capsule_approve(stored)(None, "approval-1", "d" * 64, tampered)


@pytest.mark.parametrize("tamper", ["pointer", "release", "manifest", "image"])
def test_release_binding_fails_closed_on_any_split_brain(tamper):
    values, release_parameter, manifest_parameter = fixture_release()
    image_arn = "arn:aws:lambda:us-east-1:1:microvm-image:fixture"
    if tamper == "pointer":
        pointer = json.loads(values["/pairputer/capsules/fixture/current"])
        pointer["releaseDigest"] = "sha256:" + "0" * 64
        values["/pairputer/capsules/fixture/current"] = json.dumps(pointer, sort_keys=True, separators=(",", ":"))
    elif tamper == "release":
        release = json.loads(values[release_parameter])
        release["imageVersion"] = "43"
        values[release_parameter] = json.dumps(release, sort_keys=True, separators=(",", ":"))
    elif tamper == "manifest":
        values[manifest_parameter] += " "
    else:
        image_arn += "-other"
    with pytest.raises(RuntimeError):
        load_release_resolver(values, image_arn=image_arn)("fixture")


def load_image_resolver(registry, names):
    tree = ast.parse(SERVER)
    wanted = {"_resolve_image_id", "_default_image_id"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "_effective_registry": lambda: registry,
        "_capsule_name": lambda cid: names.get(cid, cid),
        "_NO_CAPSULES": "no capsules",
    }
    exec(compile(module, "server.py:image-resolver", "exec"), namespace)
    return namespace["_resolve_image_id"]


def test_image_resolution_never_guesses_silently_and_matches_what_humans_say():
    # Observed live 2026-07-11: "open the pairputer workbench" launched DOOM because an empty
    # image_id silently defaulted to the first capsule. Multiple capsules must force a choice.
    registry = {"agent-doom": {}, "computer-use-desktop": {}}
    names = {"agent-doom": "Agent DOOM (RESTful-DOOM)", "computer-use-desktop": "Pairputer Workbench"}
    resolve = load_image_resolver(registry, names)
    with pytest.raises(ValueError, match="multiple capsules"):
        resolve("")
    # Forgiving resolution: what humans/models actually say lands on the right capsule.
    assert resolve("workbench") == "computer-use-desktop"
    assert resolve("Pairputer Workbench") == "computer-use-desktop"
    assert resolve("doom") == "agent-doom"
    assert resolve("Agent-DOOM") == "agent-doom"
    # Ambiguity and misses still error with the list — never a guess.
    with pytest.raises(ValueError, match="available"):
        resolve("caps")  # no match
    ambiguous = load_image_resolver({"a-desk": {}, "b-desk": {}}, {})
    with pytest.raises(ValueError):
        ambiguous("desk")
    # A single-capsule deploy keeps the convenient default.
    solo = load_image_resolver({"agent-doom": {}}, names)
    assert solo("") == "agent-doom"


def load_heal_or_require(vm_lookup):
    """Extract _heal_or_require_release_current with a faked probe.

    ``vm_lookup(vm_id)`` returns a state string, or raises the exception to simulate the probe
    failing (a dict-shaped {"code": ...} exception is mapped through _client_error_code)."""
    tree = ast.parse(SERVER)
    names = {"_heal_or_require_release_current", "_session_release_matches"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    cleared = []

    def clear_vm(item, state="STOPPED"):
        cleared.append(dict(item))
        item = dict(item)
        item.pop("microvm_id", None)
        item["state"] = state
        return item

    trashed = []

    def trash(identity, image_id, relaunch=False):
        trashed.append(image_id)

    namespace = {
        "CallerIdentity": object,
        "LOCAL_MODE": False,
        "_SESSION_RELEASE_FIELDS": {"release_digest": "releaseDigest"},
        "mvm": types.SimpleNamespace(
            get_microvm=lambda microvmIdentifier: {"state": vm_lookup(microvmIdentifier)}),
        "_client_error_code": lambda exc: getattr(exc, "code", ""),
        "_clear_vm": clear_vm,
        "_trash_microvm": trash,
        "_load_session": lambda _identity, _image: {"state": "STOPPED"},
        "log": types.SimpleNamespace(info=lambda *a, **k: None),
    }
    exec(compile(module, "server.py:heal-or-require", "exec"), namespace)
    return namespace["_heal_or_require_release_current"], cleared, trashed


def test_stale_release_session_always_converges_never_errors():
    # The double-widget bug, three occurrences (2026-07-13/14): hosts render one widget card per
    # tool call, so ANY first play_capsule failure + the model's retry = a dead card next to a
    # live one. THE INVARIANT: an ensure-running call must NEVER fail because the session
    # predates a capsule redeploy — dead VM heals, LIVE VM migrates (trash + fresh launch), all
    # in the same call. No input may reach the "raise" path anymore.
    identity = types.SimpleNamespace(tenant_id="tenant-a")
    current = {"releaseDigest": "sha256:new"}
    stale = {"microvm_id": "vm-1", "release_digest": "sha256:old"}

    # Dead VM (TERMINATED) -> healed: record cleared, empty vm_id, no trash round-trip.
    heal, cleared, trashed = load_heal_or_require(lambda _id: "TERMINATED")
    item, vm_id = heal(identity, dict(stale), "img", current)
    assert vm_id == "" and "microvm_id" not in item and cleared and not trashed

    # Gone VM (not-found) -> healed the same way.
    def not_found(_id):
        exc = RuntimeError("gone"); exc.code = "ResourceNotFoundException"; raise exc
    heal, cleared, trashed = load_heal_or_require(not_found)
    _item, vm_id = heal(identity, dict(stale), "img", current)
    assert vm_id == "" and cleared and not trashed

    # LIVE stale VM (suspended OR running) -> MIGRATED: trashed via the full trash path (persist
    # barrier + terminate + record clear) and the caller falls through to a fresh launch. This was
    # the third double-widget variant: the explicit "Trash it" error is gone.
    for state in ("SUSPENDED", "RUNNING"):
        heal, cleared, trashed = load_heal_or_require(lambda _id, s=state: s)
        item, vm_id = heal(identity, dict(stale), "img", current)
        assert vm_id == "" and trashed == ["img"] and not cleared

    # Probe failure other than not-found -> presumed live -> migrate (the trash path itself fails
    # loudly if AWS is truly unreachable; nothing is silently discarded).
    def flaky(_id):
        raise RuntimeError("throttled")
    heal, cleared, trashed = load_heal_or_require(flaky)
    _item, vm_id = heal(identity, dict(stale), "img", current)
    assert vm_id == "" and trashed == ["img"]

    # Release-current session -> untouched, VM kept, no probe needed.
    heal, cleared, trashed = load_heal_or_require(lambda _id: (_ for _ in ()).throw(AssertionError("probed")))
    fresh = {"microvm_id": "vm-2", "release_digest": "sha256:new"}
    item, vm_id = heal(identity, dict(fresh), "img", current)
    assert vm_id == "vm-2" and item["microvm_id"] == "vm-2" and not cleared and not trashed

    # THE CLASS PIN: the legacy error string must be unreachable from the heal path — the only
    # remaining raiser is _require_session_release_current (bridge-binding checks), not ensure-running.
    heal_src = SERVER.split("def _heal_or_require_release_current", 1)[1].split("\ndef ", 1)[0]
    assert "raise RuntimeError" not in heal_src, "ensure-running must converge, never error on stale sessions"
