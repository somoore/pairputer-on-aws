#!/usr/bin/env bash
# Runs PulseAudio, audio_ws, and Chocolate Doom as the app user.
set -u
LOG() { echo "[app] $*" >&2; }

export XDG_RUNTIME_DIR=/run/user/1000
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export HOME=/home/app
APPDIR=/home/app/app
# PulseAudio writes its cookie + runtime state under $HOME/.config/pulse; without a writable
# $HOME/.config the daemon can exit at startup. Workbench session.sh mkdir's this before pulse;
# doom's run_app never did — a real doom-vs-workbench gap. Best-effort (app-owned HOME).
mkdir -p "$HOME/.config/pulse" "$HOME/.local/state" 2>/dev/null || true

# PulseAudio null-sink, supervised. A one-shot `--start` that loses the boot
# race (or a daemon that dies later) left the capsule silent for its whole
# lifetime: parec got Connection refused and DOOM's SDL initialized with no
# audio backend. Same failure shape as the input_ws Xvnc race — the fix is the
# same: wait until it's really up, and keep a watchdog on it.
PA_LOG=/tmp/pa_dbg.log
pulse_up() {
  # Mirror workbench session.sh's PROVEN daemon start EXACTLY: `pulseaudio --daemonize=yes
  # --exit-idle-time=-1 --log-target=stderr` with stderr to a FILE (2>>$PA_LOG), NOT a `| sed`
  # pipe. The pipe was the bug: it detached the daemonized child's stderr so its own death reason
  # never surfaced, and piping a double-forked daemon's stderr is fragile. --daemonize's parent
  # exits 0 the instant it forks, so its rc is meaningless — we TRUST `pactl info`, not the exit
  # code. /dbg reads $PA_LOG so a daemon that won't stay up finally NAMES why.
  if ! pactl info >/dev/null 2>&1; then
    echo "--- pulseaudio --daemonize $(date -u +%FT%TZ) ---" >>"$PA_LOG" 2>/dev/null || true
    pulseaudio --daemonize=yes --exit-idle-time=-1 --log-target=stderr >>"$PA_LOG" 2>&1 || true
  fi
  for _ in $(seq 1 15); do pactl info >/dev/null 2>&1 && break; sleep 1; done
  if ! pactl info >/dev/null 2>&1; then
    LOG "pulse_up: daemon NOT listening after start (see /dbg pa log)"; tail -n 20 "$PA_LOG" 2>/dev/null | sed 's/^/[pa] /' >&2 || true
    return 1
  fi
  if ! pactl list short sinks 2>/dev/null | grep -q 'capsule'; then
    pactl load-module module-null-sink sink_name=capsule rate=48000 channels=2 sink_properties=device.description=capsule >/dev/null 2>&1 || LOG "null-sink load FAILED"
  fi
  pactl set-default-sink capsule >/dev/null 2>&1 || true
}
pulse_up || LOG "pulse_up failed at boot -- watchdog will keep retrying"
( while :; do
    sleep 5
    pactl info >/dev/null 2>&1 && continue
    LOG "pulse watchdog: pulseaudio down -- restarting"
    # ponytail: restores pulse + sink for new parec pipelines; a DOOM that was
    # ALREADY running silent keeps its dead SDL backend until its next restart.
    pulse_up || true
  done ) &
LOG "pulse watchdog started (pid $!)"

# Audio WebSocket.
python3.11 /opt/capsule/audio_ws.py > /tmp/audiows.log 2>&1 &
LOG "audio_ws :6902 pid $!"

export DISPLAY=:1
export SDL_VIDEODRIVER=x11
export SDL_AUDIODRIVER=pulse

# Locate the staged IWAD.
cd "$APPDIR" || { LOG "FATAL: app dir $APPDIR missing"; exit 1; }
WAD="$(find "$APPDIR" -maxdepth 1 -type f \( -name '*.wad' -o -name '*.WAD' \) | sort | head -1)"
[ -n "$WAD" ] || { LOG "FATAL: no .wad in $APPDIR"; LOG "contents: $(find "$APPDIR" -maxdepth 1 -print 2>&1 | tr '\n' '|')"; exit 1; }
LOG "iwad: $WAD"

# Launch RESTful-DOOM (agent mode) in a restart loop. Same Chocolate Doom lineage, plus the
# in-process protobuf/gRPC DoomAgent service on loopback :50051 (-agentport) and the legacy
# HTTP+JSON API on loopback :6666 (-apiport). The human streams the SAME live game via the
# Xvnc/:6903 path; the agent observes/acts via gRPC through agent_bridge.py (:6905).
#
# -warp 1 1 boots STRAIGHT INTO E1M1 (skip the title/menu) so neither the human nor the agent lands on
# a menu and has to navigate "New Game" before anything is playable — they both drop into a live level.
# PAIRPUTER_DOOM_WARP overrides the "<episode> <map>" (default "1 1"); PAIRPUTER_DOOM_SKILL sets difficulty.
WARP="${PAIRPUTER_DOOM_WARP:-1 1}"
SKILL="${PAIRPUTER_DOOM_SKILL:-3}"
( while :; do
    # SDL grabs its audio backend once at init: launching against a dead pulse
    # means a silent DOOM until the next restart. Wait (bounded) for pulse so
    # sound comes up with the game; a truly broken pulse still lets DOOM start.
    for _ in $(seq 1 20); do pactl info >/dev/null 2>&1 && break; sleep 1; done
    pactl info >/dev/null 2>&1 || LOG "WARNING: launching DOOM without pulse -- audio will be silent until next DOOM restart"
    # RESTful Doom renders native 320x200 into Xvnc; video_ws.py upscales the outgoing H.264 stream
    # to Hellbox's 640x400 so the browser-facing resolution matches without black margins.
    LOG "launching: restful-doom -iwad $WAD -warp $WARP -skill $SKILL -nogui -agentport 50051 -apiport 6666"
    /usr/local/bin/restful-doom -iwad "$WAD" -fullscreen -nograbmouse -nogui \
      -warp $WARP -skill "$SKILL" \
      -agentport 50051 -apiport 6666 2>&1 | sed 's/^/[doom] /' >&2
    LOG "doom exited rc=$? -- restarting in 2s"
    sleep 2
  done ) &
APP_LOOP=$!
LOG "doom restart loop pid $APP_LOOP"

# Keep keyboard focus on the game window.
DISPLAY=:1 python3.11 /opt/capsule/focus.py > /tmp/focus.log 2>&1 &
LOG "input-focus asserter started (pid $!)"

# Render watchdog.
render_peek() {
  DISPLAY=:1 python3.11 -c 'from Xlib import display, X
try:
    s = display.Display(":1").screen()
    W = int(s.width_in_pixels); H = int(s.height_in_pixels)
    cw = min(W, 640); ch = min(H, 480); x0 = (W - cw) // 2; y0 = (H - ch) // 2
    b = s.root.get_image(x0, y0, cw, ch, X.ZPixmap, 0xffffffff).data
    print(sum(b) // max(1, len(b)))
except Exception:
    print(0)' 2>/dev/null
}
( RENDERED=0; BLACK=0
  while :; do
    sleep 12
    [ "$RENDERED" = 1 ] && continue
    M=$(render_peek)
    if [ "${M:-0}" -ge 20 ]; then RENDERED=1; LOG "WATCHDOG: doom rendered (mean=$M) -- standing down"; continue; fi
    BLACK=$((BLACK + 1))
    if [ "$BLACK" -ge 4 ] && pgrep -x restful-doom >/dev/null 2>&1; then
      LOG "WATCHDOG: restful-doom alive but black ~48s -- killing to re-roll render"
      pkill -x restful-doom 2>/dev/null; BLACK=0; sleep 2
    fi
  done ) &
LOG "render watchdog started"

# Heartbeat.
while :; do
  if pgrep -x restful-doom >/dev/null 2>&1; then ALIVE=yes; else ALIVE=NO; fi
  LOG "heartbeat: restful-doom alive=$ALIVE"
  sleep 20
done
