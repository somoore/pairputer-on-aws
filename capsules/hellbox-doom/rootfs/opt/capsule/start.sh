#!/usr/bin/env bash
# Capsule supervisor. /ready stays 503 until DOOM has rendered.
set -u
LOG() { echo "[capsule] $*" >&2; }
READY_FLAG=/run/capsule.ready
rm -f "$READY_FLAG" 2>/dev/null || true

# Readiness responder on :9000.
cat > /opt/hook.py <<'PY'
import os, sys, json
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
FLAG = "/run/capsule.ready"
# Service-log readback over the loopback readiness port. Served UNCONDITIONALLY (the relay ships these to
# CloudWatch for durable runtime logs) — safe because :9000 is loopback-only, the VM sits behind the
# gateway's IAM auth, and these files never contain secrets.
DBG_FILES = {
    "/dbg/input": "/home/app/app/input_dbg.log",
    "/dbg/focus": "/tmp/focus.log",
    "/dbg/inputws": "/var/log/input_ws.log",
    "/dbg/selftest": "/var/log/input_selftest.log",
}
class H(BaseHTTPRequestHandler):
    def _resp(self):
        ready = os.path.exists(FLAG); code = 200 if ready else 503
        b = b'{"status":"ok"}' if ready else b'{"status":"starting"}'
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers()
        try: self.wfile.write(b)
        except Exception: pass
    def _dbg(self, path, qs):
        # Incremental tail: ?offset=N -> {size, data} for bytes [offset, size). offset>size resets to 0.
        fp = DBG_FILES[path]
        try:
            size = os.path.getsize(fp)
            off = int((qs.get("offset") or ["0"])[0])
            if off < 0 or off > size:
                off = 0
            with open(fp, "rb") as fh:
                fh.seek(off)
                chunk = fh.read(1_000_000)
            body = json.dumps({"size": size, "offset": off,
                               "data": chunk.decode("utf-8", "replace")}).encode()
        except FileNotFoundError:
            body = json.dumps({"size": 0, "offset": 0, "data": ""}).encode()
        except Exception as exc:
            body = json.dumps({"error": repr(exc)}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in DBG_FILES: return self._dbg(u.path, parse_qs(u.query))
        self._resp()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n: self.rfile.read(n)
        self._resp()
    def log_message(self, *a): pass
# AWS probes the ready hook at http://127.0.0.1:9000/... (loopback). Bind loopback
# only so :9000 is never part of the externally reachable surface — it is internal
# and must NOT be added to the minted token's allowedPorts.
ThreadingHTTPServer(("127.0.0.1", 9000), H).serve_forever()
PY
python3.11 /opt/hook.py & LOG "hook responder :9000 (503 until ready) pid $!"

# Display stack.
export HOME=/root USER=root
rm -f /tmp/.X11-unix/X1 /tmp/.X1-lock 2>/dev/null || true
# Match Chocolate Doom's 640x400 window.
Xvnc :1 -geometry 640x400 -depth 24 -SecurityTypes None -rfbport 5901 \
     -AlwaysShared -desktop capsule > /var/log/xvnc.log 2>&1 &
LOG "Xvnc starting (pid $!)"
for _ in $(seq 1 100); do [ -S /tmp/.X11-unix/X1 ] && break; sleep 0.2; done
export DISPLAY=:1
sleep 1
xsetroot -solid black 2>/dev/null || true

websockify --web=/opt/novnc 0.0.0.0:6901 localhost:5901 > /var/log/websockify.log 2>&1 &
LOG "websockify :6901 -> 5901 (pid $!)"

# H.264 video on :6903.
DISPLAY=:1 python3.11 /opt/capsule/video_ws.py 2>&1 | tee -a /var/log/video_ws.log &
LOG "video_ws (H.264) :6903 (pid $!)"
# input_ws in a restart loop: if it ever dies (e.g. an X hiccup), it must come back,
# because a dead input_ws means keyboard/mouse silently stop working for the whole VM.
( while :; do
    DISPLAY=:1 python3.11 /opt/capsule/input_ws.py 2>&1 | tee -a /var/log/input_ws.log
    echo "[input_ws] exited rc=$? -- restarting in 1s" | tee -a /var/log/input_ws.log
    sleep 1
  done ) &
LOG "input_ws (XTEST) :6904 restart-loop pid $!"
DISPLAY_OK=0
for _ in $(seq 1 50); do
  curl -sf -o /dev/null --max-time 2 http://127.0.0.1:6901/vnc.html && { DISPLAY_OK=1; break; }
  sleep 0.2
done
LOG "display: websockify serving on :6901 (vnc.html reachable=$DISPLAY_OK)"

# Audio + app stack.
install -d -o app -g app /run/user/1000 2>/dev/null || true
runuser -u app -- bash /opt/capsule/run_app.sh &
LOG "run_app.sh launched as 'app' (pid $!)"

# Readiness gate: display up + DOOM rendered.
audio_bytes() {
  runuser -u app -- env XDG_RUNTIME_DIR=/run/user/1000 \
    timeout 3 parec --format=s16le --rate=44100 --channels=2 -d capsule.monitor 2>/dev/null | wc -c
}
port_up() { python3.11 -c 'import socket,sys; socket.create_connection(("127.0.0.1",int(sys.argv[1])),2).close()' "$1" 2>/dev/null; }
# Mean brightness of a center crop at the current X size.
render_mean() {
  DISPLAY=:1 python3.11 -c 'import sys
from Xlib import display, X
try:
    s = display.Display(":1").screen()
    W = int(s.width_in_pixels); H = int(s.height_in_pixels)
    cw = min(W, 640); ch = min(H, 480); x0 = (W - cw) // 2; y0 = (H - ch) // 2
    b = s.root.get_image(x0, y0, cw, ch, X.ZPixmap, 0xffffffff).data
    print(sum(b) // max(1, len(b)))
except Exception:
    print(0)' 2>/dev/null
}
RENDER_MIN=20
display_up() { curl -sf -o /dev/null --max-time 2 http://127.0.0.1:6901/vnc.html; }

# Input self-test: inject a key via XTEST and confirm it is delivered to an X client.
# Proves the exact mechanism DOOM needs works, so a flaky build where XTEST silently
# no-ops (the failure we hit: identical source, dead input) is caught. The self-test
# talks to the X server (:1) directly, so it does NOT depend on the loopback port_up
# probe (which is unreliable in the image-build sandbox).
#
# ENFORCE flag: when PAIRPUTER_INPUT_SELFTEST_ENFORCE is truthy, a failing self-test
# keeps the gate at 503 so the BUILD FAILS instead of shipping broken input. When not
# enforcing, the result is logged only (used to validate the signal before making it a
# hard gate). Render alone stays required either way.
INPUT_SELFTEST_LOG=/var/log/input_selftest.log
case "$(printf '%s' "${PAIRPUTER_INPUT_SELFTEST_ENFORCE:-}" | tr 'A-Z' 'a-z')" in
  1|true|yes|on) SELFTEST_ENFORCE=1 ;;
  *) SELFTEST_ENFORCE=0 ;;
esac
input_verified() {
  DISPLAY=:1 python3.11 /opt/capsule/input_selftest.py 2>>"$INPUT_SELFTEST_LOG"
}

# Stop before the ready-hook timeout.
NOW=$(date +%s); DEADLINE=$((NOW + 520))
DISPLAY_OK=0; AUDIO_OK=0; SVC_OK=0; MEAN=0; RENDER_SEEN=0; INPUT_OK=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  display_up && DISPLAY_OK=1
  [ "$(audio_bytes)" -gt 0 ] 2>/dev/null && AUDIO_OK=1
  if port_up 6902 && port_up 6903 && port_up 6904; then SVC_OK=1; else SVC_OK=0; fi
  MEAN=$(render_mean); [ -n "$MEAN" ] || MEAN=0
  # Latch once a bright frame proves DOOM rendered.
  [ "${MEAN:-0}" -ge "$RENDER_MIN" ] && RENDER_SEEN=1
  if [ "$INPUT_OK" != 1 ] && [ "$RENDER_SEEN" = 1 ]; then
    if input_verified; then INPUT_OK=1; else INPUT_OK=0; fi
  fi
  LOG "gate: display=$DISPLAY_OK audio=$AUDIO_OK services(6902/3/4)=$SVC_OK render_mean=$MEAN render_seen=$RENDER_SEEN input_ok=$INPUT_OK enforce=$SELFTEST_ENFORCE"
  # Render proves DOOM drew. Input verification is required only when enforcing.
  INPUT_GATE=1
  [ "$SELFTEST_ENFORCE" = 1 ] && [ "$INPUT_OK" != 1 ] && INPUT_GATE=0
  if [ "$DISPLAY_OK" = 1 ] && [ "$RENDER_SEEN" = 1 ] && [ "$INPUT_GATE" = 1 ]; then
    touch "$READY_FLAG"
    LOG "READY: display + DOOM rendered (mean=$MEAN) input_ok=$INPUT_OK enforce=$SELFTEST_ENFORCE; audio=$AUDIO_OK; /ready -> 200"
    break
  fi
  sleep 2
done
[ -f "$READY_FLAG" ] || LOG "STILL NOT READY after 520s: display=$DISPLAY_OK audio=$AUDIO_OK services=$SVC_OK render_seen=$RENDER_SEEN input_ok=$INPUT_OK enforce=$SELFTEST_ENFORCE -- leaving 503 so the build FAILS rather than snapshotting a broken capsule"

while :; do sleep 30; done
