"""A11y-Compressor-style compaction: drop inert scaffolding + dedup so the node
budget is spent on signal. Pure functions, no AT-SPI needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "capsules/computer-use-desktop/rootfs/opt/capsule"))
from atspi_compact import compact_nodes, is_inert  # noqa: E402


def test_inert_scaffolding_is_dropped():
    assert is_inert({"name": "", "role": "filler", "actions": []})
    assert is_inert({"name": "", "role": "panel", "actions": []})
    assert is_inert({"name": "", "role": "separator", "actions": []})
    assert is_inert({"name": "", "role": "unknown", "actions": []})


def test_named_or_operable_nodes_are_never_inert():
    assert not is_inert({"name": "Save", "role": "panel", "actions": []})       # named
    assert not is_inert({"name": "", "role": "icon", "actions": ["activate"]})  # operable
    assert not is_inert({"name": "", "role": "push button", "actions": []})     # meaningful role
    assert not is_inert({"name": "", "role": "heading", "actions": []})


def test_compact_keeps_signal_drops_noise():
    raw = [
        {"name": "", "role": "filler", "actions": [], "depth": 1},
        {"name": "", "role": "panel", "actions": [], "depth": 1},
        {"name": "Save", "role": "push button", "actions": ["click"], "depth": 3},
        {"name": "", "role": "separator", "actions": [], "depth": 3},
        {"name": "Files", "role": "label", "actions": [], "depth": 2},
    ]
    out, stats = compact_nodes(raw)
    kept = {(n["name"], n["role"]) for n in out}
    assert ("Save", "push button") in kept
    assert ("Files", "label") in kept
    assert stats["dropped_inert"] == 3
    assert stats["kept"] == 2


def test_consecutive_duplicate_siblings_are_collapsed():
    raw = [{"name": "cell", "role": "table cell", "actions": [], "depth": 5} for _ in range(300)]
    out, stats = compact_nodes(raw)
    assert len(out) == 1               # 300 identical action-less cells -> one
    assert stats["dropped_dup"] == 299


def test_operable_duplicates_are_kept_not_collapsed():
    raw = [{"name": "row", "role": "list item", "actions": ["activate"], "depth": 5} for _ in range(3)]
    out, _ = compact_nodes(raw)
    assert len(out) == 3               # each is individually operable -> all kept


def test_order_is_preserved():
    raw = [
        {"name": "A", "role": "button", "actions": []},
        {"name": "B", "role": "button", "actions": []},
        {"name": "C", "role": "button", "actions": []},
    ]
    out, _ = compact_nodes(raw)
    assert [n["name"] for n in out] == ["A", "B", "C"]


def test_massive_noisy_tree_compacts_hard():
    # 500 inert + a handful of real controls: compaction should surface the real ones
    noise = [{"name": "", "role": "panel", "actions": [], "depth": d % 8} for d in range(500)]
    real = [{"name": "OK", "role": "push button", "actions": ["click"], "depth": 4}]
    out, stats = compact_nodes(noise + real)
    assert len(out) == 1 and out[0]["name"] == "OK"
    assert stats["dropped_inert"] == 500


def test_stats_shape():
    out, stats = compact_nodes([{"name": "X", "role": "button", "actions": []}])
    assert set(stats) == {"input", "kept", "dropped_inert", "dropped_dup"}
    assert stats["input"] == 1 and stats["kept"] == 1
