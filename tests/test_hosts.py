"""Multi-host MCP layer: host profiles, per-host reconnect UX, resource/meta regression locks.

One server + one widget serve Codex, ChatGPT (web/desktop), and Claude; the ONLY per-host variance is
a HostProfile (substrate/mcp-server/hosts/). These tests lock:
  1. the Codex resource URI + mime are byte-identical to the shipped values (the cached tool->resource
     binding wall — server.py documents why they must NEVER change);
  2. host resolution by Cognito client_id via PAIRPUTER_HOST_CLIENT_MAP, defaulting to codex;
  3. hosts/ carries ZERO capsule knowledge (hard platform rule);
  4. the widget prefers payload-delivered reconnect strings over its Codex defaults;
  5. tool meta carries BOTH dialects: openai/outputTemplate + the nested _meta.ui.resourceUri
     (SEP-1865) — one resource (text/html;profile=mcp-app) serves Codex, ChatGPT, and Claude.

Hermetic: stdlib only, no AWS, no network (same rules as the rest of tests/).
"""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "substrate" / "mcp-server"
SERVER = (MCP_DIR / "server.py").read_text()
APP = (MCP_DIR / "app.html").read_text()
RELAY = (REPO_ROOT / "substrate" / "stateful-relay" / "index.mjs").read_text()
HOSTS_DIR = MCP_DIR / "hosts"

sys.path.insert(0, str(MCP_DIR))
import hosts  # noqa: E402
importlib.reload(hosts)  # tolerate an earlier import with different env


class TestHostProfiles(unittest.TestCase):
    def test_codex_binding_never_changes(self):
        # THE regression lock: Codex caches the tool->resource binding across logout/login; changing
        # either value orphans every existing install ("Unknown resource"). See server.py wall comment.
        self.assertEqual(hosts.codex.PROFILE.resource_uri, "ui://pairputer-platform/app.html")
        self.assertEqual(hosts.codex.PROFILE.resource_mime, "text/html;profile=mcp-app")
        self.assertEqual(hosts.codex.PROFILE.reconnect_command, "codex mcp login pairputer")

    def test_all_hosts_present_and_distinct(self):
        ids = [p.id for p in hosts.all_profiles()]
        self.assertEqual(sorted(ids), ["chatgpt", "claude", "codex"])
        self.assertEqual(len({p.reconnect_command for p in hosts.all_profiles()}), 3)

    def test_claude_binds_the_shared_resource(self):
        # The MCP Apps spec REQUIRES mime exactly text/html;profile=mcp-app (SEP-1865) — the same
        # value Codex uses. One resource, one URI, three hosts.
        self.assertEqual(hosts.claude.PROFILE.resource_mime, "text/html;profile=mcp-app")
        self.assertEqual(hosts.claude.PROFILE.resource_uri, hosts.codex.PROFILE.resource_uri)

    def test_display_modes_are_host_gated(self):
        # Codex is inline-only: it must declare NO extra modes so the widget never shows a dead
        # PiP/Fullscreen button there. ChatGPT grants pip+fullscreen (verified live 2026-07-08).
        self.assertEqual(hosts.codex.PROFILE.display_modes, ())
        self.assertEqual(set(hosts.chatgpt.PROFILE.display_modes), {"pip", "fullscreen"})
        self.assertEqual(hosts.claude.PROFILE.display_modes, ())  # unknown until PROBE-9

    def test_stream_mode_per_host(self):
        # Codex/ChatGPT embed the relay player iframe (they allow frame-src=<relay>). Claude blocks
        # frame-src, so it streams DIRECTLY from the widget (relay is in connect-src).
        self.assertEqual(hosts.codex.PROFILE.stream_mode, "iframe")
        self.assertEqual(hosts.chatgpt.PROFILE.stream_mode, "iframe")
        self.assertEqual(hosts.claude.PROFILE.stream_mode, "direct")

    def test_client_id_resolution_and_default(self):
        import os
        os.environ["PAIRPUTER_HOST_CLIENT_MAP"] = (
            '{"codex-cid":"codex","gpt-cid":"chatgpt","claude-cid":"claude","m2m-cid":"m2m"}'
        )
        try:
            self.assertEqual(hosts.profile_for_client_id("gpt-cid").id, "chatgpt")
            self.assertEqual(hosts.profile_for_client_id("claude-cid").id, "claude")
            self.assertEqual(hosts.profile_for_client_id("codex-cid").id, "codex")
            self.assertEqual(hosts.profile_for_client_id("m2m-cid").id, "codex")
            self.assertTrue(hosts.native_approval_enforced_for_client_id("codex-cid"))
            self.assertTrue(hosts.native_approval_enforced_for_client_id("gpt-cid"))
            self.assertFalse(hosts.native_approval_enforced_for_client_id("claude-cid"))
            self.assertFalse(hosts.native_approval_enforced_for_client_id("m2m-cid"))
            self.assertFalse(hosts.native_approval_enforced_for_client_id("stranger"))
            # Unknown, empty, and garbage-map cases all fall back to codex (today's behavior).
            self.assertEqual(hosts.profile_for_client_id("stranger").id, "codex")
            self.assertEqual(hosts.profile_for_client_id("").id, "codex")
            os.environ["PAIRPUTER_HOST_CLIENT_MAP"] = "not json"
            self.assertEqual(hosts.profile_for_client_id("gpt-cid").id, "codex")
        finally:
            os.environ.pop("PAIRPUTER_HOST_CLIENT_MAP", None)

    def test_hosts_package_is_capsule_generic(self):
        # HARD RULE: hosts/ carries zero capsule knowledge — strings/URIs only. No capsule ids, no
        # registry/manifest imports. (The platform stays a platform.)
        src = "".join(p.read_text() for p in sorted(HOSTS_DIR.glob("*.py")))
        for banned in ("doom", "hellbox", "IMAGE_REGISTRY", "manifest", "capsule.yaml",
                       "import server", "from server"):
            self.assertNotIn(banned, src.lower() if banned.islower() else src,
                             f"hosts/ must not reference {banned!r}")


class TestServerHostWiring(unittest.TestCase):
    def test_server_reconnect_is_per_identity(self):
        # Every payload site rides _reconnect(identity); no hardcoded reconnect pairs remain.
        self.assertIn("def _reconnect(identity", SERVER)
        self.assertIn("hosts.profile_for_client_id(identity.client_id)", SERVER)
        self.assertNotIn('"reconnectCommand": RECONNECT_COMMAND', SERVER)
        # ...and it carries the host's granted display modes so the widget can gate its buttons.
        self.assertIn('"displayModes": list(profile.display_modes)', SERVER)

    def test_tool_meta_carries_both_dialects(self):
        # openai/outputTemplate (Codex+ChatGPT) AND the NESTED _meta.ui.resourceUri (MCP Apps
        # standard — Claude ignores the flat "ui/resourceUri" string key; empirically proven
        # 2026-07-09: no widget until the nested form).
        self.assertIn('RESOURCE_URI = hosts.codex.PROFILE.resource_uri', SERVER)
        self.assertIn('"ui": {"resourceUri": RESOURCE_URI}', SERVER)
        # ...and the legacy flat form, exactly like the official ext-apps SDK emits.
        self.assertIn('"ui/resourceUri": RESOURCE_URI', SERVER)

    def test_every_opening_tool_stamps_a_fresh_widget_nonce(self):
        # A successful play call launches synchronously, but the widget still needs a per-invocation
        # identity so it can tell a NEW open from a host remount replaying old toolOutput.
        self.assertIn("def _mark_explicit_open(payload", SERVER)
        self.assertIn('payload["openNonce"] = uuid.uuid4().hex', SERVER)
        # Definition + play_capsule + the two deprecated output-template aliases.
        self.assertEqual(SERVER.count("_mark_explicit_open("), 4)
        self.assertNotIn("def _opening_payload(", SERVER)  # the old unused STARTING payload is gone

    def test_server_imports_resolve_and_bindings_hold(self):
        # Load the real server.py (stubbed AWS/MCP deps, the existing harness pattern) and assert the
        # registered resources: Codex URI+mime byte-identical, standard URI+mime present.
        code = r"""
import sys, types
for mod in ("boto3", "botocore", "botocore.exceptions", "boto3.dynamodb", "boto3.dynamodb.conditions"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["botocore.exceptions"].ClientError = Exception
class _Attr:
    def __init__(self, *a, **k): pass
sys.modules["boto3.dynamodb.conditions"].Attr = _Attr
sys.modules["boto3.dynamodb.conditions"].Key = _Attr
resources = []
class _FakeMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k):
        def deco(fn): return fn
        return deco
    def resource(self, *a, **k):
        def deco(fn): return fn
        resources.append({"args": a, "mime": k.get("mime_type"), "meta": k.get("meta")})
        return deco
    def run(self, *a, **k): pass
fastmcp = types.ModuleType("mcp.server.fastmcp"); fastmcp.FastMCP = _FakeMCP; fastmcp.Context = object
types_mod = types.ModuleType("mcp.types")
for name in ("CallToolResult", "TextContent", "ImageContent"):
    setattr(types_mod, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
sys.modules.update({"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
                    "mcp.server.fastmcp": fastmcp, "mcp.types": types_mod})
sys.modules["boto3"].client = lambda *a, **k: types.SimpleNamespace()
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)
import runpy
runpy.run_path(r"%s", run_name="not_main")
by_uri = {r["args"][0]: r for r in resources if r["args"]}
codex = by_uri["ui://pairputer-platform/app.html"]
assert codex["mime"] == "text/html;profile=mcp-app", codex["mime"]
assert "ui://pairputer-platform/app-std.html" not in by_uri, "std resource should be retired"
meta = codex["meta"] or {}
assert "openai/widgetCSP" in meta and "ui" in meta, meta.keys()
print("HOSTS-RESOURCES-OK", len(by_uri))
""" % str(MCP_DIR / "server.py")
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("HOSTS-RESOURCES-OK", p.stdout,
                      f"stdout={p.stdout!r} stderr={p.stderr[-800:]!r}")

    def test_image_id_resolution_is_exact_and_never_cross_binds(self):
        code = r"""
import sys, types
for mod in ("boto3", "botocore", "botocore.exceptions", "boto3.dynamodb", "boto3.dynamodb.conditions"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["botocore.exceptions"].ClientError = Exception
class _Attr:
    def __init__(self, *a, **k): pass
sys.modules["boto3.dynamodb.conditions"].Attr = _Attr
sys.modules["boto3.dynamodb.conditions"].Key = _Attr
class _FakeMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k):
        def deco(fn): return fn
        return deco
    def resource(self, *a, **k):
        def deco(fn): return fn
        return deco
    def run(self, *a, **k): pass
fastmcp = types.ModuleType("mcp.server.fastmcp"); fastmcp.FastMCP = _FakeMCP; fastmcp.Context = object
types_mod = types.ModuleType("mcp.types")
for name in ("CallToolResult", "TextContent", "ImageContent"):
    setattr(types_mod, name, type(name, (), {"__init__": lambda self, *a, **k: None}))
sys.modules.update({"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
                    "mcp.server.fastmcp": fastmcp, "mcp.types": types_mod})
sys.modules["boto3"].client = lambda *a, **k: types.SimpleNamespace()
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)
import runpy
ns = runpy.run_path(r"%s", run_name="not_main")
resolve = ns["_resolve_image_id"]
# The functions close over the REAL module globals (resolve.__globals__), which runpy returns a COPY of
# in `ns` — so patch __globals__ directly. Single-capsule stack under the manifest id 'agent-doom';
# neutralize live tag discovery so the env registry is the whole truth (boto3 is stubbed).
G = resolve.__globals__
G["_discover_capsules_by_tag"] = lambda: {}
G["IMAGE_REGISTRY"] = {"agent-doom": {"arn": "arn:x", "name": "Agent DOOM", "description": ""}}
# Forgiving-but-unique resolution (2026-07-11): what humans say resolves when it matches exactly
# ONE capsule; anything without a unique match still fails closed — never a silent cross-bind.
assert resolve("doom") == "agent-doom", resolve("doom")     # unique fuzzy -> resolves
try:
    resolve("zzz-not-a-capsule")
except ValueError:
    pass
else:
    raise AssertionError("non-matching ids must fail closed even with one capsule")
assert resolve("") == "agent-doom", resolve("")             # empty + SOLE capsule -> default
assert resolve("agent-doom") == "agent-doom"                # exact id -> itself
G["IMAGE_REGISTRY"] = {"a-desk": {"arn": "arn:a"}, "b-desk": {"arn": "arn:b"}}
try:
    resolve("nope")
except ValueError:
    pass
else:
    raise AssertionError("unknown multi-capsule id must fail closed")
try:
    resolve("desk")
except ValueError:
    pass
else:
    raise AssertionError("AMBIGUOUS fuzzy match must fail closed, never guess")
try:
    resolve("")
except ValueError:
    pass
else:
    raise AssertionError("empty image_id with MULTIPLE capsules must refuse, not pick the first")
print("RESOLVE-EXACT-OK")
""" % str(MCP_DIR / "server.py")
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("RESOLVE-EXACT-OK", p.stdout,
                      f"stdout={p.stdout!r} stderr={p.stderr[-800:]!r}")


class TestCloudFormationHostWiring(unittest.TestCase):
    IDENTITY = (REPO_ROOT / "substrate" / "cloudformation" / "nested" / "identity.yaml").read_text()
    AGENTCORE = (REPO_ROOT / "substrate" / "cloudformation" / "nested" / "agentcore.yaml").read_text()
    ROOT = (REPO_ROOT / "substrate" / "cloudformation" / "pairputer.yaml").read_text()

    def test_identity_has_one_client_per_host(self):
        for res in ("ChatGPTClient:", "ClaudeClient:", "CodexClient:"):
            self.assertIn(res, self.IDENTITY)
        # Fixed callbacks: Claude's are static in-template; ChatGPT's per-connector URL is registered
        # post-deploy by wire-chatgpt.sh (the legacy static URL keeps the client deployable).
        self.assertIn("https://claude.ai/api/mcp/auth_callback", self.IDENTITY)
        self.assertIn("https://chatgpt.com/connector_platform_oauth_redirect", self.IDENTITY)
        for out in ("ChatGPTClientId:", "ClaudeClientId:"):
            self.assertIn(out, self.IDENTITY)

    def test_agentcore_admits_all_hosts_on_both_runtime_paths(self):
        # The dual-path rule: native (Private mode) AND custom (Public mode) runtimes must carry the
        # same AllowedClients + env. Each new client id must appear on BOTH.
        for ref in ("!Ref ChatGPTClientId", "!Ref ClaudeClientId"):
            self.assertEqual(self.AGENTCORE.count(ref), 2,
                             f"{ref} must appear on both runtime paths (native + custom)")
        self.assertEqual(self.AGENTCORE.count("PAIRPUTER_HOST_CLIENT_MAP"), 2,
                         "host map env must be on both runtime paths")

    def test_root_threads_new_client_ids(self):
        for line in ("ChatGPTClientId: !GetAtt IdentityStack.Outputs.ChatGPTClientId",
                     "ClaudeClientId: !GetAtt IdentityStack.Outputs.ClaudeClientId"):
            self.assertIn(line, self.ROOT)


class TestWidgetHostAdapter(unittest.TestCase):
    def test_reconnect_strings_are_payload_driven(self):
        # The widget must prefer the server-sent per-host strings; Codex values are only defaults.
        self.assertIn("sc.reconnectCommand", APP)
        self.assertIn("sc.reconnectHint", APP)
        self.assertIn("let RECONNECT_COMMAND", APP)
        self.assertNotIn("const RECONNECT_COMMAND", APP)

    def test_widget_never_hardcodes_a_capsule_id(self):
        # A widget booted without a play payload (ChatGPT rendering from a state read) must let the
        # SERVER resolve the default capsule; a hardcoded id poisons every explicit call on stacks
        # whose sole capsule is named differently (live bug 2026-07-08: thaw sent image_id 'doom'
        # to a stack whose only capsule is 'agent-doom').
        self.assertIn("let imageId = ''", APP)
        self.assertNotIn("imageId = 'doom'", APP)

    def test_fresh_open_overrides_stale_intent_but_remount_replay_does_not(self):
        # Regression: AWS was RUNNING while the widget painted TERMINATED because play_capsule's
        # full RUNNING payload had no open nonce and an old localStorage 'stopped' latch won.
        # Execute the widget's pure freshness predicate against both sides of the contract.
        start = APP.index("function isFreshExplicitOpen")
        end = APP.index("\n}\n", start) + len("\n}\n")
        helper = APP[start:end]
        script = helper + r'''
const fresh = {status:'running', state:'RUNNING', openNonce:'new-open'};
if (!isFreshExplicitOpen(fresh, 'old-open')) throw new Error('new play was not explicit');
if (isFreshExplicitOpen(fresh, 'new-open')) throw new Error('remount replay looked explicit');
if (isFreshExplicitOpen({status:'running', state:'RUNNING'}, '')) throw new Error('missing nonce looked explicit');
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        # A real fresh nonce—not the stale stopped/frozen latch—wins. The same replayed nonce falls
        # through to the existing bootIntent checks and therefore still preserves Freeze/Trash.
        self.assertNotIn("if (explicitOpen && (bootIntent === 'stopped'", APP)
        self.assertIn("if (explicitOpen && haveSession)", APP)
        self.assertIn("setIntent('running');", APP)

    def test_display_modes_from_host_context(self):
        # The MCP Apps standard (Claude) is the authoritative source for which modes a host grants:
        # hostContext.availableDisplayModes from ui/initialize + live host-context-changed updates.
        # The widget must merge those (not just the payload displayModes) so Claude's fullscreen
        # button appears — and if Claude ever advertises pip, that button appears automatically.
        self.assertIn("mergeHostModes(hc.availableDisplayModes)", APP)
        self.assertIn("ui/notifications/host-context-changed", APP)
        self.assertIn("function onHostContext", APP)
        # Claude always grants fullscreen (design guidelines) but doesn't reliably send
        # availableDisplayModes — so the standard bridge seeds fullscreen optimistically.
        self.assertIn("if (stdBridge) mergeHostModes(['fullscreen'])", APP)

    def test_fullscreen_asks_the_sdk_and_pip_is_removed(self):
        # Updated 2026-07-12: PiP was REMOVED (user QA: fullscreen works great, PiP is redundant on
        # the combined ChatGPT/Codex app). Fullscreen stays: always call requestDisplayMode and trust
        # the GRANTED mode; fall back to native element-fullscreen if the host declines. Mode requests
        # still only from click handlers.
        self.assertNotIn('<button id="pip"', APP)              # PiP button removed
        self.assertNotIn("setMode(displayMode === 'pip'", APP)  # PiP handler removed
        self.assertIn("granted = await setMode('fullscreen')", APP)  # always ask the SDK
        self.assertIn("document.documentElement.requestFullscreen()", APP)  # native fallback
        self.assertIn("host.getWidgetState()", APP)

    def test_universal_settings_menu(self):
        # One gear button opens a settings pane: capsule switch (Workbench⇄DOOM) + air-gap toggle.
        # Universal across capsules; rows that don't apply hide themselves. (No memory selector: AWS
        # Lambda MicroVM caps at 8 GB for al2023-1, so 8 GB is the only tier.)
        self.assertIn('<button id="settingsbtn">', APP)
        self.assertIn('id="settingspane"', APP)
        # capsule switch relaunches via play_capsule and reuses the lifecycle-intent latch
        self.assertIn("switchCapsule", APP)
        self.assertIn("callToolStructured('play_capsule'", APP)
        # air-gap toggle drives the network_airgap tool; row hides on capsules without it
        self.assertIn("network_airgap", APP)
        self.assertIn("agRow.hidden = true", APP)

    def test_budget_safety_knobs_in_settings(self):
        # The settings pane exposes the USER-preference layer of budget/safety: an auto-suspend selector
        # (bounded by the operator ceiling) + a usage readout. NOT a dollar-budget slider (that's
        # operator enforcement a user must not be able to edit).
        self.assertIn('id="setidlerow"', APP)      # auto-suspend selector
        self.assertIn('id="usagerow"', APP)         # usage readout
        self.assertIn("session_settings", APP)      # driven by the session_settings tool
        self.assertIn("action: 'set_idle'", APP)
        # selector is bounded by the operator ceiling (idleSecondsMax), user can only pick tighter
        self.assertIn("u.idleSecondsMax", APP)
        self.assertIn("v <= max", APP)
        # cost is shown as an estimate, never a bill
        self.assertIn("not a bill", APP)
        # NO user-editable dollar budget (that would be self-service quota bypass)
        self.assertNotIn('id="setbudget"', APP)

    def test_loading_cover_until_first_frame(self):
        # A scale-to-zero cold start can take ~45s; the stream window must show a friendly loading state
        # (not a bare black box) and drop it the INSTANT the first frame paints — on both player paths.
        self.assertIn('id="loadcover"', APP)
        self.assertIn("function showLoadCover", APP)
        self.assertIn("function firstFrame", APP)
        # shown while starting / running-before-first-frame
        self.assertIn("showLoadCover(true)", APP)
        self.assertIn("if (!gotFirstFrame) showLoadCover(true)", APP)
        # DIRECT player: first painted frame drops the cover
        self.assertIn("if (frames === 1) firstFrame()", APP)
        # IFRAME player: FPS>0 (or the relay's 'playing' message) drops the cover
        self.assertIn("if (fps && Number(fps[1]) > 0) firstFrame()", APP)
        self.assertIn("firstFrame();   // the iframe player reports it's actively streaming", APP)
        # the relay's iframe player actually emits those signals (frames===1 -> 'playing')
        self.assertIn("if(frames===1)tell('playing')", RELAY)

    def test_smart_vm_picker_flow(self):
        # The image picker checks the caller's OWN VM state for the target, then: resume a suspended/
        # running owned VM automatically (freezing the current session first), or ask before launching
        # a new one. Tenancy: the widget only ever sends image_id (a capsule TYPE) — never a VM id — so
        # the server resolves to THIS caller's VM and a switch can't cross tenants.
        self.assertIn("targetState", APP)                       # read-only per-tenant state check
        self.assertIn("capsule_state", APP)                     # via the tenant-scoped state tool
        self.assertIn("freezeCurrentIfRunning", APP)            # freeze current before switching away
        self.assertIn("confirmLaunch", APP)                     # ask before a NEW launch
        self.assertIn("'SUSPENDED'", APP) and self.assertIn("'RUNNING'", APP)
        # a NEW launch is gated behind confirmLaunch; a suspended/running owned VM switches automatically
        idx_susp = APP.find("state === 'SUSPENDED' || state === 'RUNNING'")
        self.assertGreater(idx_susp, 0, "suspended/running owned VM must switch automatically")
        # the widget must NOT send a microvm id anywhere in the switch flow (tenancy invariant)
        self.assertNotIn("microvm_id:", APP)
        self.assertNotIn("microvmId:", APP)  # only ever READ from server payloads, never SENT

    def test_stream_stall_watchdog(self):
        # ChatGPT desktop's docked pop-out severs the player's SSE streams (FPS 0 while RUNNING);
        # the widget must detect the stall, re-mint the token, and restart streams (stop THEN start
        # — the player's startStreams() no-ops while it thinks it's already started). Routed through
        # the backend-agnostic sendPlayerCmd so it works for both iframe and direct players.
        self.assertIn("function watchForStall", APP)
        self.assertIn("'stream stall'", APP)
        idx_stop = APP.find("sendPlayerCmd('stop')")
        idx_start = APP.find("sendPlayerCmd('start', { muted })")
        self.assertTrue(0 < idx_stop < idx_start, "stall recovery must stop before start")

    def test_suspended_stream_recovers_to_thaw_state(self):
        # The relay intentionally rejects data-plane traffic to SUSPENDED VMs so stale players
        # cannot wake/bill them. A relay-state failure must therefore fall back to the MCP control
        # plane and render Thaw, rather than swallowing the failure and reconnecting forever.
        self.assertIn("function reconcileInactiveSession", APP)
        self.assertIn("function probeLifecycleOnReconnect", APP)
        self.assertIn("if (isInactiveVmState(sc.state)) reconcileInactiveSession(sc);", APP)
        self.assertIn("refreshSession({ reason: 'player reconnect', requireToken: false })", APP)
        self.assertIn("refreshSession({ reason: 'poll fallback', requireToken: false })", APP)
        self.assertIn("suspended — click Thaw to resume", APP)
        inactive_idx = APP.find("if (sc && isInactiveVmState(sc.state))")
        wake_idx = APP.find("if ((!sc || (!sc.token && !sc.relayUrl)) && !ensureRunning && canAutoWake())")
        self.assertTrue(0 < inactive_idx < wake_idx, "inactive control-plane state must win before auto-recovery")

    def test_standard_bridge_branch_exists(self):
        # Claude (MCP Apps standard) path: the app must send a FULL ui/initialize (protocolVersion +
        # clientInfo) and follow with ui/notifications/initialized — hosts keep the iframe veiled
        # until then (observed blank widget on claude.ai 2026-07-09). Tool data then arrives as
        # ui/notifications/tool-result, stashed for the reader.
        self.assertIn("ui/notifications/tool-result", APP)
        self.assertIn("uiBridgeData", APP)
        self.assertIn("host.initialize()", APP)
        self.assertIn("protocolVersion: '2026-01-26'", APP)
        self.assertIn("appInfo", APP)  # the spec key — clientInfo is silently rejected by hosts
        self.assertIn("ui/notifications/initialized", APP)
        self.assertIn("ui/notifications/size-changed", APP)
        self.assertIn("hostContext", APP)

    def test_tools_call_gated_behind_handshake(self):
        # THE claude.ai reveal bug (root-caused in a local host harness): the host refuses to reveal
        # the app if a tools/call arrives before ui/notifications/initialized. Two guards:
        # (1) standard-bridge callTool awaits the handshake gate; the gate opens only inside
        #     initialize(), AFTER 'initialized' is sent — never on a boot timeout (that fired the
        #     premature tools/call).
        self.assertIn("await handshake;", APP)
        self.assertIn("handshakeDone()", APP)
        self.assertNotIn("host.releaseGate()", APP)  # the premature boot release is gone
        # (2) on the standard bridge the widget renders from the delivered tool-result payload and
        #     SKIPS the boot refreshSession — so no tools/call races the handshake at all.
        self.assertIn("stdBridge", APP)
        self.assertIn("if (stdBridge && haveSession && !explicitOpen)", APP)

    def test_direct_player_for_no_frame_src_hosts(self):
        # Claude blocks frame-src → the widget streams via an in-widget engine (makeDirectPlayer),
        # selected by the payload's streamMode==='direct'. The relay's SSE/POST are CORS-open.
        self.assertIn("function makeDirectPlayer", APP)
        self.assertIn("useDirectPlayer = sc.streamMode === 'direct'", APP)
        self.assertIn("streamMode", SERVER)  # server emits it per host
        # The engine must reuse the relay's transports directly (SSE video/audio + POST input).
        self.assertIn("EventSource(U('/video'))", APP)
        self.assertIn("EventSource(U('/audio'))", APP)
        self.assertIn("fetch(U('/input')", APP)


if __name__ == "__main__":
    unittest.main()
