import ast
from pathlib import Path
import types
import urllib.error
import urllib.request
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def section(text, start_marker, end_marker):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def load_drain_relay():
    source = read_text("substrate/mcp-server/server.py")
    tree = ast.parse(source)
    function = next(node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_drain_relay")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              function], type_ignores=[])
    ast.fix_missing_locations(module)
    logger = mock.Mock()
    namespace = {
        "CallerIdentity": object,
        "VIDEO_RELAY_URL": "https://relay.example.test",
        "RELAY_DRAIN_ATTEMPTS": 2,
        "RELAY_DRAIN_TIMEOUT_SECONDS": 5,
        "RELAY_DRAIN_MAX_RESPONSE_BYTES": 64 * 1024,
        # The extracted unit tests exercise transport behavior with a minimal
        # synthetic VM; release-binding cleanup is covered by the integration
        # tests below and by the production session schema.
        "_SESSION_RELEASE_FIELDS": (),
        "SESSION_TOKEN_TTL_SECONDS": 900,
        "_relay_token": lambda identity, vm, exp=None: "redacted-token",
        "_cloudfront_signed_params": lambda exp: "",
        "time": types.SimpleNamespace(time=lambda: 1_700_000_000, sleep=mock.Mock()),
        "urllib": types.SimpleNamespace(error=urllib.error, parse=__import__("urllib.parse", fromlist=["urlencode"]),
                                         request=urllib.request),
        "log": logger,
    }
    exec(compile(module, "server.py:_drain_relay", "exec"), namespace)
    return namespace["_drain_relay"], namespace, logger


def load_server_function(name, namespace):
    tree = ast.parse(read_text("substrate/mcp-server/server.py"))
    function = next(node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    function.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, f"server.py:{name}", "exec"), namespace)
    return namespace[name]


class FakeDrainResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self.body = body

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class RelaySessionFreshnessTests(unittest.TestCase):
    def test_authorize_checks_durable_session_before_accepting_claims(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        authorize = section(relay, "async function authorize(reqUrl, channel)", "async function handleHttp")

        self.assertIn("TransactGetItemsCommand", relay)
        self.assertIn("async function loadActiveSession(claims)", relay)
        self.assertIn("function sessionClaimsFresh(claims, item)", relay)
        self.assertIn("current = await loadActiveSessionCoalesced(claims);", authorize)
        self.assertIn("if (!sessionClaimsFresh(claims, current)) return null;", authorize)

        channel_index = authorize.index("if (!claims || !hasChannel(claims, channel)) return null;")
        load_index = authorize.index("current = await loadActiveSessionCoalesced(claims);")
        return_index = authorize.index("return claims;")
        self.assertLess(channel_index, load_index)
        self.assertLess(load_index, return_index)

    def test_freshness_check_compares_rotated_session_identity(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        freshness = section(relay, "function sessionClaimsFresh(claims, item)", "function json(res")

        self.assertIn("ddbAttrString(item.tenant_id) === claims.tenantId", freshness)
        self.assertIn("ddbAttrString(item.image_id) === claims.imageId", freshness)
        self.assertIn("ddbAttrString(item.microvm_id) === claims.microvmId", freshness)
        self.assertIn("ddbAttrString(item.session_id) === claims.sessionId", freshness)
        self.assertIn("currentSessionVersion === claims.sessionVersion", freshness)
        self.assertIn("item.release_digest) === claims.releaseDigest", freshness)
        self.assertIn("item.manifest_digest) === claims.manifestDigest", freshness)
        self.assertIn("item.image_arn) === claims.imageArn", freshness)
        self.assertIn("item.image_version) === claims.imageVersion", freshness)

    def test_session_lookup_is_consistent_and_fails_closed_without_table(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        lookup = section(relay, "async function loadActiveSession(claims)", "function sessionClaimsFresh")

        self.assertIn("if (!SESSION_TABLE) return null;", lookup)
        self.assertIn("TransactGetItemsCommand", lookup)
        self.assertIn("pk: { S: `TENANT#${claims.tenantId}` }", lookup)
        self.assertIn("sk: { S: `IMAGE#${claims.imageId}` }", lookup)
        self.assertIn("pk: { S: `MICROVM#${claims.microvmId}` }", lookup)
        self.assertIn('sk: { S: "OWNER" }', lookup)
        self.assertIn("owner.tenant_id", lookup)
        self.assertIn("owner.release_digest", lookup)
        self.assertIn("owner.manifest_digest", lookup)
        self.assertIn("owner.image_arn", lookup)
        self.assertIn("owner.image_version", lookup)
        self.assertNotIn("if (!SESSION_TABLE) return claims", relay)

    def test_authorize_refuses_a_suspending_session_so_reconnects_cannot_auto_resume(self):
        # A browser EventSource auto-reconnects during freeze; opening an upstream to the VM
        # auto-resumes it (idlePolicy.autoResumeEnabled) and defeats the suspend. authorize() must
        # reject on the AUTHORITATIVE session state (SUSPENDING/SUSPENDED/frozen), read from DynamoDB —
        # not the possibly-stale token claim — so freeze wins the race deterministically.
        relay = read_text("substrate/stateful-relay/index.mjs")
        authorize = section(relay, "async function authorize(reqUrl, channel)", "async function handleHttp")
        self.assertIn("function sessionSuspending(item)", relay)
        # The guard is scoped to STREAM channels only — blocking "control" (/drain) would make freeze's
        # own drain fail closed (freeze sets state=SUSPENDING in DynamoDB BEFORE it calls /drain).
        self.assertIn('const STREAM_CHANNELS = new Set(["video", "audio", "input", "player"]);', relay)
        self.assertIn("if (STREAM_CHANNELS.has(channel) && sessionSuspending(current)) return null;", authorize)
        # the guard runs AFTER freshness (so we've confirmed the row is this session) and before accept
        fresh_index = authorize.index("if (!sessionClaimsFresh(claims, current)) return null;")
        guard_index = authorize.index("if (STREAM_CHANNELS.has(channel) && sessionSuspending(current)) return null;")
        return_index = authorize.index("return claims;")
        self.assertLess(fresh_index, guard_index)
        self.assertLess(guard_index, return_index)
        # sessionSuspending keys off state + frozen, the exact fields the control plane writes pre-suspend
        suspending = section(relay, "function sessionSuspending(item)", "function json(res")
        self.assertIn('=== "SUSPENDING"', suspending)
        self.assertIn('=== "SUSPENDED"', suspending)
        self.assertIn("ddbAttrBool(item.frozen)", suspending)

    def test_all_relay_control_and_data_paths_still_use_authorize(self):
        relay = read_text("substrate/stateful-relay/index.mjs")

        self.assertIn('const claims = await authorize(reqUrl, "input");', relay)
        self.assertIn('const claims = await authorize(reqUrl, "state");', relay)
        self.assertIn("const claims = await authorize(reqUrl, channel);", relay)

    def test_anonymous_or_malformed_viewers_cannot_reach_relay_data_or_control(self):
        relay = read_text("substrate/stateful-relay/index.mjs")
        input_handler = section(relay, "async function handleInputPost", "async function drainSession")
        events_handler = section(relay, "async function handleEvents", "function playerHtml")
        http_handler = relay[relay.index("async function handleHttp(req, res)"):]

        self.assertLess(input_handler.index('authorize(reqUrl, "input")'), input_handler.index("claimViewer(sess"))
        self.assertLess(events_handler.index('authorize(reqUrl, "state")'), events_handler.index("claimViewer(sess"))
        self.assertLess(http_handler.index("const claims = await authorize(reqUrl, channel)"),
                        http_handler.index('if (channel === "player")'))
        self.assertLess(http_handler.index("const claims = await authorize(reqUrl, channel)"),
                        http_handler.index('if (channel === "control")'))
        self.assertIn('text(res, 403, "forbidden: valid relay session required")', input_handler)
        self.assertIn('if (!claims) { text(res, 403, "forbidden"); return; }', events_handler)
        self.assertIn('if (requestViewer.present && !requestViewer.valid)', http_handler)
        self.assertIn('text(res, 400, "invalid viewer id")', http_handler)
        for route in ('"/player"', '"/audio"', '"/drain"'):
            self.assertIn(route, http_handler)
        self.assertIn(': "video";', http_handler)


class RelayDrainLifecycleTests(unittest.TestCase):
    def test_freeze_epoch_conflict_never_suspends_vm(self):
        item = {"pk": "TENANT#t", "sk": "IMAGE#c", "microvm_id": "vm-1",
                "endpoint": "https://vm", "session_id": "old", "session_version": 1}
        mvm = mock.Mock()
        namespace = {
            "Context": object, "CallToolResult": object,
            "_caller_identity": lambda _ctx: types.SimpleNamespace(tenant_id="tenant"),
            "_resolve_image_id": lambda image: image or "capsule",
            "_resolve_recovery_image_id": lambda _identity, image: image or "capsule",
            "_discover_vm": lambda *_args: (dict(item), {"id": "vm-1"}),
            "_acquire_session_lease": lambda _item: "lease",
            "_load_session": lambda *_args: dict(item),
            "_require_session_release_current": mock.Mock(),
            "_rotate_bound_session_epoch": mock.Mock(side_effect=RuntimeError("epoch conflict")),
            "_release_session_lease": mock.Mock(),
            "_vm_from_item": mock.Mock(), "_capsule_lifecycle_hook": mock.Mock(),
            "_persist_export": lambda *_args: {"enabled": False},
            "_drain_relay": mock.Mock(), "_scale_relay_to_zero_if_idle": lambda: "always_on",
            "_widget_result": mock.Mock(), "mvm": mvm, "time": types.SimpleNamespace(sleep=lambda _: None),
            "log": mock.Mock(), "RELAY_WARM_SECONDS": -1,
        }
        freeze = load_server_function("freeze", namespace)
        with self.assertRaisesRegex(RuntimeError, "epoch conflict"):
            freeze(object(), "capsule")
        mvm.suspend_microvm.assert_not_called()
        namespace["_drain_relay"].assert_not_called()
        namespace["_release_session_lease"].assert_called_once()

    def test_freeze_reloads_after_lifecycle_hook_advances_record_version(self):
        durable = {"pk": "TENANT#t", "sk": "IMAGE#c", "microvm_id": "vm-1",
                   "endpoint": "https://vm", "session_id": "old", "session_version": 1,
                   "record_version": 10, "state": "RUNNING"}

        def load(*_args):
            return dict(durable)

        def acquire(_item):
            durable["record_version"] += 1
            durable["lease_owner"] = "lease"
            return "lease"

        def rotate(value):
            self.assertEqual(value["record_version"], durable["record_version"])
            value["session_id"] = "rotated"
            value["session_version"] += 1
            value["record_version"] += 1
            durable.update(value)
            return value

        def lifecycle(*_args):
            # _bridge -> _discover_vm performs an independent optimistic save.
            durable["record_version"] += 1
            durable["state"] = "RUNNING"
            return {"ok": True}

        def save(value):
            self.assertEqual(value["record_version"], durable["record_version"],
                             "freeze attempted to save the pre-hook stale item")
            value["record_version"] += 1
            durable.update(value)
            return value

        mvm = mock.Mock()
        mvm.get_microvm.return_value = {"state": "SUSPENDED"}
        namespace = {
            "Context": object, "CallToolResult": object,
            "_caller_identity": lambda _ctx: types.SimpleNamespace(tenant_id="tenant"),
            "_resolve_image_id": lambda image: image or "capsule",
            "_resolve_recovery_image_id": lambda _identity, image: image or "capsule",
            "_discover_vm": lambda *_args: (dict(durable), {"id": "vm-1"}),
            "_acquire_session_lease": acquire, "_load_session": load,
            "_require_session_release_current": mock.Mock(),
            "_rotate_bound_session_epoch": rotate,
            "_session_version": lambda value: int(value.get("session_version") or 1),
            "_release_session_lease": mock.Mock(),
            "_vm_from_item": lambda value, **kwargs: {"id": value["microvm_id"], **kwargs},
            "_capsule_lifecycle_hook": lifecycle, "_drain_relay": mock.Mock(),
            "_persist_export": lambda *_args: {"enabled": False},
            "_scale_relay_to_zero_if_idle": lambda: "always_on", "_save_session": save,
            "_widget_result": lambda value, **_kwargs: value,
            "mvm": mvm, "time": types.SimpleNamespace(sleep=lambda _: None),
            "log": mock.Mock(), "RELAY_WARM_SECONDS": -1,
            "SessionConflict": RuntimeError,
        }
        freeze = load_server_function("freeze", namespace)
        result = freeze(object(), "capsule")
        self.assertEqual(result["state"], "SUSPENDED")
        self.assertEqual(durable["state"], "SUSPENDED")
        self.assertEqual(durable["session_id"], "rotated")
        mvm.suspend_microvm.assert_called()
        namespace["_drain_relay"].assert_called_once()

    def test_resume_epoch_conflict_never_resumes_vm(self):
        item = {"pk": "TENANT#t", "sk": "IMAGE#c", "microvm_id": "vm-1",
                "endpoint": "https://vm", "session_id": "old", "session_version": 1,
                "image_arn": "arn:image", "image_version": "7"}
        mvm = mock.Mock()
        mvm.get_microvm.return_value = {
            "state": "SUSPENDED", "endpoint": "https://vm",
            "imageArn": "arn:image", "imageVersion": "7",
        }
        namespace = {
            "CallerIdentity": object,
            "LOCAL_MODE": False,
            "_resolve_image_id": lambda image: image or "capsule",
            "_resolve_recovery_image_id": lambda _identity, image: image or "capsule",
            "_release_for": lambda _image: {"imageVersion": "7"},
            "_image_arn": lambda _image: "arn:image",
            "_load_session": lambda *_args: dict(item),
            "_require_session_release_current": mock.Mock(),
            # pass-through: release matches in this fixture, so no heal and no probe
            "_heal_or_require_release_current": lambda _identity, healed_item, _image, _release: (
                healed_item, str(healed_item.get("microvm_id") or "")),
            "_acquire_session_lease": lambda _item: "lease",
            "_rotate_bound_session_epoch": mock.Mock(side_effect=RuntimeError("epoch conflict")),
            "_release_session_lease": mock.Mock(),
            "_client_error_code": lambda _exc: "",
            "_vm_from_item": mock.Mock(), "_save_session": mock.Mock(), "_clear_vm": mock.Mock(),
            "_apply_release_binding": mock.Mock(), "_session_version": lambda value: value.get("session_version", 1),
            "_session_release_matches": lambda *_args: True, "_capsule_run_role": lambda _image: "",
            "_bind_new_vm_owner": mock.Mock(), "_local_vm": mock.Mock(),
            "mvm": mvm, "time": types.SimpleNamespace(sleep=lambda _: None), "log": mock.Mock(),
            "INGRESS_NETWORK_CONNECTORS": [], "EGRESS_NETWORK_CONNECTORS": [],
            "MICROVM_MAX_IDLE_SECONDS": 1, "MICROVM_SUSPENDED_DURATION_SECONDS": 1,
            "MICROVM_MAX_DURATION_SECONDS": 10, "secrets": __import__("secrets"),
            "uuid": __import__("uuid"), "json": __import__("json"), "ClientError": Exception,
            "hmac": __import__("hmac"),
        }
        # the real ownership assertion (item has no tenant_id here -> passes; exercises the wiring)
        namespace["_assert_owns"] = load_server_function("_assert_owns", namespace)
        ensure = load_server_function("_ensure_running", namespace)
        with self.assertRaisesRegex(RuntimeError, "epoch conflict"):
            ensure(types.SimpleNamespace(tenant_id="tenant"), "capsule")
        mvm.resume_microvm.assert_not_called()
        namespace["_release_session_lease"].assert_called_once()

    def test_required_drain_retries_transport_failure_then_fails_closed_without_leaking_url(self):
        drain, namespace, _logger = load_drain_relay()
        failure = urllib.error.URLError("sensitive transport detail")
        with mock.patch.object(urllib.request, "urlopen", side_effect=failure) as urlopen:
            with self.assertRaisesRegex(RuntimeError, r"failed closed.*transport") as raised:
                drain(object(), {"id": "vm-1"})

        self.assertEqual(urlopen.call_count, 2)
        namespace["time"].sleep.assert_called_once_with(0.1)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertNotIn("redacted-token", str(raised.exception))

        namespace["VIDEO_RELAY_URL"] = ""
        with self.assertRaisesRegex(RuntimeError, "not_configured"):
            drain(object(), {"id": "vm-1"})
        self.assertFalse(drain(object(), {"id": "vm-1"}, required=False))

    def test_non_2xx_drain_fails_closed_but_safe_termination_remains_best_effort(self):
        drain, namespace, logger = load_drain_relay()
        with mock.patch.object(urllib.request, "urlopen", return_value=FakeDrainResponse(status=502)) as urlopen:
            with self.assertRaisesRegex(RuntimeError, r"http_502"):
                drain(object(), {"id": "vm-1"})
        self.assertEqual(urlopen.call_count, 2)

        namespace["time"].sleep.reset_mock()
        with mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError()) as urlopen:
            self.assertFalse(drain(object(), {"id": "vm-1"}, required=False))
        self.assertEqual(urlopen.call_count, 2)
        logger.warning.assert_called_once()
        self.assertNotIn("redacted-token", str(logger.warning.call_args))

    def test_legacy_vm_cleanup_skips_tokenized_drain(self):
        drain, namespace, _logger = load_drain_relay()
        namespace["_SESSION_RELEASE_FIELDS"] = ("release_digest", "manifest_digest", "image_arn", "image_version")
        with mock.patch.object(urllib.request, "urlopen") as urlopen:
            self.assertFalse(drain(object(), {"id": "legacy-vm"}, required=False))
        urlopen.assert_not_called()

    def test_successful_drain_is_bounded_and_freeze_waits_for_it_before_suspend(self):
        drain, _namespace, _logger = load_drain_relay()
        with mock.patch.object(urllib.request, "urlopen", return_value=FakeDrainResponse()) as urlopen:
            self.assertTrue(drain(object(), {"id": "vm-1"}))
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)

        server = read_text("substrate/mcp-server/server.py")
        freeze = section(server, "def freeze(ctx:", "def thaw(ctx:")
        trash = section(server, "def _trash_microvm", "def play_capsule")
        self.assertLess(freeze.index("_rotate_bound_session_epoch(item)"),
                        freeze.index("mvm.suspend_microvm"))
        self.assertLess(freeze.index("_drain_relay(identity, vm)"), freeze.index("mvm.suspend_microvm"))
        self.assertIn("_drain_relay(identity, vm, required=False)", trash)

    def test_resume_rotates_epoch_before_waking_suspended_vm(self):
        server = read_text("substrate/mcp-server/server.py")
        ensure = section(server, "def _ensure_running", "def _vm_state")
        suspended = ensure[ensure.index('if st == "SUSPENDED":'):]
        self.assertLess(suspended.index("_acquire_session_lease(item)"),
                        suspended.index("_rotate_bound_session_epoch(latest)"))
        self.assertLess(suspended.index("_rotate_bound_session_epoch(latest)"),
                        suspended.index("mvm.resume_microvm"))
        self.assertIn("A failed epoch transaction therefore leaves the VM safely", suspended)

    def test_scale_to_zero_is_fail_safe_never_stops_on_a_stale_read(self):
        # Scale-to-zero is now REAL (RelayWarmSeconds 0/N>0), but the multi-tenant safety objection is
        # honored structurally: the relay is stopped ONLY on a SUCCESSFUL count read of EXACTLY 0. Any
        # failure or non-zero leaves it warm, and -1 stays always-on. This pins that invariant on both
        # the control plane (server.py) and the data plane (index.mjs) so a stale/eventually-consistent
        # read can never kill a live session.
        server = read_text("substrate/mcp-server/server.py")
        control = section(server, "def _scale_relay_to_zero_if_idle", "def _active_relay_session_count")
        self.assertIn('return "always_on"', control)            # -1 path
        self.assertIn("_active_relay_session_count()", control)  # genuinely reads the count now
        self.assertIn("if active != 0:", control)               # only proceeds on exactly 0
        self.assertIn("count_read_failed", control)             # a failed read leaves it warm
        self.assertIn("_scale_relay(0)", control)

        relay = read_text("substrate/stateful-relay/index.mjs")
        scale = section(relay, "async function scaleRelayToZeroIfIdle", "function scheduleRelayScaleDown")
        self.assertIn("activeRelaySessionCount()", scale)       # reads the count
        self.assertIn("active !== 0", scale)                    # only scales on exactly 0
        self.assertIn("count_read_failed", scale)               # failed read -> stay warm (fail-safe)
        self.assertIn("RELAY_WARM_SECONDS < 0", scale)          # -1 stays always-on
        self.assertIn("await scaleRelayToZero()", scale)


if __name__ == "__main__":
    unittest.main()
