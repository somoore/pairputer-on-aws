"""Per-tenant budget/safety knobs: running-seconds accounting + the operator-bounded idle override.
The cost figure is a display readout, never billing-authoritative; the idle preference can only make a
user's box suspend SOONER than the operator ceiling, never later."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "substrate/mcp-server/server.py").read_text()


def _load(names, extra_ns=None):
    tree = ast.parse(SERVER)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    mod = ast.Module(body=fns, type_ignores=[]); ast.fix_missing_locations(mod)
    ns = {
        "_now": lambda: _load.now[0],
        "MICROVM_COST_PER_SECOND_USD": 0.0001,
        "MICROVM_MAX_IDLE_SECONDS": 300,
        "MICROVM_MIN_IDLE_SECONDS": 60,
    }
    ns.update(extra_ns or {})
    exec(compile(mod, "server.py:budget", "exec"), ns)
    return ns


_load.now = [1000]


def test_running_seconds_accumulate_across_transitions():
    ns = _load({"_accumulate_running_seconds", "_effective_idle_seconds"})
    acc = ns["_accumulate_running_seconds"]
    item = {"state": "RUNNING"}
    _load.now[0] = 1000
    acc(item)                                   # enter RUNNING
    assert item["running_since"] == 1000
    _load.now[0] = 1000 + 200                     # leave RUNNING (freeze) after 200s
    item["state"] = "SUSPENDED"
    acc(item)
    assert item["running_seconds"] == 200         # banked (the persisted integer fact)
    assert item["running_since"] == 0             # interval closed
    # ONLY integer facts are persisted — no float derived fields (they'd break DynamoDB put_item)
    assert "estimated_cost_usd" not in item and "running_seconds_total" not in item
    # resume: a NEW interval accrues on top of the banked 200
    item["state"] = "RUNNING"; _load.now[0] = 5000
    acc(item)
    assert item["running_since"] == 5000
    _load.now[0] = 5030
    acc(item)  # still RUNNING -> running_since unchanged, banked stays 200 until it leaves RUNNING
    assert item["running_seconds"] == 200 and item["running_since"] == 5000


def test_stale_running_interval_is_capped_at_last_proof_of_life_plus_idle():
    # Live-QA 2026-07-14: "this session: 808m running" on a box that ran minutes. The record can
    # sit "RUNNING" for hours after AWS idle-suspended the VM (billing paused) if nobody touches
    # the session; banking raw wall-clock counted the whole suspended night as running. The bank
    # must cap at last-proof-of-life (updated_at of the previous save) + the idle window.
    ns = _load({"_accumulate_running_seconds", "_effective_idle_seconds"})
    acc = ns["_accumulate_running_seconds"]
    # entered RUNNING at t=1000; last save (proof of life) at t=1060; then nobody looked all night
    item = {"state": "SUSPENDED", "running_since": 1000, "updated_at": 1060}
    _load.now[0] = 50000                          # observed ~13.6h later
    acc(item)
    # banked = (1060 + idle 300) - 1000 = 360, NOT 49000
    assert item["running_seconds"] == 360 and item["running_since"] == 0
    # a live freeze (fresh updated_at) still banks the true interval — the cap never undercounts
    item2 = {"state": "SUSPENDED", "running_since": 1000, "updated_at": 1195, "running_seconds": 0}
    _load.now[0] = 1200
    acc(item2)
    assert item2["running_seconds"] == 200


def test_idle_override_clamps_tighter_only():
    ns = _load({"_effective_idle_seconds"})
    eff = ns["_effective_idle_seconds"]
    assert eff({}) == 300                          # unset -> operator default (the ceiling)
    assert eff({"idle_seconds_pref": 0}) == 300     # 0 -> operator default
    assert eff({"idle_seconds_pref": 120}) == 120   # tighter than ceiling -> honored
    assert eff({"idle_seconds_pref": 999}) == 300   # looser than ceiling -> CLAMPED to ceiling
    assert eff({"idle_seconds_pref": 5}) == 60      # below min -> raised to MIN (no thrash)


def test_session_settings_tool_is_readout_and_bounded():
    # the tool never lets a user exceed the operator ceiling and is explicit that cost is a readout
    assert 'name="session_settings"' in SERVER
    assert "def session_settings" in SERVER
    assert "you can't exceed this" in SERVER or "operator ceiling" in SERVER
    assert '"costNote": "estimate for display only, not a bill"' in SERVER
    assert "idleSecondsMax" in SERVER   # the widget reads the ceiling to bound its selector
    # idle knob is operator-parameterized, not a hardcoded constant
    assert 'os.environ.get("PAIRPUTER_MAX_IDLE_SECONDS"' in SERVER
