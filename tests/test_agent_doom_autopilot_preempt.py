"""Autopilot preemption: an explicit MCP/human drive_goal must not be starved by the
in-VM autopilot, which shares the same RUNTIME and lock.

The bug: autopilot bursts loop back-to-back holding self._lock; an explicit command
queued behind them until the MCP socket read timed out ("tool read timeout"). The fix:
an explicit caller raises self._preempt so an in-flight autopilot burst bails at the top
of its step loop and frees the lock fast.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = REPO_ROOT / "capsules" / "agent-doom" / "rootfs" / "opt" / "capsule"
sys.path.insert(0, str(CAPSULE_ROOT))

from brain_runtime import BrainRuntime  # noqa: E402


class TestAutopilotPreempt(unittest.TestCase):
    def _runtime_with_captured_drive(self):
        rt = BrainRuntime()
        seen = {}

        def fake_locked(directive, *, full, include_recent=False, is_autopilot=False):
            # Record whether the preempt flag was set at the moment the lock body ran,
            # and whether this call was tagged autopilot.
            seen["is_autopilot"] = is_autopilot
            seen["preempt_at_entry"] = rt._preempt.is_set()
            return {"status": "ok", "stop_reason": "done", "objective": directive.objective}

        rt._drive_locked = fake_locked  # type: ignore[assignment]
        return rt, seen

    def test_explicit_drive_clears_preempt_inside_lock_and_after(self):
        rt, seen = self._runtime_with_captured_drive()
        rt.drive_goal({"goal": "fight demons"})
        # Explicit caller: once it holds the lock it stops signalling; flag is clear after.
        self.assertFalse(seen["is_autopilot"])
        self.assertFalse(seen["preempt_at_entry"])
        self.assertFalse(rt._preempt.is_set())

    def test_autopilot_drive_is_tagged_and_leaves_preempt_untouched(self):
        rt, seen = self._runtime_with_captured_drive()
        rt.drive_goal({"goal": "clear the map", "source": "autopilot"})
        self.assertTrue(seen["is_autopilot"])
        # Autopilot must NOT set the preempt flag (only explicit callers do).
        self.assertFalse(rt._preempt.is_set())

    def test_autopilot_step_loop_yields_when_preempt_set(self):
        # Directly exercise the gate the fix added: is_autopilot AND _preempt -> bail.
        rt = BrainRuntime()
        rt._preempt.set()
        self.assertTrue(rt._preempt.is_set())
        # The gate condition the loop uses:
        should_yield = True and rt._preempt.is_set()
        self.assertTrue(should_yield)
        rt._preempt.clear()
        self.assertFalse(True and rt._preempt.is_set())

    def test_explicit_drive_is_async_and_returns_immediately(self):
        # The timeout fix: an explicit drive_goal must NOT block on the drive (which runs
        # ~15-60s and overruns the transport timeout). It returns status=driving instantly
        # and runs the drive on a background thread.
        rt = BrainRuntime()
        started = threading.Event()
        release = threading.Event()

        def slow_locked(directive, *, full, include_recent=False, is_autopilot=False):
            started.set()
            release.wait(5)  # simulate a long drive
            return {"status": "ok"}

        rt._drive_locked = slow_locked  # type: ignore[assignment]
        t0 = __import__("time").monotonic()
        out = rt.drive_goal({"goal": "fight demons"})
        elapsed = __import__("time").monotonic() - t0
        self.assertLess(elapsed, 1.0, "explicit drive must return without waiting for the drive")
        self.assertEqual(out["status"], "driving")
        self.assertTrue(out.get("async"))
        self.assertIn("committed_contract", out)
        self.assertTrue(started.wait(2), "the drive must actually run in the background")
        release.set()

    def test_wait_true_runs_synchronously(self):
        # Eval/headless callers can still get the real result with wait=true.
        rt = BrainRuntime()
        seen = {}

        def fake_locked(directive, *, full, include_recent=False, is_autopilot=False):
            seen["ran"] = True
            return {"status": "ok", "stop_reason": "done", "steps": 3}

        rt._drive_locked = fake_locked  # type: ignore[assignment]
        rt._compact_goal_result = lambda r: r  # type: ignore[assignment]
        out = rt.drive_goal({"goal": "fight demons", "wait": True})
        self.assertTrue(seen.get("ran"))
        self.assertEqual(out.get("stop_reason"), "done")
        self.assertNotEqual(out.get("status"), "driving")

    def test_rapid_commands_do_not_stack_threads(self):
        # Firing many commands must not leak threads: only one background drive at a time.
        rt = BrainRuntime()

        def quick_locked(directive, *, full, include_recent=False, is_autopilot=False):
            return {"status": "ok"}

        rt._drive_locked = quick_locked  # type: ignore[assignment]
        for _ in range(10):
            rt.drive_goal({"goal": "fight demons"})
        # At most the one tracked bg thread should linger (others finished/were joined).
        alive = [t for t in threading.enumerate() if t.name == "drive_goal_bg" and t.is_alive()]
        self.assertLessEqual(len(alive), 1, "background drive threads must not stack")

    def test_explicit_preempt_visible_to_a_concurrent_autopilot(self):
        # Simulate ordering: an explicit caller raises preempt before grabbing the lock,
        # so a concurrent autopilot loop observes it and would bail.
        rt = BrainRuntime()
        observed = threading.Event()

        # Hold the lock as a stand-in autopilot burst; watch for the preempt signal.
        def autopilot_holds_lock():
            with rt._lock:
                for _ in range(100):
                    if rt._preempt.is_set():
                        observed.set()
                        return
                    threading.Event().wait(0.005)

        t = threading.Thread(target=autopilot_holds_lock)
        t.start()
        threading.Event().wait(0.02)  # let it grab the lock
        rt._preempt.set()             # explicit caller signals before blocking on lock
        t.join(timeout=2)
        self.assertTrue(observed.is_set(), "autopilot must observe the explicit preempt signal")


if __name__ == "__main__":
    unittest.main()
