"""Memory-tier plumbing: memory is fixed at MicroVM-image build (RunMicrovm has no memory param), so a
bigger tier would be a SEPARATE image folded into the base's memoryTiers and hidden from list_capsules.
NOTE (proven on AWS 2026-07-12): the al2023-1 base caps at 8192 MiB (supported: 512/1024/2048/4096/
8192) — 16 GB is rejected at image build, so no tier ships today. This plumbing stays INERT (no tier
registered) in case the cap lifts; these tests pin the resolution + discovery-fold logic remain correct."""
import ast
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "substrate/mcp-server/server.py").read_text()
DEPLOY = (ROOT / "substrate/deploy-capsule.sh").read_text()
STACK = (ROOT / "capsules/nested/capsule-stack.yaml").read_text()


def load_tier_resolver(registry):
    tree = ast.parse(SERVER)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tier_image_id")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {"_effective_registry": lambda: registry}
    exec(compile(module, "server.py:tier", "exec"), ns)
    return ns["_tier_image_id"]


def test_tier_resolution_picks_the_sibling_or_falls_back():
    reg = {
        "computer-use-desktop": {"arn": "a", "memoryTiers": {"16384": "computer-use-desktop-16g"}},
        "computer-use-desktop-16g": {"arn": "b"},
        "agent-doom": {"arn": "c"},
    }
    tier = load_tier_resolver(reg)
    # no size -> base unchanged
    assert tier("computer-use-desktop", None) == "computer-use-desktop"
    # 16 GB -> the registered sibling
    assert tier("computer-use-desktop", 16384) == "computer-use-desktop-16g"
    # 8 GB (no such tier) -> base unchanged
    assert tier("computer-use-desktop", 8192) == "computer-use-desktop"
    # a capsule with no tier map ignores memory_mib
    assert tier("agent-doom", 16384) == "agent-doom"


def test_discovery_folds_tier_siblings_and_hides_them():
    """The fold logic: a discovered image tagged memoryTierOf=<base> + memoryMib=<n> is attached to
    the base's memoryTiers and removed from the top-level capsule list."""
    tree = ast.parse(SERVER)
    src = ast.get_source_segment(SERVER, next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_discover_capsules_by_tag"))
    # the fold block is inline in discovery; assert its shape rather than executing AWS calls
    assert 'found[base]["memoryTiers"] = tier_map' in src
    assert "found.pop(tier_id, None)" in src
    assert '"memoryTierOf": tags.get("pairputer:capsule-memory-tier-of")' in src


def test_play_capsule_accepts_memory_mib():
    assert "def play_capsule(ctx: Context, image_id: str = \"\", memory_mib: int = 0)" in SERVER
    assert "_tier_image_id(_resolve_image_id(image_id), memory_mib" in SERVER


def test_deploy_and_stack_wire_the_tier_tags():
    # deploy-capsule.sh accepts --memory-tier-of and skips the manifest-id match for a tier
    assert "--memory-tier-of" in DEPLOY
    assert 'MemoryTierOf=${MEMORY_TIER_OF}' in DEPLOY
    # the stack tags the image so discovery can fold it
    assert "pairputer:capsule-memory-tier-of" in STACK
    assert "pairputer:capsule-memory-mib" in STACK
