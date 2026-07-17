"""Cross-cartridge substrate contracts introduced for manifest v2 and co-play events."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "substrate/mcp-server/server.py").read_text()
APP = (ROOT / "substrate/mcp-server/app.html").read_text()
RELAY = (ROOT / "substrate/stateful-relay/index.mjs").read_text()
DESKTOP_VIDEO = (ROOT / "capsules/computer-use-desktop/rootfs/opt/capsule/video_ws.py").read_text()
LOCAL_DEV = (ROOT / "substrate/local-dev.sh").read_text()
DEPLOY_CAPSULE = (ROOT / "substrate/deploy-capsule.sh").read_text()
CAPSULE_STACK = (ROOT / "capsules/nested/capsule-stack.yaml").read_text()


class TestManifestV2Registration(unittest.TestCase):
    def test_current_fastmcp_registration_contract_and_hard_binding(self):
        """Exercise real server registration with a manager-shaped FastMCP stub.

        The stub intentionally exposes get_tool/parameters/output_schema the same way current FastMCP
        does, without making the repository test environment install the whole MCP/AWS dependency set.
        """
        code = r'''
import inspect, json, os, runpy, sys, types

for mod in ("boto3", "botocore", "botocore.exceptions", "boto3.dynamodb", "boto3.dynamodb.conditions"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["botocore.exceptions"].ClientError = Exception
class _Condition:
    def __init__(self, *a, **k): pass
sys.modules["boto3.dynamodb.conditions"].Attr = _Condition
sys.modules["boto3.dynamodb.conditions"].Key = _Condition
sys.modules["boto3"].client = lambda *a, **k: types.SimpleNamespace()
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(Table=lambda name: None)

class _Manager:
    def __init__(self): self.tools = {}
    def get_tool(self, name): return self.tools.get(name)
class _FakeMCP:
    def __init__(self, *a, **k): self._tool_manager = _Manager()
    def tool(self, *a, **k):
        def deco(fn):
            name = k.get("name") or fn.__name__
            self._tool_manager.tools[name] = types.SimpleNamespace(
                fn=fn, parameters={}, meta=k.get("meta"), description=k.get("description"))
            return fn
        return deco
    def resource(self, *a, **k): return lambda fn: fn
    def run(self, *a, **k): pass
fastmcp = types.ModuleType("mcp.server.fastmcp")
fastmcp.FastMCP = _FakeMCP
fastmcp.Context = object
types_mod = types.ModuleType("mcp.types")
class CallToolResult:
    def __init__(self, *a, **k):
        self.structuredContent = k.get("structuredContent")
        self.content = k.get("content") or (a[0] if a else [])
class TextContent:
    def __init__(self, *a, **k): self.type = "text"; self.text = k.get("text", "")
class ImageContent:
    def __init__(self, *a, **k):
        self.type = "image"; self.data = k.get("data", ""); self.mimeType = k.get("mimeType", "")
types_mod.CallToolResult = CallToolResult
types_mod.TextContent = TextContent
types_mod.ImageContent = ImageContent
sys.modules.update({"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
                    "mcp.server.fastmcp": fastmcp, "mcp.types": types_mod})

manifest = {"capsule": {"id": "typed-cap", "interaction": {"tier1": False},
  "bridge": {"protocol": "http-json", "port": 7123},
  "tools": [{"name": "drive", "path": "/drive", "effects": ["local_mutation"],
    "riskClass": "medium", "presentationModes": ["hybrid"],
    "inputSchema": {"type": "object", "properties": {
      "goal": {"type": "string"}, "limit": {"type": "integer"},
      "action_approval_token": {"type": "string"}}, "required": ["goal"],
      "additionalProperties": False},
    "outputSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
      "required": ["ok"]}}]}}
os.environ["PAIRPUTER_LOCAL_MODE"] = "1"
os.environ["PAIRPUTER_CAPSULE_MANIFEST"] = json.dumps(manifest)
os.environ["PAIRPUTER_IMAGE_REGISTRY"] = json.dumps({"typed-cap": {"arn": "local:typed-cap"}})
g = runpy.run_path(%r, run_name="not_main")
info = g["mcp"]._tool_manager.get_tool("typed_cap__drive")
assert info is not None
sig = inspect.signature(info.fn)
assert "goal" in sig.parameters and "limit" in sig.parameters
assert "action_approval_token" in sig.parameters
assert "args" in sig.parameters and "approval_token" in sig.parameters
assert "image_id" not in sig.parameters, sig
assert info.parameters["properties"]["goal"]["type"] == "string"
assert "args" in info.parameters["properties"]
assert info.__dict__["output_schema"]["required"] == ["ok"]
assert info.meta["pairputer/tool"]["effects"] == ["local_mutation"]

calls = []
fg = info.fn.__globals__
fg["_caller_identity"] = lambda ctx: "identity"
fg["_note_agent_action"] = lambda *a, **k: None
fg["_bridge"] = lambda identity, image, method, path, body, **kwargs: (
  calls.append((image, path, body)) or {"ok": True})
info.fn(None, goal="typed", limit=3, action_approval_token="brain-exact-token")
info.fn(None, args={"goal": "legacy"})
assert calls == [("typed-cap", "/drive", {"goal": "typed", "limit": 3,
                                            "action_approval_token": "brain-exact-token"}),
                 ("typed-cap", "/drive", {"goal": "legacy"})], calls
try:
    info.fn(None, goal="bad", image_id="other-cap")
except TypeError:
    pass
else:
    raise AssertionError("named tool accepted an image_id override")

bfg = g["_bridge_settings_for"].__globals__
bfg["_manifest_for"] = lambda image: {"bridge": {"protocol": "http-json", "port": 7444}}
assert g["_bridge_settings_for"]("typed-cap") == ("http-json", 7444)
bfg["_manifest_for"] = lambda image: {"bridge": {"protocol": "grpc", "port": 7444}}
try:
    g["_bridge_settings_for"]("typed-cap")
except ValueError:
    pass
else:
    raise AssertionError("unsupported bridge protocol was silently accepted")

# A capsule tool result carrying imageBase64 (e.g. screenshot) must surface an inline image
# content block so a host's computer-use loop can SEE the frame — not just a file path.
_b64 = "A"*80
res = g["_compact_agent_result"]({"accepted": True, "dataJson":
    json.dumps({"path":"/var/lib/x.png","mimeType":"image/png","imageBase64":_b64})})
kinds = [getattr(c, "type", None) for c in res.content]
assert "image" in kinds, "screenshot result must include an inline image content block"
img = next(c for c in res.content if getattr(c, "type", None) == "image")
assert img.mimeType == "image/png" and len(img.data) >= 80
# and the giant base64 must NOT be duplicated into the text/structured view
text = next(c for c in res.content if getattr(c, "type", None) == "text")
assert "A"*80 not in text.text, "base64 image bytes must not be duplicated into the text block"
# a result WITHOUT an image stays single text block (no regression)
plain = g["_compact_agent_result"]({"ok": True})
assert [getattr(c, "type", None) for c in plain.content] == ["text"]
print("IMAGE-CONTENT-OK")
print("MANIFEST-V2-OK")
''' % str(ROOT / "substrate/mcp-server/server.py")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("MANIFEST-V2-OK", result.stdout,
                      f"stdout={result.stdout!r}\nstderr={result.stderr[-2000:]!r}")

    def test_schema_and_metadata_fields_are_generic(self):
        for field in ("inputSchema", "outputSchema", "effects", "riskClass", "approvalPolicy",
                      "interruptibility", "idempotency", "presentationModes", "timeoutClass",
                      "capabilityScopes"):
            self.assertIn(field, SERVER)
        self.assertNotIn("target = image_id or bind_image", SERVER)
        self.assertIn("target = bind_image", SERVER)


class TestRuntimeAndLocalRegistry(unittest.TestCase):
    def test_manifest_runtime_memory_reaches_nested_stack(self):
        self.assertIn("minimumMemoryMiB", DEPLOY_CAPSULE)
        self.assertIn("--memory-mib", DEPLOY_CAPSULE)
        self.assertIn('"CapsuleMinMemoryMiB=${CAPSULE_MIN_MEMORY_MIB}"', DEPLOY_CAPSULE)
        self.assertIn("CapsuleMinMemoryMiB:", CAPSULE_STACK)
        self.assertIn("MinimumMemoryInMiB: !Ref CapsuleMinMemoryMiB", CAPSULE_STACK)

    def test_deploy_supports_versioned_image_name_without_changing_capsule_identity(self):
        self.assertIn("--image-name", DEPLOY_CAPSULE)
        self.assertIn('CAPSULE_IMAGE_NAME="${CAPSULE_IMAGE_NAME:-${CAPSULE_ID}}"', DEPLOY_CAPSULE)
        self.assertIn('"CapsuleImageName=${CAPSULE_IMAGE_NAME}"', DEPLOY_CAPSULE)
        self.assertIn('"CapsuleId=${CAPSULE_ID}"', DEPLOY_CAPSULE)

    def test_release_pointer_is_published_only_after_matching_image_succeeds(self):
        # Content-addressed manifest bytes may be staged early because they are undiscoverable. The
        # CloudFormation custom resource depends on LatestActiveImageVersion and commits current last.
        self.assertLess(DEPLOY_CAPSULE.index("put_immutable_parameter"),
                        DEPLOY_CAPSULE.index("aws cloudformation deploy"))
        self.assertIn("CapsuleReleasePublisher", CAPSULE_STACK)
        self.assertIn("!GetAtt CapsuleMicrovmImage.LatestActiveImageVersion", CAPSULE_STACK)
        self.assertIn("The one mutable write is last", CAPSULE_STACK)
        self.assertIn("rollback restores the previous release pointer", DEPLOY_CAPSULE)

    def test_local_registry_uses_selected_manifest(self):
        self.assertIn('CAPSULE_ID="$CAPSULE"', LOCAL_DEV)
        self.assertIn('c.get("id")', LOCAL_DEV)
        self.assertIn('c.get("name")', LOCAL_DEV)
        self.assertIn('c.get("description")', LOCAL_DEV)
        self.assertIn('"127.0.0.1:${BRIDGE_PORT}:${BRIDGE_PORT}"', LOCAL_DEV)
        self.assertNotIn('REGISTRY_JSON="{\\"doom\\"', LOCAL_DEV)

    def test_local_dev_fails_when_capsule_never_becomes_ready(self):
        self.assertIn("CAPSULE_READY=0", LOCAL_DEV)
        self.assertIn('if [[ "$CAPSULE_READY" != 1 ]]', LOCAL_DEV)
        self.assertIn("did not satisfy its readiness contract", LOCAL_DEV)


class TestGenericDisplayAndEvents(unittest.TestCase):
    def test_dynamic_display_transform_has_legacy_fallback(self):
        for token in ("updateDisplayMetadata", "updateVideoMetadata", "renderRect", "logicalPoint",
                      "displayRevision", "requestAnimationFrame", "prefers-reduced-motion",
                      "cursorPlan", "inputReceipt"):
            self.assertIn(token, APP)
        self.assertNotIn("cur.x / 320", APP)
        self.assertNotIn("aspect-ratio:8/5", APP)
        self.assertIn("state.agentCursor", APP)  # Agent DOOM fallback
        self.assertIn("/coplay", APP)            # polling fallback

    def test_relay_and_widget_support_sse_events(self):
        self.assertIn('reqUrl.pathname === "/events"', RELAY)
        self.assertIn('"content-type": "text/event-stream; charset=utf-8"', RELAY)
        self.assertIn("event: state", RELAY)
        self.assertIn("new EventSource(coplayUrl('/events'))", APP)
        self.assertIn("startCoplayPollFallback", APP)

    def test_large_desktop_frames_apply_backpressure_without_reconnecting(self):
        stream_handler = RELAY[RELAY.index("await requireRunning(claims);"):]
        self.assertIn("const writable = res.write", stream_handler)
        self.assertIn('res.once("drain", responseDrainListener)', stream_handler)
        self.assertIn("up?.pause()", stream_handler)
        self.assertIn("up?.resume()", stream_handler)
        self.assertNotIn("if (!res.write(`data:${payload", stream_handler)
        self.assertIn("_drain_diagnostics", DESKTOP_VIDEO)

    def test_player_recovers_when_an_open_stream_stops_delivering_frames(self):
        # Jitter fix (2026-07-12): stale threshold raised to 8s and the restart uses exponential
        # backoff so a marginal link settles instead of thrashing; a mid-stream onerror with bytes
        # already received auto-reconnects quietly rather than re-minting the token.
        self.assertIn("stale=lastFrameAt?gap>8000:gap>10000", RELAY)
        self.assertIn("window._lastStaleRestart", RELAY)
        self.assertIn("_staleRestarts", RELAY)  # backoff counter
        self.assertIn("if(bytes>0){tell('status',{s:'reconnecting…'});return;}", RELAY)  # quiet reconnect
        self.assertIn("stopStreams();streamRestartTimer=setTimeout", RELAY)
        self.assertIn("_supervise_stream", DESKTOP_VIDEO)
        self.assertIn("ws.wait_closed()", DESKTOP_VIDEO)


if __name__ == "__main__":
    unittest.main()
