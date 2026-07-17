#!/usr/bin/env python3.11
"""Ready-gate input self-test.

Proves that XTEST-synthesized input actually reaches an X client, so a flaky image
build where XTEST silently no-ops (the failure we hit: identical source, dead input)
FAILS the readiness gate instead of shipping a broken image. Exit 0 = input verified.

It tests the exact mechanism DOOM depends on — xtest.fake_input -> X event delivery —
using a small control window we own, so the result is deterministic and does not
depend on DOOM's demo/menu behaviour. Create a window, focus it, inject a key via
XTEST, and confirm the window receives the KeyPress.
"""
import sys
import time

from Xlib import X, XK, display
from Xlib.ext import xtest


def _drain(d):
    while d.pending_events():
        d.next_event()


def _inject_and_check(d, win, keysym):
    kc = d.keysym_to_keycode(keysym)
    if not kc:
        return None  # can't map this key; caller tries another
    _drain(d)
    xtest.fake_input(d, X.KeyPress, kc)
    d.sync()
    xtest.fake_input(d, X.KeyRelease, kc)
    d.sync()
    deadline = time.time() + 1.0
    while time.time() < deadline:
        while d.pending_events():
            ev = d.next_event()
            if ev.type == X.KeyPress:
                return True
        time.sleep(0.02)
    return False


def main():
    d = display.Display(":1")
    if not d.query_extension("XTEST"):
        print("input-selftest: FAIL XTEST extension missing", file=sys.stderr)
        return 2

    root = d.screen().root
    # override_redirect keeps the WM/focus-asserter from reparenting or restacking us
    # while we test; we take input focus explicitly so injected keys route here.
    win = root.create_window(
        0, 0, 16, 16, 0,
        d.screen().root_depth,
        X.InputOutput,
        X.CopyFromParent,
        override_redirect=1,
        event_mask=X.KeyPressMask,
    )
    win.map()
    d.sync()
    time.sleep(0.3)

    ok = False
    try:
        for _ in range(3):
            # Grab the keyboard so injected keys route to us regardless of the
            # focus-asserter; fall back to set_input_focus if the grab is unavailable.
            grabbed = False
            try:
                r = win.grab_keyboard(True, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
                grabbed = (r == X.GrabSuccess)
            except Exception:
                grabbed = False
            if not grabbed:
                win.set_input_focus(X.RevertToParent, X.CurrentTime)
            d.sync()
            time.sleep(0.15)
            for keysym in (XK.XK_a, XK.XK_space, XK.XK_Return):
                res = _inject_and_check(d, win, keysym)
                if res:
                    ok = True
                    break
            try:
                d.ungrab_keyboard(X.CurrentTime)
                d.sync()
            except Exception:
                pass
            if ok:
                break
    finally:
        try:
            win.destroy()
            d.sync()
        except Exception:
            pass

    print("input-selftest: %s" % ("PASS" if ok else "FAIL (XTEST key not delivered)"),
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
