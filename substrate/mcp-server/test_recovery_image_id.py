"""Self-check for the recovery image_id decision: empty image_id + multiple capsules resolves to the
caller's SOLE live VM, and refuses (falls through to the strict resolver) on zero/ambiguous.

Run: python3 substrate/mcp-server/test_recovery_image_id.py"""
import sys


def pick(live_image_ids, reg):
    """The core decision extracted from _resolve_recovery_image_id: given the caller's live-VM image
    ids and the registry, return the resolved id or None (meaning: fall through to strict refuse)."""
    uniq = sorted({c for c in live_image_ids if c in reg})
    return uniq[0] if len(uniq) == 1 else None


def main():
    reg = {"agent-doom", "computer-use-desktop"}
    # sole live VM (the actual bug: suspended workbench, empty image_id) -> resolves to it
    assert pick(["computer-use-desktop"], reg) == "computer-use-desktop"
    # duplicate rows for the same capsule still count as one -> resolves
    assert pick(["computer-use-desktop", "computer-use-desktop"], reg) == "computer-use-desktop"
    # zero live VMs -> None (refuse with list, as before)
    assert pick([], reg) is None
    # two different live capsules -> genuinely ambiguous -> None (refuse)
    assert pick(["agent-doom", "computer-use-desktop"], reg) is None
    # a live VM for a capsule no longer in the registry is ignored
    assert pick(["retired-capsule"], reg) is None
    print("OK: recovery image_id resolution")


if __name__ == "__main__":
    try:
        main()
    except AssertionError:
        print("FAIL", file=sys.stderr)
        raise
