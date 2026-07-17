#!/usr/bin/env python3.11
"""Input WebSocket on :6904.

Receives JSON keyboard/mouse events and injects them into X display :1 via XTEST.

CO-PLAY ARBITRATION (interaction.md Phase 4): the human (relay) and the agent (via the capsule's
agent bridge) both send events here — this is the single injection chokepoint, so coordination lives
here and is automatic for EVERY capsule. Rules, in priority order:
  1. THE HUMAN ALWAYS WINS. A human event injects immediately, seizes focus, and revokes any agent
     grant *mid-action* — the agent can never lock the human out.
  2. After human activity, the agent is blocked for AGENT_COOLDOWN_S (it "yields"), then may act again.
  3. The agent injects only while it holds focus (human idle past the cooldown); otherwise its event is
     dropped and reported, so the model learns it must wait. Every event is attributed (actor=human|agent).
Grant state is published to :6906/state (JSON) for the widget's ghost cursor + whose-turn indicator.
"""
import asyncio
import hmac
import http.server
import json
import logging
import os
import sys
import threading
import time

import websockets
from Xlib import X, display, XK
from Xlib.ext import xtest

# Health-check probes (loopback TCP connect + immediate close) make the websockets server log a full
# handshake-EOF traceback per probe — pure noise that buries real events in the runtime logs. Silence
# the library's failed-handshake chatter; genuine errors still surface via our own LOG/AUDIT.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

PORT = 6904
STATE_PORT = 6906  # read-only coordination state for the widget (ghost cursor / whose turn)

# How long the agent must yield after any human input. Short: the human grabs the wheel, the agent
# resumes quickly once the human is idle. Env-tunable per deployment.
AGENT_COOLDOWN_S = float(os.environ.get("PAIRPUTER_AGENT_COOLDOWN_S", "0.6"))
# How long an agent grant lasts without renewal (so a stalled agent doesn't hold focus forever).
AGENT_GRANT_TTL_S = float(os.environ.get("PAIRPUTER_AGENT_GRANT_TTL_S", "2.0"))
FEED_MAX = int(os.environ.get("PAIRPUTER_ACTION_FEED_MAX", "8"))  # theatre-of-work: recent agent actions kept
# Actor trust (interaction.md defense-in-depth): ONLY the agent bridge — in-VM, able to read this
# root-owned key file — may label events actor=agent. A connection proves it by sending
# {"t":"auth","key":<key>} first; every other connection has its actor FORCED to "human" (which is
# definitionally correct: the relay channel IS the human's channel). No key file -> nothing can be agent.
AGENT_KEY_FILE = os.environ.get("PAIRPUTER_AGENT_KEY_FILE", "/run/pairputer/agent-input.key")


def _agent_key() -> str:
    try:
        with open(AGENT_KEY_FILE) as fh:
            return fh.read().strip()
    except Exception:
        return ""


class Arbiter:
    """Human-always-wins focus arbitration between the human and the agent."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_human = 0.0     # last human event time
        self._agent_until = 0.0    # agent holds focus until this time (renewed while it acts)
        self._agent_dropped = 0    # count of agent events refused because the human was active
        self._feed = []            # rolling "theatre of work" — recent agent actions the human can see
        self._seq = 0
        self._agent_cursor = None  # (x, y, t) of the agent's LAST pointer move — the ghost cursor

    def on_human(self):
        """A human event arrived: injects unconditionally, seizes focus, revokes any agent grant."""
        with self._lock:
            self._last_human = time.time()
            self._agent_until = 0.0  # revoke the agent's grant instantly, mid-action
        return True

    def allow_agent(self):
        """May an agent event inject right now? Only if the human has been idle past the cooldown."""
        now = time.time()
        with self._lock:
            if now - self._last_human < AGENT_COOLDOWN_S:
                self._agent_dropped += 1
                return False
            self._agent_until = now + AGENT_GRANT_TTL_S  # (re)grant the agent a short turn
            return True

    def note_agent_action(self, label, grant=True):
        """Register that the agent acted (label = human-readable, e.g. 'fired shotgun'). Feeds the
        theatre-of-work. Actions that DON'T flow through input_ws (the gRPC doom_act path) call this so
        the human still sees them AND the whose-turn indicator lights for BOTH agent paths."""
        now = time.time()
        with self._lock:
            self._seq += 1
            self._feed.append({"seq": self._seq, "t": now, "label": str(label)[:120]})
            self._feed = self._feed[-FEED_MAX:]
            if grant and now - self._last_human >= AGENT_COOLDOWN_S:
                self._agent_until = now + AGENT_GRANT_TTL_S  # light 'agent driving' for the gRPC path too
            return self._seq

    def note_agent_cursor(self, x, y):
        """The agent moved the pointer: remember where, so the widget can render a ghost cursor
        (interaction.md: the agent's pointer rendered distinctly — the human SEES where it acts)."""
        with self._lock:
            self._agent_cursor = (int(x), int(y), time.time())

    def owner(self):
        now = time.time()
        with self._lock:
            if now - self._last_human < AGENT_COOLDOWN_S:
                return "human"
            if now < self._agent_until:
                return "agent"
            return "idle"

    def snapshot(self):
        now = time.time()
        with self._lock:
            return {
                "owner": ("human" if now - self._last_human < AGENT_COOLDOWN_S
                          else "agent" if now < self._agent_until else "idle"),
                "humanActiveMsAgo": int((now - self._last_human) * 1000) if self._last_human else None,
                "agentGrantMsLeft": max(0, int((self._agent_until - now) * 1000)),
                "agentDropped": self._agent_dropped,
                "cooldownMs": int(AGENT_COOLDOWN_S * 1000),
                # theatre of work: recent agent actions (newest last), relative age in ms.
                "feed": [{"seq": f["seq"], "label": f["label"], "ageMs": int((now - f["t"]) * 1000)}
                         for f in self._feed],
                # ghost cursor: where the agent last moved the pointer (display px), age in ms.
                "agentCursor": ({"x": self._agent_cursor[0], "y": self._agent_cursor[1],
                                 "ageMs": int((now - self._agent_cursor[2]) * 1000)}
                                if self._agent_cursor else None),
            }


ARBITER = Arbiter()


def _serve_state():
    """Coordination endpoint: GET / -> whose-turn + action feed (widget polls it); POST /note {label}
    lets the MCP server register a gRPC agent action (which never passes through input_ws) so the human
    sees it and the indicator lights for both agent paths."""
    arb = ARBITER

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self._json(200, arb.snapshot())

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                seq = arb.note_agent_action(body.get("label", "agent action"), grant=body.get("grant", True))
                self._json(200, {"ok": True, "seq": seq})
            except Exception as exc:
                self._json(400, {"error": str(exc)})

        def _json(self, code, payload):
            b = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    try:
        http.server.ThreadingHTTPServer(("0.0.0.0", STATE_PORT), H).serve_forever()
    except Exception as exc:  # never let the state endpoint take down input
        AUDIT("state endpoint failed: %r" % (exc,))


def _connect_display(name=":1", attempts=150, delay=0.2):
    """Connect to the X server, retrying until Xvnc is accepting connections.

    input_ws is launched moments after Xvnc, so a bare display.Display(":1") at
    import time can lose the startup race and raise DisplayConnectionError. That
    used to kill the process permanently -> input silently dead for the whole VM
    lifetime (the intermittent 'keyboard/mouse do nothing' bug). Retry instead.
    """
    last = None
    for _ in range(attempts):
        try:
            return display.Display(name)
        except Exception as exc:  # DisplayConnectionError and friends
            last = exc
            time.sleep(delay)
    raise last if last else RuntimeError("could not connect to X display")


d = _connect_display(":1")

# Verbose per-event diagnostics are opt-in via PAIRPUTER_DEBUG so production stays quiet.
# When on, log to stderr and to a file the :9000 hook can serve back, since MicroVM
# runtime stderr does not currently reach CloudWatch.
DEBUG = os.environ.get("PAIRPUTER_DEBUG", "").lower() in ("1", "true", "yes", "on")
_DBG_FILE = "/home/app/app/input_dbg.log"


def LOG(*a):
    if not DEBUG:
        return
    line = "[input_ws] " + " ".join(str(x) for x in a)
    print(line, file=sys.stderr, flush=True)
    try:
        with open(_DBG_FILE, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def AUDIT(*a):
    """Security/lifecycle events that must reach the runtime logs UNCONDITIONALLY (not DEBUG-gated):
    agent auth refusals, connections, startup. Low-volume by design — per-event chatter stays in LOG.
    start.sh tees stderr to CloudWatch, so these are the capsule's audit trail."""
    line = "[input_ws] " + " ".join(str(x) for x in a)
    print(line, file=sys.stderr, flush=True)
    if DEBUG:
        try:
            with open(_DBG_FILE, "a") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


if DEBUG:
    # If XTEST is missing, xtest.fake_input silently no-ops and DOOM never sees input.
    try:
        LOG("startup: XTEST present=%s display=%s"
            % (bool(d.query_extension("XTEST")), d.get_display_name()))
    except Exception as exc:
        LOG("startup: query XTEST failed: %r" % (exc,))

# Non-printable JS key names.
SPECIAL_KEYS = {
    "ArrowUp": XK.XK_Up,
    "ArrowDown": XK.XK_Down,
    "ArrowLeft": XK.XK_Left,
    "ArrowRight": XK.XK_Right,
    "Control": XK.XK_Control_L,
    "Alt": XK.XK_Alt_L,
    "Shift": XK.XK_Shift_L,
    "Meta": XK.XK_Super_L,
    " ": XK.XK_space,
    "Enter": XK.XK_Return,
    "Escape": XK.XK_Escape,
    "Tab": XK.XK_Tab,
    "Backspace": XK.XK_BackSpace,
}

_event_count = 0


def _focus_name():
    try:
        f = d.get_input_focus().focus
        if f in (X.PointerRoot, X.NONE, 0):
            return "<root/none:%r>" % (f,)
        try:
            nm = f.get_wm_name()
        except Exception:
            nm = None
        return "%r name=%r" % (f, nm)
    except Exception as exc:
        return "<focus-error %r>" % (exc,)


def _keysym_for(key):
    """Map a JS key name to an X keysym."""
    if key in SPECIAL_KEYS:
        return SPECIAL_KEYS[key]
    if isinstance(key, str) and len(key) == 1:
        ks = XK.string_to_keysym(key)
        if ks == 0 and key.isupper():
            ks = XK.string_to_keysym(key.lower())
        return ks
    return 0


def _handle_key(msg):
    key = msg.get("key")
    keysym = _keysym_for(key)
    if not keysym:
        LOG("key %r -> no keysym (dropped)" % (key,))
        return
    kc = d.keysym_to_keycode(keysym)
    if not kc:
        LOG("key %r keysym=%s -> no keycode (dropped)" % (key, keysym))
        return
    evt = X.KeyPress if msg.get("down") else X.KeyRelease
    xtest.fake_input(d, evt, kc)
    d.sync()
    if DEBUG:
        LOG("key %r down=%s keysym=%s kc=%s injected; focus=%s"
            % (key, msg.get("down"), keysym, kc, _focus_name()))


def _handle_move(msg):
    xtest.fake_input(d, X.MotionNotify, x=int(msg.get("x", 0)), y=int(msg.get("y", 0)))
    d.sync()


def _handle_button(msg):
    button = int(msg.get("button", 0)) + 1
    evt = X.ButtonPress if msg.get("down") else X.ButtonRelease
    xtest.fake_input(d, evt, button)
    d.sync()
    if DEBUG:
        LOG("button %s down=%s injected; focus=%s" % (button, msg.get("down"), _focus_name()))


def _inject(msg):
    """Dispatch one input event to the XTEST injectors. Capsule-agnostic — works for any workload."""
    t = msg.get("t")
    if t == "k":
        _handle_key(msg)
    elif t == "m":
        _handle_move(msg)
    elif t == "b":
        _handle_button(msg)
    else:
        LOG("unknown event type %r" % (t,))


def _arbitrate_and_inject(msg):
    """Apply co-play arbitration, then inject if allowed. Returns (injected, owner).

    actor defaults to 'human' (the relay path historically sends no actor field; the agent bridge
    always tags actor='agent'). Human events always inject and seize focus; agent events inject only
    when the human is idle past the cooldown, else they're dropped and the agent is told to wait.
    """
    actor = msg.get("actor", "human")
    if actor == "agent":
        if not ARBITER.allow_agent():
            if DEBUG:
                LOG("agent event dropped (human active); owner=%s" % (ARBITER.owner(),))
            return False, ARBITER.owner()
        _inject(msg)
        if msg.get("t") == "m":  # ghost cursor: the human sees where the agent points
            ARBITER.note_agent_cursor(msg.get("x", 0), msg.get("y", 0))
        return True, "agent"
    # Human (or unlabeled): always wins.
    ARBITER.on_human()
    _inject(msg)
    return True, "human"


async def handler(ws):
    global _event_count
    peer = getattr(ws, "remote_address", None)
    agent_conn = False  # this connection proved it is the agent bridge (see AGENT_KEY_FILE)
    AUDIT("client connected peer=%r" % (peer,))
    try:
        async for raw in ws:
            _event_count += 1
            try:
                msg = json.loads(raw)
                if msg.get("t") == "auth":  # bridge handshake: authenticate this connection as agent
                    key = _agent_key()
                    agent_conn = bool(key) and hmac.compare_digest(str(msg.get("key", "")), key)
                    if not agent_conn:
                        AUDIT("agent auth REFUSED peer=%r" % (peer,))
                    continue
                if not agent_conn:
                    # Only the authenticated bridge may claim actor=agent. Anything else on this
                    # socket is the human's channel by definition — force it, never trust the field.
                    msg["actor"] = "human"
                _arbitrate_and_inject(msg)
            except Exception as exc:
                LOG("handle error %r on raw=%r" % (exc, str(raw)[:120]))
    finally:
        AUDIT("client disconnected peer=%r total_events=%s" % (peer, _event_count))


async def main():
    # Coordination-state endpoint (widget polls it for ghost cursor / whose turn). Daemon thread so it
    # can never block or outlive input injection.
    threading.Thread(target=_serve_state, daemon=True).start()
    AUDIT("listening on 127.0.0.1:%s (state on :%s, agent cooldown %.2fs)" % (PORT, STATE_PORT, AGENT_COOLDOWN_S))
    async with websockets.serve(handler, "127.0.0.1", PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
