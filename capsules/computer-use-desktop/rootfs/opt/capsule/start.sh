#!/usr/bin/env bash
set -euo pipefail
log(){ echo "[computer-use-desktop] $*" >&2; }
export PYTHONPATH=/opt/capsule:/opt/capsule/desktopgen PAIRPUTER_CONTROL_STATE_DIR=/run/pairputer/control
export PAIRPUTER_STATE_DIR=/var/lib/pairputer
install -d -o root -g root -m 0755 /run/pairputer
install -d -o root -g pairputer-state -m 2775 /run/pairputer/control
install -d -o agent -g agent -m 0711 /run/pairputer/brain
install -d -o root -g root -m 0700 "$PAIRPUTER_STATE_DIR"
install -d -o agent -g agent -m 0700 /var/lib/pairputer-brain
install -d -o app -g app -m 0700 /run/user/1000 /home/app/.local/state/pairputer /home/app/.config/chromium
install -d -o app -g app -m 0770 /home/app/workspace
install -d -o terminal -g terminal -m 0700 /run/user/1001 /home/terminal
install -d -o job -g app -m 0710 /home/job
install -d -o root -g root -m 0000 /run/pairputer/job-empty-x11
install -d -o root -g egressd -m 0750 /run/pairputer/preview-grants
env PYTHONPATH="$PYTHONPATH" PAIRPUTER_CONTROL_STATE_DIR="$PAIRPUTER_CONTROL_STATE_DIR" \
  python3.11 -c 'import os; from services.control_state import ControlState; ControlState(os.environ["PAIRPUTER_CONTROL_STATE_DIR"])'
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > /run/pairputer/agent-input.key
chmod 0640 /run/pairputer/agent-input.key
chown root:pairputer-input /run/pairputer/agent-input.key
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > /run/pairputer/desktop-agent.key
chmod 0640 /run/pairputer/desktop-agent.key
chown root:pairputer-rpc /run/pairputer/desktop-agent.key
bridge_capability="${PAIRPUTER_BRIDGE_BOOTSTRAP_CAPABILITY:-}"
if ! [[ "$bridge_capability" =~ ^[A-Za-z0-9_-]{43,256}$ ]]; then
  bridge_capability="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
(umask 0137; printf '%s\n' "$bridge_capability" > /run/pairputer/bridge-ingress.key)
chmod 0640 /run/pairputer/bridge-ingress.key
chown root:agent /run/pairputer/bridge-ingress.key
unset bridge_capability PAIRPUTER_BRIDGE_BOOTSTRAP_CAPABILITY

# Untrusted agent-run build/test commands use uid 1005.  They retain ordinary
# internet and preview-port access but cannot reach any privileged desktop,
# media, hook, CDP, or broker listener.  The process service separately hides
# the X11 Unix socket in a private mount namespace.
protected_ports="5901,6001,6901,6902,6903,6904,6905,6906,6907,9000,9222,50051"
# Lambda's nft compatibility layer does not provide xt_owner. Put every
# untrusted job in a dedicated cgroup and match that cgroup in the OUTPUT hook;
# descendants inherit it, and the child joins before dropping privileges.
job_cgroup="/sys/fs/cgroup/pairputer-jobs"
job_cgroup_ready=0
if test -d /sys/fs/cgroup && mkdir -p "$job_cgroup" 2>/dev/null \
    && test -w "$job_cgroup/cgroup.procs"; then
  chmod 0750 "$job_cgroup" || { log "job cgroup permissions failed"; exit 1; }
  job_cgroup_ready=1
  export PAIRPUTER_JOB_CGROUP_PATH="$job_cgroup"
elif test "${PAIRPUTER_ALLOW_UID_FIREWALL:-false}" != true; then
  # AWS Lambda's minimal kernel may expose neither xt_owner nor xt_cgroup. In
  # that case every job receives a private network namespace (no loopback or
  # desktop/control-plane reachability) instead of running with ambient access.
  # This is intentionally offline; an explicit local-dev override is required
  # for the UID firewall path that retains internet access.
  export PAIRPUTER_ISOLATE_JOB_NETWORK=true
  log "job cgroup unavailable; jobs will run in an offline network namespace"
fi
if test "$job_cgroup_ready" = 1; then
  # Probe the actual xtables cgroup match before installing rules. Some AWS
  # kernels expose a writable cgroup filesystem but omit the match extension;
  # treat that as the offline-network case instead of aborting halfway through.
  if iptables -t filter -C OUTPUT -m cgroup --path "$PAIRPUTER_JOB_CGROUP_PATH" -j ACCEPT >/dev/null 2>&1; then
    cgroup_probe_rc=0
  else
    cgroup_probe_rc=$?
  fi
  if test "$cgroup_probe_rc" -eq 2; then
    job_cgroup_ready=0
    if test "${PAIRPUTER_ALLOW_UID_FIREWALL:-false}" != true; then
      export PAIRPUTER_ISOLATE_JOB_NETWORK=true
      log "xtables cgroup match unavailable; jobs will run in an offline network namespace"
    fi
  fi
  unset job_cgroup
fi
if test "$job_cgroup_ready" = 1 || test "${PAIRPUTER_ALLOW_UID_FIREWALL:-false}" = true; then
for firewall in iptables ip6tables; do
  command -v "$firewall" >/dev/null || { log "$firewall is required for job isolation"; exit 1; }
  # The minimal Lambda kernel may not expose an IPv6 filter table. That is safe
  # only when IPv6 is explicitly disabled; otherwise fail closed.
  if test "$firewall" = ip6tables && ! "$firewall" -t filter -L >/dev/null 2>&1; then
    test "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null || echo 0)" = 1 \
      || { log "IPv6 filter table is unavailable while IPv6 is enabled"; exit 1; }
    continue
  fi
  # The Lambda MicroVM network namespace may expose an empty nftables table
  # without the usual built-in OUTPUT chain. Create the chain before adding
  # the uid-scoped deny rules; ordinary Docker namespaces already have it.
  if test "$job_cgroup_ready" = 1; then
    "$firewall" -A OUTPUT -p tcp -m cgroup --path "$PAIRPUTER_JOB_CGROUP_PATH" \
      -m multiport --dports "$protected_ports" -j REJECT --reject-with tcp-reset \
      || { log "$firewall TCP cgroup job-isolation rule failed"; exit 1; }
    "$firewall" -A OUTPUT -p udp -m cgroup --path "$PAIRPUTER_JOB_CGROUP_PATH" \
      -m multiport --dports "$protected_ports" -j REJECT \
      || { log "$firewall UDP cgroup job-isolation rule failed"; exit 1; }
    "$firewall" -C OUTPUT -p tcp -m cgroup --path "$PAIRPUTER_JOB_CGROUP_PATH" \
      -m multiport --dports "$protected_ports" -j REJECT --reject-with tcp-reset \
      || { log "$firewall TCP cgroup job-isolation verification failed"; exit 1; }
    "$firewall" -C OUTPUT -p udp -m cgroup --path "$PAIRPUTER_JOB_CGROUP_PATH" \
      -m multiport --dports "$protected_ports" -j REJECT \
      || { log "$firewall UDP cgroup job-isolation verification failed"; exit 1; }
  else
    "$firewall" -A OUTPUT -p tcp -m owner --uid-owner 1005 -m multiport --dports "$protected_ports" \
      -j REJECT --reject-with tcp-reset || { log "$firewall TCP UID job-isolation rule failed"; exit 1; }
    "$firewall" -A OUTPUT -p udp -m owner --uid-owner 1005 -m multiport --dports "$protected_ports" \
      -j REJECT || { log "$firewall UDP UID job-isolation rule failed"; exit 1; }
    "$firewall" -C OUTPUT -p tcp -m owner --uid-owner 1005 -m multiport --dports "$protected_ports" \
      -j REJECT --reject-with tcp-reset || { log "$firewall TCP UID job-isolation verification failed"; exit 1; }
    "$firewall" -C OUTPUT -p udp -m owner --uid-owner 1005 -m multiport --dports "$protected_ports" \
      -j REJECT || { log "$firewall UDP UID job-isolation verification failed"; exit 1; }
  fi
done
fi
unset protected_ports firewall job_cgroup_ready
unset protected_ports firewall
# Air-gap the box's egress by default and keep it reconciled to the bridge's
# intent. This is SEPARATE from the job-isolation rules above (which only wall
# untrusted build jobs off the control plane): air-gap cuts the WHOLE box off
# from the internet, hot-toggleable from the widget/agent. Root-only; the bridge
# just writes /run/pairputer/brain/airgap.intent and this loop enforces it.
# Guarded so a failure can never trip set -e and kill the supervisor.
(while :;do if PAIRPUTER_AIRGAP_DEFAULT="${PAIRPUTER_AIRGAP_DEFAULT:-on}" \
   /opt/capsule/airgap-reconciler.sh;then rc=0;else rc=$?;fi;sleep 1;done)&
rm -f /tmp/.X11-unix/X1 /tmp/.X1-lock
install -d -o root -g root -m 1777 /tmp/.X11-unix
# Require MIT-MAGIC-COOKIE-1 authentication even though the X server is only
# guest-local.  The cookie is readable by the four trusted desktop principals
# and root, but not by the untrusted job user.  `-nolisten local` is also
# intentional: with TigerVNC/Xtrans it disables the network-namespace-wide
# abstract Unix socket while retaining the pathname socket which the per-job
# mount namespace hides.
XAUTHORITY=/run/pairputer/xauthority
x11_cookie="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
rm -f "$XAUTHORITY"
(umask 0177; xauth -f "$XAUTHORITY" add :1 MIT-MAGIC-COOKIE-1 "$x11_cookie")
chmod 0640 "$XAUTHORITY"
chown root:pairputer-x11 "$XAUTHORITY"
unset x11_cookie
export XAUTHORITY
# Lambda MicroVM image validation starts the capsule in a constrained boot
# environment where an unprivileged Xvnc cannot create its display socket.  The
# established Agent-Doom capsule therefore bootstraps Xvnc from the supervisor,
# then runs every client and semantic service under its dedicated UID.  Match
# that known-good MicroVM bootstrap exactly: the AWS runtime does not populate
# HOME/USER, and DISPLAY must not point at the server before its socket exists.
# Port
# 5901 is not routed by the relay or any host binding; only the authenticated
# noVNC proxy is reachable outside the guest.
export HOME=/root USER=root
Xvnc :1 -geometry "${PAIRPUTER_DISPLAY_GEOMETRY:-1440x900}" -depth 24 \
  -auth "$XAUTHORITY" -nolisten local \
  -SecurityTypes None -rfbport 5901 -AlwaysShared -desktop "Pairputer Workbench" \
  > /var/log/xvnc.log 2>&1 &
xvnc_pid=$!
log "Xvnc bootstrap pid=$xvnc_pid"
dump_xvnc_diagnostics(){
  ps -o pid,ppid,user,state,wchan:32,args -p "$xvnc_pid" >&2 || true
  test -r "/proc/$xvnc_pid/status" && sed -n '1,80p' "/proc/$xvnc_pid/status" >&2 || true
  test -r "/proc/$xvnc_pid/wchan" && { printf 'wchan=' >&2; cat "/proc/$xvnc_pid/wchan" >&2; } || true
  sed -n '1,200p' /var/log/xvnc.log >&2 || true
}
# A converted MicroVM image has substantially colder first-touch I/O than the
# equivalent local Docker layer.  Stay within the image Ready hook's 600-second
# budget, but do not misclassify a live, still-initializing X server as failed.
for attempt in $(seq 1 900);do
  test -S /tmp/.X11-unix/X1 && break
  kill -0 "$xvnc_pid" 2>/dev/null || break
  if test "$attempt" = 100 || test "$attempt" = 300; then
    log "Xvnc still initializing after $((attempt / 5))s"
    dump_xvnc_diagnostics
  fi
  sleep .2
done
if ! test -S /tmp/.X11-unix/X1; then
  log "Xvnc failed to create display :1"
  dump_xvnc_diagnostics
  kill "$xvnc_pid" 2>/dev/null || true
  sleep .2
  kill -KILL "$xvnc_pid" 2>/dev/null || true
  wait "$xvnc_pid" || true
  exit 1
fi
export DISPLAY=:1
xsetroot -solid '#20242b' || true
runuser -u app -- websockify --web=/opt/novnc 0.0.0.0:6901 127.0.0.1:5901 &
python3.11 /opt/capsule/readiness.py &
(
 while :;do
  runuser -u egressd -- env \
   PAIRPUTER_EGRESS_PROXY_PORT="${PAIRPUTER_EGRESS_PROXY_PORT:-6907}" \
   PAIRPUTER_ALLOW_LOCAL_PREVIEW="${PAIRPUTER_ALLOW_LOCAL_PREVIEW:-false}" \
   PAIRPUTER_PREVIEW_PORTS="${PAIRPUTER_PREVIEW_PORTS:-3000-5899,7000-8999}" \
   PAIRPUTER_BROWSER_REMOTE_PORTS="${PAIRPUTER_BROWSER_REMOTE_PORTS:-80,443}" \
   python3.11 /opt/capsule/egress_proxy.py
  sleep 1
 done
)&
# Chromium refuses to start until the dedicated proxy is healthy.  Waiting
# here avoids a permanent session-start race while preserving fail-closed
# behavior if the supervised proxy later exits.
for attempt in $(seq 1 50);do
 python3.11 -c 'import http.client; c=http.client.HTTPConnection("127.0.0.1",6907,timeout=.2); c.request("GET","/health"); r=c.getresponse(); r.read(); c.close(); raise SystemExit(0 if r.status==204 else 1)' \
  >/dev/null 2>&1 && break
 sleep .1
done
python3.11 -c 'import http.client; c=http.client.HTTPConnection("127.0.0.1",6907,timeout=1); c.request("GET","/health"); r=c.getresponse(); r.read(); c.close(); raise SystemExit(0 if r.status==204 else 1)' || {
 log "mandatory Chromium egress proxy failed to start"; exit 1;
}
# Capture session.sh output into a ROOT-owned diag log (the /dbg reader requires uid==0, mode 0600),
# via bounded_log.py on the root side of the pipe — same pattern as the input-ws/bridge logs.
# rc-guarded so a pipe exit can't trip set -e and crash the capsule.
(while :;do if runuser -u app -- env DISPLAY=:1 XAUTHORITY="$XAUTHORITY" XDG_RUNTIME_DIR=/run/user/1000 \
  PAIRPUTER_SESSION_LOG=/var/log/pairputer-session.log dbus-run-session -- /opt/capsule/session.sh 2>&1 \
  | python3.11 /opt/capsule/bounded_log.py /var/log/pairputer-session.log;then rc=0;else rc=$?;fi;sleep 3;done)&
runuser -u terminal -- env DISPLAY=:1 XAUTHORITY="$XAUTHORITY" HOME=/home/terminal USER=terminal XDG_RUNTIME_DIR=/run/user/1001 \
  xterm -geometry 110x30+20+40 -title "Pairputer Workbench Terminal" -e /opt/capsule/terminal-session.sh &
for service in video_ws input_ws;do
 if test "$service" = input_ws; then service_user=inputd; else service_user=app; fi
 if test "$service" = input_ws; then
  (while :;do if runuser -u "$service_user" -- env DISPLAY=:1 XAUTHORITY="$XAUTHORITY" PYTHONPATH="$PYTHONPATH" \
   PAIRPUTER_CONTROL_STATE_DIR="$PAIRPUTER_CONTROL_STATE_DIR" python3.11 "/opt/capsule/${service}.py" 2>&1 \
   | python3.11 /opt/capsule/bounded_log.py /var/log/pairputer-input-ws.log;then rc=0;else rc=$?;fi;sleep 1;done)&
 else
  (while :;do runuser -u "$service_user" -- env DISPLAY=:1 XAUTHORITY="$XAUTHORITY" PYTHONPATH="$PYTHONPATH" \
   PAIRPUTER_CONTROL_STATE_DIR="$PAIRPUTER_CONTROL_STATE_DIR" python3.11 "/opt/capsule/${service}.py";sleep 1;done)&
 fi
done
(while :;do if runuser -u agent -- env DISPLAY=:1 XAUTHORITY="$XAUTHORITY" PYTHONPATH="$PYTHONPATH" PAIRPUTER_CONTROL_STATE_DIR="$PAIRPUTER_CONTROL_STATE_DIR" \
 PAIRPUTER_BRAIN_PREEMPT_SOCKET=/run/pairputer/brain/brain-preempt.sock \
 PAIRPUTER_DESKTOP_BRAIN_DB=/var/lib/pairputer-brain/brain.sqlite3 \
 python3.11 /opt/capsule/agent_bridge.py 2>&1 \
 | python3.11 /opt/capsule/bounded_log.py /var/log/pairputer-agent-bridge.log;then rc=0;else rc=$?;fi;sleep 1;done)&
# Guard the loop body so a non-zero service exit does NOT trip `set -e` and kill the restart
# subshell — a crashing service must self-heal rather than silently losing that capability. The
# bridge/input loops above already do this; desktopd + audio_ws need the same guard.
(while :;do if env DISPLAY=:1 XAUTHORITY="$XAUTHORITY" PYTHONPATH="$PYTHONPATH" PAIRPUTER_CONTROL_STATE_DIR="$PAIRPUTER_CONTROL_STATE_DIR" \
 python3.11 /opt/capsule/desktopd.py;then rc=0;else rc=$?;fi;sleep 1;done)&
(while :;do if runuser -u app -- env XDG_RUNTIME_DIR=/run/user/1000 python3.11 /opt/capsule/audio_ws.py;then rc=0;else rc=$?;fi;sleep 1;done)&
log "services launched; readiness is http://127.0.0.1:9000/ready"
# PID 1 must not die just because an ordinary desktop child exits. In particular, xterm is a
# user-facing window: closing it is expected, but `wait -n` treated that normal action as a capsule
# failure and AWS terminated the entire MicroVM. The restart-loop services above heal themselves;
# Xvnc is the one critical root process whose loss makes the desktop unusable, so wait specifically
# for it and fail the capsule only if that display server exits.
wait "$xvnc_pid" || true
log "critical Xvnc process exited"
exit 1
