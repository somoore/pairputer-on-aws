"""agent-doom capsule + manifest-gated agent tools (interaction.md Tier 1/2, dream.md).

Source-level assertions in the same style as test_image_source_mode.py: cheap, hermetic,
and they fail loudly if someone breaks the contract wiring.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE = REPO_ROOT / "capsules" / "agent-doom"
SERVER = (REPO_ROOT / "substrate" / "mcp-server" / "server.py").read_text()
AGENTCORE = (REPO_ROOT / "substrate" / "cloudformation" / "nested" / "agentcore.yaml").read_text()
ROOT_TPL = (REPO_ROOT / "substrate" / "cloudformation" / "pairputer.yaml").read_text()
DEPLOY = (REPO_ROOT / "substrate" / "deploy.sh").read_text()

# Manifest declares BARE VERBS; the platform namespaces them to agent_doom__<verb> at registration.
EXPECTED_VERBS = [
    "observe",
    "act",
    "drive_objective",
    "drive_ticks",
    "drive_goal",
    "autopilot",
    "tactical_status",
    "brain_status",
    "brain_memory",
    "map_status",
    "vision_status",
    "reset_episode",
    "save_snapshot",
    "load_snapshot",
]
EXPECTED_TOOLS = ["agent_doom__" + v for v in EXPECTED_VERBS]  # platform-namespaced registered names


class TestCapsuleLayout(unittest.TestCase):
    def test_capsule_files_exist(self):
        for f in ["Dockerfile", "capsule.yaml", "wad-source.json", "index.html", "requirements.txt",
                  "eval_gates.py",
                  "rootfs/opt/capsule/start.sh", "rootfs/opt/capsule/run_app.sh",
                  "rootfs/opt/capsule/agent_bridge.py", "rootfs/opt/capsule/brain_runtime.py",
                  "rootfs/opt/capsule/goal_contract.py",
                  "rootfs/opt/capsule/map_cache.py", "rootfs/opt/capsule/planner.py",
                  "rootfs/opt/capsule/door_memory.py", "rootfs/opt/capsule/wad_map.py",
                  "rootfs/opt/capsule/world_memory.py", "rootfs/opt/capsule/combat_state.py",
                  "rootfs/opt/capsule/probe_runtime.py", "rootfs/opt/capsule/trace_logger.py",
                  "rootfs/opt/capsule/vision_state.py", "rootfs/opt/capsule/frame_sampler.py",
                  "rootfs/opt/capsule/vision_brain.py", "rootfs/opt/capsule/vision_adapter.py",
                  "rootfs/opt/capsule/vision_adapter_model.json",
                  "rootfs/opt/capsule/video_ws.py",
                  "rootfs/opt/capsule/input_ws.py", "rootfs/opt/capsule/audio_ws.py"]:
            self.assertTrue((CAPSULE / f).is_file(), f"missing {f}")

    def test_streaming_stack_matches_hellbox(self):
        # The human-facing contract is shared, but agent-doom carries a small Doom-specific overlay:
        # fixed 640x400 output and coordinate remapping so its player matches Hellbox's capsule view.
        hb = REPO_ROOT / "capsules" / "hellbox-doom" / "rootfs" / "opt" / "capsule"
        for f in ["focus.py", "input_selftest.py"]:
            self.assertEqual((hb / f).read_bytes(), (CAPSULE / "rootfs/opt/capsule" / f).read_bytes(),
                             f"{f} diverged between hellbox-doom and agent-doom")
        audio = (CAPSULE / "rootfs/opt/capsule/audio_ws.py").read_text()
        for token in ["PORT = 6902", "DEVICE = \"capsule.monitor\"", "libopus", "OpusHead", "_read_ogg_page"]:
            self.assertIn(token, audio)
        self.assertIn("PAIRPUTER_WS_BIND", audio)

        video = (CAPSULE / "rootfs/opt/capsule/video_ws.py").read_text()
        for token in ["PORT = 6903", "x11grab", "libx264", "keyint={GOP}", "aud=1", "repeat-headers=1"]:
            self.assertIn(token, video)
        self.assertIn("PAIRPUTER_VIDEO_OUTPUT_SIZE", video)
        self.assertIn("scale={out[0]}:{out[1]}:flags=neighbor", video)

        input_ws = (CAPSULE / "rootfs/opt/capsule/input_ws.py").read_text()
        for token in ["PORT = 6904", "class Arbiter", "STATE_PORT = 6906", "agentCursor", "AGENT_KEY_FILE"]:
            self.assertIn(token, input_ws)
        self.assertIn("PAIRPUTER_INPUT_SOURCE_SIZE", input_ws)
        self.assertIn("def _map_xy", input_ws)

    def test_manifest_shape(self):
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        self.assertEqual(m["id"], "agent-doom")
        self.assertTrue(m["interaction"]["tier1"])
        self.assertFalse(m["interaction"]["agentInteractDefault"])
        # capsule.yaml declares bare verbs (no capsule prefix) — the platform owns the final tool name.
        self.assertEqual([t["name"] for t in m["tools"]], EXPECTED_VERBS)
        self.assertEqual(m["bridge"]["port"], 6905)
        # Disruptive tools must gate on human approval.
        approval = {t["name"]: t.get("requiresApproval", False) for t in m["tools"]}
        self.assertTrue(approval["reset_episode"])
        self.assertTrue(approval["load_snapshot"])

    def test_dockerfile_pins_restful_doom(self):
        d = (CAPSULE / "Dockerfile").read_text()
        self.assertIn("RESTFUL_DOOM_COMMIT=", d)          # pinned fetch, not a moving branch
        self.assertIn("grpc_tools.protoc", d)              # stubs generated from the same tree
        self.assertIn("restfuldoom/v1/agent.proto", d)
        self.assertIn("install -m 0755 src/restful-doom", d)
        self.assertIn("agent/restfuldoom_agent/brain.py", d)
        self.assertIn("agent/restfuldoom_agent/env.py", d)
        self.assertIn("RESTFULDOOM_PROTO_STUBS=/opt/capsule/rdgen", d)

    def test_run_app_launches_restful_doom_with_agentport(self):
        r = (CAPSULE / "rootfs/opt/capsule/run_app.sh").read_text()
        self.assertIn("restful-doom", r)
        self.assertIn("-agentport 50051", r)
        self.assertNotIn("chocolate-doom -iwad", r)

    def test_start_sh_supervises_bridge(self):
        s = (CAPSULE / "rootfs/opt/capsule/start.sh").read_text()
        self.assertIn("agent_bridge.py", s)
        self.assertIn("port_up 6905", s)

    def test_bridge_tags_actor_agent(self):
        b = (CAPSULE / "rootfs/opt/capsule/agent_bridge.py").read_text()
        self.assertIn('e["actor"] = "agent"', b)  # actor identity per interaction.md
        for route in ["/observe", "/act", "/reset_episode", "/snapshot/save", "/snapshot/load",
                      "/brain/drive", "/brain/drive_ticks", "/brain/drive_goal", "/brain/tactical_status",
                      "/brain/status", "/brain/memory", "/brain/map_status", "/brain/vision_status",
                      "/input", "/screen", "/health"]:
            self.assertIn(f'"{route}"', b)
        for route in ["/brain/status", "/brain/memory", "/brain/map_status", "/brain/vision_status"]:
            self.assertIn(f'("POST", "{route}")', b)
        self.assertIn('"k": player.get("kills")', b)
        self.assertIn('"kt": level.get("total_kills")', b)

    def test_brain_runtime_uses_restful_doom_controller(self):
        b = (CAPSULE / "rootfs/opt/capsule/brain_runtime.py").read_text()
        self.assertIn("SkillController", b)
        self.assertIn("AgentMemory", b)
        self.assertIn("BrainPolicy/SkillController", (CAPSULE / "capsule.yaml").read_text())
        self.assertIn("allowed_skills", b)
        self.assertIn("human input interrupted", b)
        self.assertIn("budget_exhausted", b)
        self.assertIn('"kills": int(getattr(player, "kills", 0))', b)
        self.assertIn("steps_run", b)
        self.assertIn("shootable_seen", b)
        self.assertIn("_planner_override", b)
        self.assertIn("SpatialPlanner", b)
        self.assertIn("MapCache", b)
        self.assertIn("DoorMemory", b)
        self.assertIn("WorldMemory", b)
        self.assertIn("CombatState", b)
        self.assertIn("ProbeBatcher", b)
        self.assertIn("VisionBrain", b)
        self.assertIn("TraceLogger", b)
        self.assertIn("compile_goal_contract", b)
        self.assertIn("_guard_contract_action", b)
        self.assertIn("_contract_override", b)
        self.assertIn("ACTION_SWITCH_WEAPON", b)
        self.assertIn("_run_action", b)
        self.assertIn("CopyFrom(action)", b)
        self.assertIn("map_status", b)
        planner = (CAPSULE / "rootfs/opt/capsule/planner.py").read_text()
        self.assertIn("planner_route_use_line_for_contact", planner)
        self.assertIn("max_distance_fp=1920 * FP_UNIT", planner)
        self.assertIn("amount=42, duration_tics=14", planner)
        self.assertNotIn("vector_", b)
        self.assertIn("explicit_allowed_skills", b)
        self.assertNotIn("len(transitions), last_skill", b)
        self.assertNotIn("eval(", b)
        self.assertNotIn("exec(", b)

    def test_act_rejects_silent_no_op(self):
        # A wrong-shaped act payload ({"attack":true}) was SILENTLY dropped -> empty action -> DOOM did
        # nothing but the tool returned success (the agent 'shot 3 times' with no effect). The bridge must
        # parse strictly and reject an empty/unknown action so the agent gets a real error.
        b = (CAPSULE / "rootfs/opt/capsule/agent_bridge.py").read_text()
        self.assertIn("ignore_unknown_fields=False", b)        # strict parse in _act
        self.assertIn("ACTION_UNSPECIFIED", b)                 # empty-action guard
        self.assertIn("empty action", b)

    def test_act_tool_teaches_the_action_vocabulary(self):
        # The act tool description must give the LLM the exact ACTION_* enum + payload shape so it doesn't
        # guess {"attack":true} and no-op. (Root cause of "says it shot but never did" + the flailing.)
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        act = next(t for t in m["tools"] if t["name"] == "act")
        for verb in ("ACTION_SHOOT", "ACTION_FORWARD", "ACTION_TURN_RIGHT", "ACTION_USE"):
            self.assertIn(verb, act["description"])
        self.assertIn('"action"', act["description"])          # names the field
        self.assertIn("3 times", act["description"])           # answers "shoot 3 times"
        self.assertLess(len(act["description"]), 420)

    def test_drive_goal_is_preferred_high_level_tool(self):
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        drive = next(t for t in m["tools"] if t["name"] == "drive_objective")
        self.assertEqual(drive["path"], "/brain/drive")
        self.assertFalse(drive.get("requiresApproval", False))
        self.assertIn("Prefer drive_goal", drive["description"])
        self.assertIn("map-aware planner", drive["description"])
        self.assertLess(len(drive["description"]), 260)

        ticks = next(t for t in m["tools"] if t["name"] == "drive_ticks")
        self.assertEqual(ticks["path"], "/brain/drive_ticks")
        self.assertGreaterEqual(ticks["timeoutSeconds"], 60)
        self.assertFalse(ticks.get("requiresApproval", False))
        self.assertIn("max_tics", ticks["description"])
        self.assertIn("compact status", ticks["description"])
        self.assertIn("never raw map geometry", ticks["description"])
        self.assertLess(len(ticks["description"]), 220)

        goal = next(t for t in m["tools"] if t["name"] == "drive_goal")
        self.assertEqual(goal["path"], "/brain/drive_goal")
        self.assertGreaterEqual(goal["timeoutSeconds"], 60)
        self.assertFalse(goal.get("requiresApproval", False))
        self.assertIn("natural-language DOOM control", goal["description"])
        self.assertIn("objective enum", goal["description"])
        self.assertIn("committed_contract", goal["description"])
        self.assertIn("stop_reason", goal["description"])
        # anti-duplicate steer: gameplay prompts on a running DOOM must go here, not play_capsule
        self.assertIn("play_capsule", goal["description"])
        self.assertLess(len(goal["description"]), 280)  # +20 over the others for the steering clause

        tactical = next(t for t in m["tools"] if t["name"] == "tactical_status")
        self.assertEqual(tactical["path"], "/brain/tactical_status")
        self.assertFalse(tactical.get("requiresApproval", False))
        self.assertIn("Commander status", tactical["description"])
        self.assertIn("human_active", tactical["description"])
        self.assertIn("No raw maps", tactical["description"])
        self.assertLess(len(tactical["description"]), 220)

    def test_manifest_declares_capsule_help_metadata(self):
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        exp = m["experience"]
        self.assertIn("humanHelpText", exp)
        self.assertIn("suggestedPrompts", exp)
        self.assertIn("beat this level", exp["suggestedPrompts"])
        self.assertIn("find an enemy and punch it, no ammo", exp["suggestedPrompts"])
        self.assertIn("free_form_goals", exp["capabilities"])

    def test_map_status_tool_is_compact_debug_only(self):
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        tool = next(t for t in m["tools"] if t["name"] == "map_status")
        self.assertEqual(tool["path"], "/brain/map_status")
        self.assertFalse(tool.get("requiresApproval", False))
        self.assertIn("Compact debug status", tool["description"])
        self.assertIn("No raw map geometry", tool["description"])
        self.assertLess(len(tool["description"]), 120)

    def test_vision_status_tool_is_compact_debug_only(self):
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        tool = next(t for t in m["tools"] if t["name"] == "vision_status")
        self.assertEqual(tool["path"], "/brain/vision_status")
        self.assertFalse(tool.get("requiresApproval", False))
        self.assertIn("Compact debug status", tool["description"])
        self.assertIn("No screenshots or raw model text", tool["description"])
        self.assertLess(len(tool["description"]), 130)

    def test_hellbox_doom_stays_tier0(self):
        # The classic capsule must never grow a manifest — Tier 0 (agent-inert) is its contract.
        self.assertFalse((REPO_ROOT / "capsules" / "hellbox-doom" / "capsule.yaml").exists())


class TestServerGating(unittest.TestCase):
    def test_tier2_is_generic_manifest_driven(self):
        # NO capsule-specific tool code in the shared server: Tier 2 tools are registered by a generic
        # loop over the manifest, dispatching to the manifest-declared bridge `path`.
        self.assertIn("PAIRPUTER_CAPSULE_MANIFEST", SERVER)
        self.assertIn("def _register_tier2_tool", SERVER)
        self.assertIn("timeoutSeconds", SERVER)
        self.assertIn("timeout_s=timeout_s", SERVER)
        # Registration iterates over EVERY capsule's manifest (env seed + tag-discovered), not one env manifest.
        self.assertIn("def _all_capsule_manifests(", SERVER)
        self.assertIn("for _cap_image, _cap_manifest in _MANIFESTS_BY_IMAGE.items():", SERVER)
        # The generic dispatcher forwards to the manifest path — no 'doom'-shaped defs remain.
        self.assertNotIn('def doom_act(', SERVER)
        self.assertNotIn('def doom_observe(', SERVER)
        self.assertIn("if _TIER1_ENABLED:", SERVER)  # Tier 1 stays platform code
        for t1 in ["capsule_send_keys", "capsule_key_chord", "capsule_pointer", "capsule_read_screen"]:
            self.assertIn(t1, SERVER)

    def test_manifest_tools_declare_path_and_label(self):
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        for t in m["tools"]:
            self.assertIn("path", t, f"{t['name']} must declare a bridge path for generic dispatch")
            self.assertTrue(t["path"].startswith("/"))
            self.assertIn("label", t, f"{t['name']} should have a theatre-of-work label")

    def test_tool_names_are_platform_namespaced(self):
        # The PLATFORM owns tool naming: capsule.yaml declares bare verbs; the server prefixes each with the
        # capsule id (<capsule>__<verb>). No capsule may pick its own final tool name -> no cross-capsule
        # collisions and no capsule-specific names baked into the platform.
        self.assertIn("def _namespaced_tool_name(", SERVER)
        self.assertIn("def _capsule_ns(", SERVER)
        # Manifest declares BARE verbs (no 'doom_' prefix in the capsule file anymore).
        m = yaml.safe_load((CAPSULE / "capsule.yaml").read_text())["capsule"]
        for t in m["tools"]:
            self.assertFalse(t["name"].startswith("doom_"),
                             f"{t['name']} should be a bare verb; platform adds the capsule namespace")

    def test_tier1_screens_sensitive_patterns(self):
        # interaction.md: even Tier 1 raw input must be screenable against the TARGET capsule's
        # sensitivePatterns (a privileged capsule driven by synthetic keys can still act on the world).
        self.assertIn("def _screen_tier1(", SERVER)
        self.assertIn("_PATTERNS_BY_IMAGE", SERVER)
        # each Tier 1 input tool calls the screen and accepts an exact single-use approval token
        for t1 in ("capsule_send_keys", "capsule_key_chord", "capsule_pointer"):
            idx = SERVER.index(f"def {t1}(")
            body = SERVER[idx:idx + 600]
            self.assertIn("_screen_tier1(", body, f"{t1} must screen sensitivePatterns")
            self.assertIn("approval_token", body, f"{t1} must accept exact approval tokens")

    def test_bridge_uses_authed_vm_gateway(self):
        self.assertIn("create_microvm_auth_token", SERVER)
        self.assertIn("X-aws-proxy-auth", SERVER)
        self.assertIn("X-aws-proxy-port", SERVER)

    def test_no_auto_thaw_from_agent_tools(self):
        # Agent tools must not start/resume VMs (billing + surprise are the human's call).
        self.assertIn("ask the human to open/Thaw it first", SERVER)

    def test_approval_gate_exists(self):
        self.assertIn("_require_approval", SERVER)
        self.assertIn("def capsule_approve", SERVER)
        self.assertIn("_consume_exact_approval", SERVER)
        self.assertIn("confirmed_preview: dict", SERVER)
        self.assertIn("approval confirmation does not match the exact displayed action", SERVER)
        self.assertIn('"preview": preview', SERVER)

    def test_tools_off_without_manifest(self):
        # Import the server module with NO manifest env and stub deps; agent tools must not register.
        code = r"""
import sys, types, json, gzip, base64, hashlib
for mod in ("boto3", "botocore", "botocore.exceptions", "boto3.dynamodb", "boto3.dynamodb.conditions"):
    m = types.ModuleType(mod); sys.modules.setdefault(mod, m)
sys.modules["botocore.exceptions"].ClientError = Exception
class _Attr:  # noqa
    def __init__(self, *a, **k): pass
sys.modules["boto3.dynamodb.conditions"].Attr = _Attr
sys.modules["boto3.dynamodb.conditions"].Key = _Attr
tools = []
class _FakeMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k):
        def deco(fn):
            tools.append(k.get("name") or fn.__name__); return fn
        return deco
    def resource(self, *a, **k):
        def deco(fn): return fn
        return deco
    def run(self, *a, **k): pass
fastmcp = types.ModuleType("mcp.server.fastmcp"); fastmcp.FastMCP = _FakeMCP; fastmcp.Context = object
mcp_mod = types.ModuleType("mcp"); server_mod = types.ModuleType("mcp.server")
types_mod = types.ModuleType("mcp.types")
class CallToolResult:  # noqa
    def __init__(self, *a, **k): pass
class TextContent:  # noqa
    def __init__(self, *a, **k): pass
class ImageContent:  # noqa
    def __init__(self, *a, **k): pass
types_mod.CallToolResult = CallToolResult; types_mod.TextContent = TextContent; types_mod.ImageContent = ImageContent
sys.modules.update({"mcp": mcp_mod, "mcp.server": server_mod,
                    "mcp.server.fastmcp": fastmcp, "mcp.types": types_mod})
sys.modules["boto3"].client = lambda *a, **k: types.SimpleNamespace()
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)
import os
os.environ.pop("PAIRPUTER_CAPSULE_MANIFEST", None)
import runpy
g = runpy.run_path(r"%s", run_name="not_main")
AGENT_TOOLS = {"agent_doom__observe", "agent_doom__act", "agent_doom__drive_objective",
               "agent_doom__drive_ticks", "agent_doom__drive_goal", "agent_doom__tactical_status",
               "agent_doom__brain_status", "agent_doom__brain_memory",
               "agent_doom__reset_episode", "agent_doom__save_snapshot",
               "agent_doom__load_snapshot", "capsule_send_keys",
               "capsule_key_chord", "capsule_pointer", "capsule_read_screen"}
agent = sorted(set(tools) & AGENT_TOOLS)
assert agent == [], f"agent tools registered WITHOUT a manifest: {agent}"
print("TIER0-OK", len(tools))
""" % str(REPO_ROOT / "substrate" / "mcp-server" / "server.py")
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("TIER0-OK", p.stdout, f"stdout={p.stdout!r} stderr={p.stderr[-800:]!r}")

    def _registered_tools(self, extra_env=None):
        """Import server.py under the mock harness and return the set of registered tool names."""
        code = r"""
import sys, types, json, gzip, base64, hashlib
for mod in ("boto3", "botocore", "botocore.exceptions", "boto3.dynamodb", "boto3.dynamodb.conditions"):
    m = types.ModuleType(mod); sys.modules.setdefault(mod, m)
sys.modules["botocore.exceptions"].ClientError = Exception
class _Attr:
    def __init__(self, *a, **k): pass
sys.modules["boto3.dynamodb.conditions"].Attr = _Attr
sys.modules["boto3.dynamodb.conditions"].Key = _Attr
tools = []
class _FakeMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k):
        def deco(fn):
            tools.append(k.get("name") or fn.__name__); return fn
        return deco
    def resource(self, *a, **k):
        def deco(fn): return fn
        return deco
    def run(self, *a, **k): pass
fastmcp = types.ModuleType("mcp.server.fastmcp"); fastmcp.FastMCP = _FakeMCP; fastmcp.Context = object
mcp_mod = types.ModuleType("mcp"); server_mod = types.ModuleType("mcp.server")
types_mod = types.ModuleType("mcp.types")
class CallToolResult:
    def __init__(self, *a, **k): pass
class TextContent:
    def __init__(self, *a, **k): pass
class ImageContent:
    def __init__(self, *a, **k): pass
types_mod.CallToolResult = CallToolResult; types_mod.TextContent = TextContent; types_mod.ImageContent = ImageContent
sys.modules.update({"mcp": mcp_mod, "mcp.server": server_mod,
                    "mcp.server.fastmcp": fastmcp, "mcp.types": types_mod})
sys.modules["boto3"].client = lambda *a, **k: types.SimpleNamespace()
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)
import os, runpy
os.environ.pop("PAIRPUTER_CAPSULE_MANIFEST", None)
runpy.run_path(r"%s", run_name="not_main")
print("TOOLS", json.dumps(sorted(tools)))
""" % str(REPO_ROOT / "substrate" / "mcp-server" / "server.py")
        env = dict(os.environ, **(extra_env or {}))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env)
        line = next((l for l in p.stdout.splitlines() if l.startswith("TOOLS ")), None)
        self.assertIsNotNone(line, f"no TOOLS line; stderr={p.stderr[-800:]!r}")
        return set(json.loads(line[len("TOOLS "):]))

    def test_deprecated_aliases_gated_off_by_default(self):
        # Default: the 4 deprecated aliases do NOT register (no tools/list cost); the generic tools
        # they aliased are always present, so no capability is lost.
        aliases = {"play_doom", "play_image", "doom_state", "list_images"}
        generics = {"play_capsule", "capsule_state", "list_capsules"}
        default = self._registered_tools()
        self.assertEqual(default & aliases, set(), f"aliases leaked by default: {default & aliases}")
        self.assertEqual(generics - default, set(), f"generic tools missing: {generics - default}")
        # Opt back in: the flag restores every alias for an old integration.
        on = self._registered_tools({"PAIRPUTER_DEPRECATED_ALIASES": "1"})
        self.assertEqual(aliases - on, set(), f"flag did not restore aliases: {aliases - on}")

    def test_n_capsules_each_register_their_own_tools(self):
        # THE cartridge end-state: two genuinely different capsules — one from the env manifest (agent-doom)
        # and one tag-DISCOVERED with its manifest in SSM (a fake 'cloudshell') — each register their OWN
        # namespaced Tier 2 tools on the single MCP server. Exercises server.py's real registration path.
        code = r"""
import sys, types, json, gzip, base64, hashlib
for mod in ("boto3", "botocore", "botocore.exceptions", "boto3.dynamodb", "boto3.dynamodb.conditions"):
    m = types.ModuleType(mod); sys.modules.setdefault(mod, m)
sys.modules["botocore.exceptions"].ClientError = Exception
class _Attr:
    def __init__(self, *a, **k): pass
sys.modules["boto3.dynamodb.conditions"].Attr = _Attr
sys.modules["boto3.dynamodb.conditions"].Key = _Attr
tools = []
class _FakeMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k):
        def deco(fn):
            tools.append(k.get("name") or fn.__name__); return fn
        return deco
    def resource(self, *a, **k):
        def deco(fn): return fn
        return deco
    def run(self, *a, **k): pass
fastmcp = types.ModuleType("mcp.server.fastmcp"); fastmcp.FastMCP = _FakeMCP; fastmcp.Context = object
types_mod = types.ModuleType("mcp.types")
class CallToolResult:
    def __init__(self, *a, **k): pass
class TextContent:
    def __init__(self, *a, **k): pass
class ImageContent:
    def __init__(self, *a, **k): pass
types_mod.CallToolResult = CallToolResult; types_mod.TextContent = TextContent; types_mod.ImageContent = ImageContent
sys.modules.update({"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
                    "mcp.server.fastmcp": fastmcp, "mcp.types": types_mod})

# Capsule B ('cloudshell'): discovered by tag, manifest in SSM. Declares a bare verb 'exec' -> the platform
# namespaces it to cloudshell__exec. Own sensitivePatterns.
SHELL_MANIFEST = {"capsule": {"id": "cloudshell",
    "tools": [{"name": "exec", "path": "/exec", "label": "running a command"}],
    "safety": {"sensitivePatterns": ["aws iam"]}}}
IMAGE_ARN = "arn:aws:lambda:us-east-1:1:microvm-image:cloudshell"
ENCODED = "gzip+base64:" + base64.b64encode(
    gzip.compress(json.dumps(SHELL_MANIFEST).encode(), mtime=0)).decode()
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(ENCODED.encode()).hexdigest()
MANIFEST_PARAM = "/pairputer/capsules/cloudshell/manifests/sha256-" + MANIFEST_DIGEST.split(":", 1)[1]
RELEASE = {"schemaVersion": 1, "capsuleId": "cloudshell", "imageArn": IMAGE_ARN,
    "imageVersion": "7", "manifestParameter": MANIFEST_PARAM, "manifestDigest": MANIFEST_DIGEST,
    "contextSha256": "0" * 64, "contextUri": "s3://fixture/context.tar"}
RELEASE_DIGEST = "sha256:" + hashlib.sha256(json.dumps(
    RELEASE, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
RELEASE["releaseDigest"] = RELEASE_DIGEST
RELEASE_PARAM = "/pairputer/capsules/cloudshell/releases/sha256-" + RELEASE_DIGEST.split(":", 1)[1]
POINTER = {"schemaVersion": 1, "capsuleId": "cloudshell",
    "releaseParameter": RELEASE_PARAM, "releaseDigest": RELEASE_DIGEST}
class _Pager:
    def paginate(self, **k):
        return [{"ResourceTagMappingList": [{"ResourceARN":
            IMAGE_ARN,
            "Tags": [{"Key": "pairputer:capsule", "Value": "true"},
                     {"Key": "pairputer:capsule-id", "Value": "cloudshell"},
                     {"Key": "pairputer:capsule-manifest-ssm", "Value": MANIFEST_PARAM},
                     {"Key": "pairputer:capsule-release-ssm", "Value": "/pairputer/capsules/cloudshell/current"}]}]}]
def _client(name, **k):
    if name == "resourcegroupstaggingapi":
        return types.SimpleNamespace(get_paginator=lambda n: _Pager())
    if name == "ssm":
        values = {"/pairputer/capsules/cloudshell/current": json.dumps(POINTER, sort_keys=True, separators=(",", ":")),
                  RELEASE_PARAM: json.dumps(RELEASE, sort_keys=True, separators=(",", ":")),
                  MANIFEST_PARAM: ENCODED}
        return types.SimpleNamespace(get_parameter=lambda Name: {"Parameter": {"Value": values[Name]}})
    return types.SimpleNamespace()
sys.modules["boto3"].client = _client
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(Table=lambda n: None)

import os
# Capsule A ('agent-doom') comes from the env manifest.
os.environ["PAIRPUTER_CAPSULE_MANIFEST"] = json.dumps({"capsule": {"id": "agent-doom",
    "interaction": {"tier1": True},
    "tools": [{"name": "act", "path": "/act", "label": "acting"}]}})
import runpy
runpy.run_path(r"%s", run_name="not_main")
# Each capsule's bare verb is platform-namespaced to <capsule-id>__<verb>. Distinct capsules never collide.
assert "agent_doom__act" in tools, f"env-manifest capsule tool missing/not namespaced: {tools}"
assert "cloudshell__exec" in tools, f"discovered capsule tool missing/not namespaced: {tools}"
# No BARE (un-namespaced) capsule verb leaked as a tool name.
assert "act" not in tools and "exec" not in tools, f"un-namespaced verb leaked: {tools}"
# Tier 1 primitives on because at least one capsule declared interaction.tier1.
assert "capsule_send_keys" in tools, f"tier1 not enabled: {tools}"
# The hot-add dispatcher is ALWAYS registered (static), for cartridges inserted mid-session.
assert "capsule_invoke" in tools, f"capsule_invoke missing: {tools}"
print("N-CAPSULES-OK", sorted(t for t in tools if t in ("agent_doom__act", "cloudshell__exec", "capsule_send_keys", "capsule_invoke")))
""" % str(REPO_ROOT / "substrate" / "mcp-server" / "server.py")
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("N-CAPSULES-OK", p.stdout, f"stdout={p.stdout!r} stderr={p.stderr[-1200:]!r}")

    def test_capsule_metadata_is_generic_manifest_driven(self):
        self.assertIn("def _capsule_metadata(", SERVER)
        self.assertIn("humanHelpText", SERVER)
        self.assertIn("suggestedPrompts", SERVER)
        self.assertIn("def capsule_metadata(", SERVER)
        self.assertIn('"capsule": _capsule_metadata', SERVER)
        self.assertNotIn('if image_id == "doom"', SERVER)


class TestPlumbing(unittest.TestCase):
    def test_agentcore_param_env_iam(self):
        self.assertIn("CapsuleManifestJson:", AGENTCORE)
        self.assertEqual(AGENTCORE.count("PAIRPUTER_CAPSULE_MANIFEST: !Ref CapsuleManifestJson"), 2,
                         "manifest env must be on BOTH runtime paths (native + custom)")
        self.assertIn("lambda:CreateMicrovmAuthToken", AGENTCORE)

    def test_root_param_passthrough(self):
        self.assertIn("CapsuleManifestJson:", ROOT_TPL)
        self.assertIn("CapsuleManifestJson: !Ref CapsuleManifestJson", ROOT_TPL)

    def test_deploy_sh_passes_manifest(self):
        self.assertIn("capsule.yaml", DEPLOY)
        self.assertIn("CapsuleManifestJson=", DEPLOY)

    def test_redeploy_preserves_stack_managed_doom_image_ownership(self):
        self.assertIn("STACK_MANAGES_DOOM_IMAGE", DEPLOY)
        self.assertIn("--logical-resource-id DoomImageStack", DEPLOY)
        self.assertIn('elif [[ "${STACK_MANAGES_DOOM_IMAGE}" == "true" ]]', DEPLOY)
        managed = DEPLOY.index('elif [[ "${STACK_MANAGES_DOOM_IMAGE}" == "true" ]]')
        adopt = DEPLOY.index("Found existing MicroVM image", managed)
        self.assertLess(managed, adopt)

    def test_stack_ownership_lookup_only_treats_confirmed_not_found_as_absent(self):
        start = DEPLOY.index("detect_doom_stack_ownership()")
        end = DEPLOY.index("\n}\n", start) + 3
        helper = DEPLOY[start:end]
        script = r'''
aws() {
  printf '%s' "${FAKE_AWS_STDOUT:-}"
  printf '%s' "${FAKE_AWS_STDERR:-}" >&2
  return "${FAKE_AWS_RC:-0}"
}
''' + helper + '\ndetect_doom_stack_ownership fixture us-east-1\n'

        def run(*, stdout="", stderr="", rc=0):
            env = dict(os.environ, FAKE_AWS_STDOUT=stdout, FAKE_AWS_STDERR=stderr, FAKE_AWS_RC=str(rc))
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)

        managed = run(stdout="pairputer-doom-stack\n")
        self.assertEqual(managed.returncode, 0, managed.stderr)
        self.assertEqual(managed.stdout.strip(), "managed")

        absent = run(rc=255, stderr=(
            "An error occurred (ValidationError) when calling the DescribeStackResource operation: "
            "Stack with id fixture does not exist"
        ))
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertEqual(absent.stdout.strip(), "absent")

        absent_resource = run(rc=255, stderr=(
            "An error occurred (ValidationError) when calling the DescribeStackResource operation: "
            "Resource DoomImageStack does not exist for stack fixture"
        ))
        self.assertEqual(absent_resource.returncode, 0, absent_resource.stderr)
        self.assertEqual(absent_resource.stdout.strip(), "absent")

        for error in (
            "An error occurred (AccessDenied) when calling the DescribeStackResource operation: denied",
            "An error occurred (ThrottlingException) when calling the DescribeStackResource operation: slow down",
            "An error occurred (ValidationError) when calling the DescribeStackResource operation: invalid stack name",
            "An error occurred (ValidationError) when calling the DescribeStackResource operation: IAM role does not exist",
        ):
            failed = run(rc=255, stderr=error)
            self.assertNotEqual(failed.returncode, 0)
            self.assertNotIn("absent", failed.stdout)
            self.assertIn("refusing to continue", failed.stderr)

        empty_success = run()
        self.assertNotEqual(empty_success.returncode, 0)
        self.assertIn("returned no physical ID", empty_success.stderr)


if __name__ == "__main__":
    unittest.main()


class TestLocalMode(unittest.TestCase):
    """LOCAL MODE (roadmap F dev loop) — verify the gates exist and are env-flagged, production-safe."""

    def test_local_mode_gates_present(self):
        s = SERVER
        self.assertIn("LOCAL_MODE = os.environ.get(\"PAIRPUTER_LOCAL_MODE\"", s)
        self.assertIn("def _local_vm(", s)
        # discovery, ensure-running, session payload, bridge, and identity all branch on LOCAL_MODE.
        self.assertGreaterEqual(s.count("if LOCAL_MODE:"), 5)
        # bridge talks plain HTTP to the local capsule instead of the :443 gateway.
        self.assertIn("http.client.HTTPConnection(endpoint, local_port", s)
        self.assertIn("_bridge_settings_for(image_id)", s)
        # local identity needs no Cognito JWT.
        self.assertIn("tenant_id=LOCAL_TENANT", s)

    def test_local_mode_serves_novnc_viewer(self):
        # No relay locally -> the widget loads the capsule's own player (viewerUrl :6901/index.html),
        # reports state RUNNING (no false SUSPENDED/billing overlay), and the widget honors both.
        s = SERVER
        self.assertIn('"viewerUrl"', s)
        self.assertIn("index.html", s)
        self.assertIn("video_ws", s)
        self.assertIn("audio_ws", s)
        self.assertIn("input_ws", s)
        self.assertIn('"state": "RUNNING"', s)   # local payload never SUSPENDED
        app = (REPO_ROOT / "substrate/mcp-server/app.html").read_text()
        self.assertIn("sc.viewerUrl", app)                 # widget reads it
        self.assertIn("if (viewer) return viewer;", app)   # iframe loads noVNC directly
        self.assertIn("if (viewer) show = false;", app)    # no freeze overlay in local mode

    def test_local_dev_script_exists_and_rebuilds(self):
        script = REPO_ROOT / "substrate" / "local-dev.sh"
        self.assertTrue(script.is_file())
        t = script.read_text()
        self.assertIn("PAIRPUTER_LOCAL_MODE=1", t)
        self.assertIn("docker build", t)      # always rebuilds by default (no stale-image trap)
        self.assertIn('"127.0.0.1:${BRIDGE_PORT}:${BRIDGE_PORT}"', t)  # loopback-only manifest bridge
        self.assertIn("6906:6906", t)          # exposes coplay state for agent action labels


class TestCoordination(unittest.TestCase):
    """Co-play arbitration (interaction.md Phase 4) — human-always-wins, agent yields, in the SHARED input path."""

    INPUT_WS = (REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule" / "input_ws.py").read_text()

    def test_arbiter_present_in_shared_input(self):
        s = self.INPUT_WS
        self.assertIn("class Arbiter", s)
        self.assertIn("def on_human", s)
        self.assertIn("def allow_agent", s)
        self.assertIn("AGENT_COOLDOWN_S", s)
        self.assertIn("STATE_PORT = 6906", s)  # state endpoint for the widget

    def test_arbitration_is_platform_wide(self):
        # The arbiter contract is platform-wide. Agent Doom may add coordinate scaling, but it must not
        # fork the human-always-wins arbitration or actor-trust model.
        hb = (REPO_ROOT / "capsules" / "hellbox-doom" / "rootfs" / "opt" / "capsule" / "input_ws.py").read_text()
        for s in [hb, self.INPUT_WS]:
            for token in ["class Arbiter", "def on_human", "def allow_agent", "def note_agent_action",
                          "STATE_PORT = 6906", "agentCursor", "AGENT_KEY_FILE",
                          "msg[\"actor\"] = \"human\"", "ARBITER.on_human()"]:
                self.assertIn(token, s)

    def test_arbiter_logic(self):
        import os, re, time, threading
        os.environ["PAIRPUTER_AGENT_COOLDOWN_S"] = "0.2"
        ns = {"os": os, "time": time, "threading": threading}
        m = re.search(r"AGENT_COOLDOWN_S = .*?\nclass Arbiter:.*?\n\nARBITER = Arbiter\(\)", self.INPUT_WS, re.S)
        exec(m.group(0), ns)
        A = ns["Arbiter"]()
        self.assertTrue(A.allow_agent())          # fresh: agent may act
        A.on_human()
        self.assertFalse(A.allow_agent())         # human just acted: agent blocked
        self.assertEqual(A.owner(), "human")
        time.sleep(0.25)
        self.assertTrue(A.allow_agent())          # cooldown passed: agent may act again
        A.on_human()
        self.assertEqual(A.owner(), "human")      # human preempts mid-grant (hard rule)

    def test_widget_shows_whose_turn(self):
        app = (REPO_ROOT / "substrate" / "mcp-server" / "app.html").read_text()
        self.assertIn("renderCoplay", app)
        self.assertIn("Agent is driving", app)
        self.assertIn("You are driving", app)
        self.assertIn("/coplay", app)
        self.assertIn("agent-driving", app)  # frame glow class
        self.assertIn("capsulehelp", app)
        self.assertIn("applyCapsuleMetadata", app)
        self.assertIn("sc.capsule", app)

    def test_relay_proxies_coplay_state(self):
        relay = (REPO_ROOT / "substrate" / "stateful-relay" / "index.mjs").read_text()
        self.assertIn("/coplay", relay)
        self.assertIn("COPLAY_PORT", relay)

    def test_actor_agent_requires_connection_auth(self):
        # interaction.md defense-in-depth (audit gap #2): input_ws only honors actor=agent from a
        # connection that authenticated with the in-VM key file; everything else is FORCED to human.
        for cap in ("agent-doom", "hellbox-doom"):
            s = (REPO_ROOT / "capsules" / cap / "rootfs" / "opt" / "capsule" / "input_ws.py").read_text()
            self.assertIn("AGENT_KEY_FILE", s)
            self.assertIn('msg["actor"] = "human"', s)      # unauthenticated -> forced human
            self.assertIn("hmac.compare_digest", s)         # constant-time key compare
        # the bridge performs the handshake; start.sh provisions the key (agent-doom only — a Tier 0
        # capsule like hellbox gets NO key, so nothing there can ever claim agent).
        bridge = (REPO_ROOT / "capsules/agent-doom/rootfs/opt/capsule/agent_bridge.py").read_text()
        self.assertIn('"t": "auth"', bridge)
        start = (REPO_ROOT / "capsules/agent-doom/rootfs/opt/capsule/start.sh").read_text()
        self.assertIn("agent-input.key", start)
        hb_start = (REPO_ROOT / "capsules/hellbox-doom/rootfs/opt/capsule/start.sh").read_text()
        self.assertNotIn("agent-input.key", hb_start)

    def test_session_token_carries_agent_interact_claim(self):
        # interaction.md: the session token states whether THIS capsule permits agent interaction.
        self.assertIn('"agentInteract"', SERVER)
        self.assertIn("_AGENT_ALLOWED_BY_IMAGE", SERVER)

    def test_service_logs_reach_cloudwatch(self):
        # Runtime log data must never be VM-trapped: services tee to BOTH stdout (-> CloudWatch via the
        # supervisor) AND the file the PAIRPUTER_DEBUG /dbg endpoints serve. And the security/lifecycle
        # events (auth refusals, connections) log UNCONDITIONALLY via AUDIT — not DEBUG-gated.
        for cap in ("agent-doom", "hellbox-doom"):
            start = (REPO_ROOT / "capsules" / cap / "rootfs/opt/capsule/start.sh").read_text()
            self.assertIn("| tee -a /var/log/input_ws.log", start, cap)
            self.assertIn("| tee -a /var/log/video_ws.log", start, cap)
            self.assertNotIn(">> /var/log/input_ws.log", start, cap)  # old file-only redirect gone
            ws = (REPO_ROOT / "capsules" / cap / "rootfs/opt/capsule/input_ws.py").read_text()
            self.assertIn("def AUDIT(", ws)
            self.assertIn('AUDIT("agent auth REFUSED', ws)      # the audit trail that matters
            self.assertIn('AUDIT("client connected', ws)
        agent_start = (REPO_ROOT / "capsules/agent-doom/rootfs/opt/capsule/start.sh").read_text()
        self.assertIn("| tee -a /var/log/agent_bridge.log", agent_start)

    def test_relay_ships_runtime_logs_from_dbg(self):
        # Durable runtime logs: the relay pulls the capsule's /dbg tail (served UNCONDITIONALLY now, with an
        # ?offset incremental protocol) and PutLogEvents to a relay-owned per-capsule group.
        relay = (REPO_ROOT / "substrate/stateful-relay/index.mjs").read_text()
        self.assertIn("CloudWatchLogsClient", relay)
        self.assertIn("PutLogEventsCommand", relay)
        self.assertIn("function shipSessionLogs(", relay)
        self.assertIn("capsuleLogGroup", relay)
        self.assertIn("/pairputer/capsule-runtime/", relay)   # relay-owned namespace, not the build group
        self.assertIn("/dbg/bridge", relay)
        # hook.py serves the dbg files UNCONDITIONALLY (no DEBUG gate) with the incremental offset protocol.
        for cap in ("agent-doom", "hellbox-doom"):
            start = (REPO_ROOT / "capsules" / cap / "rootfs/opt/capsule/start.sh").read_text()
            self.assertIn('if u.path in DBG_FILES: return self._dbg(u.path, parse_qs(u.query))', start, cap)
            self.assertNotIn("if DEBUG and p in DBG_FILES", start, cap)   # old opt-in gate gone
            self.assertIn('"size": size', start, cap)                    # incremental protocol
        # SDK dep declared for npm ci.
        pkg = (REPO_ROOT / "substrate/stateful-relay/package.json").read_text()
        self.assertIn("@aws-sdk/client-cloudwatch-logs", pkg)
        # RelayTaskRole granted logs write scoped to the relay-owned namespace ONLY (not "*").
        rel = (REPO_ROOT / "substrate/cloudformation/nested/relay.yaml").read_text()
        self.assertIn("ShipCapsuleRuntimeLogs", rel)
        self.assertIn("log-group:/pairputer/capsule-runtime/*", rel)

    def test_agent_cursor_end_to_end_wiring(self):
        # The legacy capsule point feeds the generalized Agent Cursor attribution layer.
        # capsule: arbiter records the agent's last pointer move + exposes it in the snapshot.
        s = (REPO_ROOT / "capsules/agent-doom/rootfs/opt/capsule/input_ws.py").read_text()
        self.assertIn("note_agent_cursor", s)
        self.assertIn('"agentCursor"', s)
        # Widget keeps the legacy DOM id for compatibility but uses authoritative display transforms.
        app = (REPO_ROOT / "substrate/mcp-server/app.html").read_text()
        self.assertIn("ghostcur", app)
        self.assertIn("agentCursor", app)
        self.assertIn("displayMetadata", app)
        self.assertIn("renderRect", app)
        self.assertNotIn("cur.x / 320", app)


class TestInteractionContract(unittest.TestCase):
    """The interaction.md gaps that were closed."""

    def test_tier1_scroll_and_drag(self):
        self.assertIn('"scroll"', SERVER)
        self.assertIn('"drag"', SERVER)

    def test_sensitive_pattern_screening(self):
        # Patterns are per-capsule now (each capsule's own manifest.safety), carried on the tool spec —
        # not a single server-wide global — so N capsules each screen against their OWN sensitive list.
        self.assertIn("def _matched_sensitive_pattern", SERVER)
        self.assertIn("sensitivePatterns", SERVER)
        self.assertIn("_cap_patterns", SERVER)  # per-capsule pattern list built in the registration loop

    def test_agent_interact_gate_on_bridge(self):
        # The bridge gate is PER-CAPSULE and call-time-capable: this capsule's own manifest must declare
        # interaction; one capsule's declaration never opens another's bridge, and a hot-added cartridge
        # (manifest resolved at call time) passes without a server restart.
        self.assertIn("def _agent_interact_for(", SERVER)
        self.assertIn("_agent_interact_for(image_id)", SERVER)
        self.assertIn("does not permit agent interaction", SERVER)

    def test_capsule_invoke_hot_add(self):
        # FastMCP registers tools at startup; capsule_invoke is the hot-add path for a cartridge inserted
        # mid-session: resolves the manifest AT CALL TIME with the SAME gates as registered tools.
        self.assertIn("def capsule_invoke(", SERVER)
        self.assertIn("def _manifest_for(", SERVER)
        idx = SERVER.index("def capsule_invoke(")
        body = SERVER[idx:idx + 2000]
        self.assertIn("_require_approval(", body)     # approval + sensitivePatterns, same as typed tools
        self.assertIn("_note_agent_action(", body)    # theatre of work
        self.assertIn("_manifest_for(capsule_id)", body)  # call-time manifest resolution
        self.assertIn('tool.split("__", 1)[-1]', body)    # accepts bare verb AND namespaced form

    def test_per_capsule_iam_role_hook(self):
        self.assertIn("def _capsule_run_role", SERVER)
        self.assertIn("executionRole", SERVER)
        self.assertIn("permissions.iamRole", SERVER)  # the manifest field is honored


class TestDisplayModePiP(unittest.TestCase):
    """PiP is an OPT-IN enhancement; inline stays the default/known-good fallback (never forced)."""

    APP = (REPO_ROOT / "substrate" / "mcp-server" / "app.html").read_text()

    def test_pip_is_opt_in_not_forced(self):
        # No auto display-mode request on boot/stream — inline is the default that ships today.
        self.assertNotIn("enterCoplayLayout()", self.APP)  # removed the auto-pip path
        # The "Keep visible" (PiP) button was removed — Codex only does inline, so it was a
        # no-op. Assert the button ELEMENT is gone (the string may still appear in the
        # explanatory comments that document the removal — that's fine).
        self.assertNotIn('id="pipbtn"', self.APP)
        self.assertNotIn("<button id=\"pipbtn\"", self.APP)
        self.assertNotIn(">📌 Keep visible<", self.APP)

    def test_reads_granted_mode(self):
        # requestDisplayMode returns the GRANTED mode — we render from it, not the requested one.
        self.assertIn("granted", self.APP)
        self.assertIn("requestDisplayMode", self.APP)

    def test_persists_state_for_remounts(self):
        self.assertIn("setWidgetState", self.APP)
        self.assertIn("persistState", self.APP)

    def test_listens_for_host_globals(self):
        self.assertIn("openai:set_globals", self.APP)


class TestCapsuleCartridge(unittest.TestCase):
    """Cartridge model: standalone capsule stack + tag-based MCP discovery (docs/capsule-architecture.md)."""

    def test_standalone_capsule_stack_exists_and_tags(self):
        tpl = (REPO_ROOT / "capsules" / "nested" / "capsule-stack.yaml")
        self.assertTrue(tpl.is_file())
        t = tpl.read_text()
        # Generalized off DOOM + carries the discovery tag namespace.
        self.assertIn("CapsuleId", t)
        self.assertIn("pairputer:capsule", t)
        self.assertIn("pairputer:capsule-id", t)
        self.assertIn("pairputer:capsule-manifest-ssm", t)

    def test_mcp_discovers_capsules_by_tag(self):
        self.assertIn("def _discover_capsules_by_tag", SERVER)
        self.assertIn('"pairputer:capsule"', SERVER)
        self.assertIn("get_resources", SERVER)  # tag-based enumeration
        self.assertIn("def _effective_registry", SERVER)
        # list_capsules + resolution use the effective (discovered) registry, not just the env.
        self.assertIn("_effective_registry()", SERVER)

    def test_iam_controls_tagged_capsules(self):
        # Control plane can drive ANY pairputer:capsule=true image (cartridge deployed later), tag-scoped.
        self.assertIn("MicrovmControlTaggedCapsules", AGENTCORE)
        self.assertIn("aws:ResourceTag/pairputer:capsule", AGENTCORE)
        self.assertIn("tag:GetResources", AGENTCORE)

    def test_deploy_capsule_script(self):
        s = (REPO_ROOT / "substrate" / "deploy-capsule.sh")
        self.assertTrue(s.is_file())
        t = s.read_text()
        self.assertIn("CapsuleId=", t)
        self.assertIn("pairputer:capsule", t)  # tags the manifest SSM param
        self.assertIn("capsule-stack.yaml", t)
