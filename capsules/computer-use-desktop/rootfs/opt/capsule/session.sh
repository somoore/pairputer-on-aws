#!/usr/bin/env bash
set -euo pipefail
export DISPLAY=:1 XAUTHORITY="${XAUTHORITY:-/run/pairputer/xauthority}" HOME=/home/app USER=app XDG_RUNTIME_DIR=/run/user/1000
export NO_AT_BRIDGE=0 GTK_MODULES="${GTK_MODULES:-}:atk-bridge"
export XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 GSK_RENDERER=cairo LIBGL_ALWAYS_SOFTWARE=1
mkdir -p "$HOME/.config/chromium" "$HOME/.local/state/pairputer" "$HOME/workspace"
# Point the developer's dev state (git identity, ssh keys, editor settings, their
# project tree) at workspace/persistent/ so it survives a trashed VM. Best-effort:
# a failure here must not stop the desktop session from coming up.
/opt/capsule/persistent-home.sh || echo "[pairputer-session] persistent-home wiring skipped" >&2
env | grep -E '^(DBUS_SESSION_BUS_ADDRESS|AT_SPI_BUS_ADDRESS)=' > /run/user/1000/session.env || true
pulseaudio --daemonize=yes --exit-idle-time=-1 --log-target=stderr >/dev/null 2>&1
pactl load-module module-null-sink sink_name=capsule sink_properties=device.description=Capsule >/dev/null || true
pactl set-default-sink capsule || true
mutter --x11 --replace --sm-disable --display=:1 >/dev/null 2>&1 &
sleep 2
# Diagnostic output goes to STDOUT/STDERR — start.sh pipes it through bounded_log to a root-owned
# /var/log/pairputer-session.log (readable via PAIRPUTER_DEBUG /vmdbg?f=session). Records launcher
# stderr + a window dump so the desktop layout can be debugged without shell access.
echo "[session] start $(date -u +%FT%TZ) DISPLAY=$DISPLAY"
# Human-facing app launcher DOCK across the top (the desktop has no panel/icons otherwise — it's
# AI-driven via apps_open). Start it FIRST and give it a beat to reserve its strut so apps that open
# later tile below it. Guarded restart so a crash self-heals; best-effort, never blocks the session.
# NOTE: python3 (system 3.9), NOT python3.11 — PyGObject (gi) is the python3-gobject RPM which targets
# 3.9 only; 3.11 has no gi (this is why the dock silently never showed: it crashed on `import gi` 3x/sec
# the whole session). Xlib (3.11-only, for the strut) is imported defensively in the launcher, so on 3.9
# the strut is skipped and the dock still shows via its DOCK hint + keep_above.
( while :; do echo "[launcher] (re)start"; python3 /opt/capsule/launcher-panel.py 2>&1 || echo "[launcher] exited $?"; sleep 3; done ) &
sleep 1
# One-shot window dump ~8s in, once apps have mapped — geometry of every top-level window, so we can
# see whether the dock rendered and where Chromium landed.
( sleep 8; python3.11 - <<'PYDUMP' 2>&1 || true
from Xlib import display
d=display.Display(":1"); r=d.screen().root
print("[windows] screen %dx%d"%(d.screen().width_in_pixels,d.screen().height_in_pixels))
for w in r.query_tree().children:
    try:
        g=w.get_geometry(); a=w.get_attributes()
        if a.map_state!=2: continue  # only viewable
        nm=""
        try: nm=(w.get_wm_name() or "")
        except Exception: pass
        t=w.translate_coords(r,0,0)
        print("[win] '%s' pos=(%d,%d) size=%dx%d"%(nm[:40], getattr(t,'x',0), getattr(t,'y',0), g.width, g.height))
    except Exception as e:
        print("[win] err",e)
PYDUMP
) &
# Open the file manager as a friendly default surface. The browser/editor/VS Code are NOT auto-opened
# at boot — a maximized browser on about:blank hid the whole desktop (and the dock); the human opens
# what they want from the dock, and the AI opens apps via apps_open on demand.
nautilus --no-desktop >/dev/null 2>&1 &
# code-server (VS Code in the browser) on a loopback preview port. Bound to
# 127.0.0.1 only and reached through visible Chromium; the whole VM's auth
# boundary is the gate, so in-box auth is off. Guarded restart loop so a crash
# self-heals and never takes down the session (the start.sh service rule).
(
  while :; do
    if PASSWORD= /usr/local/bin/code-server \
         --bind-addr 127.0.0.1:"${PAIRPUTER_CODE_SERVER_PORT:-4500}" \
         --auth none --disable-telemetry --disable-update-check \
         "$HOME/workspace"; then rc=0; else rc=$?; fi
    echo "[pairputer-session] code-server exited rc=$rc; restarting" >&2
    sleep 2
  done
) &
# Chromium is NOT launched at boot — the browser must never auto-open. It starts ONLY on demand: the
# dock's Browser/VS Code buttons and the model's apps_open("browser") run pairputer-chromium then. It's
# single-instance, so repeat opens raise the existing window. Readiness no longer gates on Chromium
# (readiness.py), so nothing forces it up.
wait
