#!/usr/bin/env bash
# Air-gap the box's egress. ON = the VM cannot reach the PUBLIC internet (pip/git/
# curl and the browser all fail); OFF = normal internet.
#
# DESIGN (learned the hard way on AWS): do NOT default-reject all OUTPUT. The AWS
# MicroVM control plane (the aws-proxy that carries the bridge + video/audio/input)
# rides eth0 through a private/link-local path that a blanket OUTPUT reject severs —
# and once severed the bridge 502s and air-gap can't even be turned back off. Also,
# the minimal Lambda kernel may lack nf_conntrack, so ESTABLISHED matching can't be
# relied on. So instead we REJECT only NEW traffic to PUBLIC (internet-routable)
# destinations and leave every private/link-local/loopback range ALONE — the same
# shape as the existing uid-1005 job firewall, which never breaks the control plane.
# Public-internet destinations are "everything except" the reserved/private blocks.
#
# Enforcement is a dedicated PAIRPUTER-AIRGAP chain hung off OUTPUT; on/off is a single
# chain flush — hot, no reboot. Must run as root (needs iptables). Usage:
#   airgap.sh on|off|status
set -euo pipefail
CHAIN=PAIRPUTER-AIRGAP
STATE_FILE=/run/pairputer/airgap.state
DETAIL_FILE=/run/pairputer/airgap.detail

# Never-blocked destination ranges: loopback, RFC1918 private, link-local (incl. the
# 169.254 cloud-metadata/proxy range), CGNAT, and multicast/reserved. The control
# plane and any in-VM service live in these; only PUBLIC destinations get rejected.
EXEMPT_DESTS="127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 224.0.0.0/4 240.0.0.0/4 0.0.0.0/8 192.0.2.0/24"

fw_list() { for fw in iptables ip6tables; do command -v "$fw" >/dev/null 2>&1 && echo "$fw"; done; }

detach() {
  for fw in $(fw_list); do
    while "$fw" -D OUTPUT -j "$CHAIN" 2>/dev/null; do :; done
    "$fw" -F "$CHAIN" 2>/dev/null || true
    "$fw" -X "$CHAIN" 2>/dev/null || true
  done
}

attach() {
  detach
  local applied=0 fw
  for fw in iptables; do   # v4 only: the exempt list is IPv4; IPv6 egress is separately absent/disabled
    "$fw" -t filter -L >/dev/null 2>&1 || continue
    "$fw" -N "$CHAIN" 2>/dev/null || "$fw" -F "$CHAIN" 2>/dev/null || continue
    # Loopback + every private/link-local/reserved destination: always allowed. This
    # is what keeps the aws-proxy control plane and in-VM services fully intact.
    "$fw" -A "$CHAIN" -o lo -j RETURN 2>/dev/null || true
    local d
    for d in $EXEMPT_DESTS; do
      "$fw" -A "$CHAIN" -d "$d" -j RETURN 2>/dev/null || true
    done
    # Everything else (public internet) is rejected. REJECT if available, else DROP.
    if "$fw" -A "$CHAIN" -j REJECT --reject-with icmp-admin-prohibited 2>/dev/null \
       || "$fw" -A "$CHAIN" -j DROP 2>/dev/null; then :; fi
    if "$fw" -A OUTPUT -j "$CHAIN" 2>/dev/null; then applied=1; fi
  done
  test "$applied" = 1
}

case "${1:-status}" in
  on)
    if attach; then
      echo on > "$STATE_FILE"
      { echo "on"; iptables -S "$CHAIN" 2>&1; } > "$DETAIL_FILE" 2>&1 || true
      echo "airgap on" >&2
    else
      { echo "ATTACH-FAILED"; iptables -S 2>&1 | head -20; } > "$DETAIL_FILE" 2>&1 || true
      echo "airgap on FAILED to apply any rule" >&2
      exit 1
    fi ;;
  off) detach; echo off > "$STATE_FILE"; echo "off" > "$DETAIL_FILE" 2>/dev/null || true; echo "airgap off" >&2 ;;
  status)
    if iptables -C OUTPUT -j "$CHAIN" >/dev/null 2>&1; then echo on; else echo off; fi
    ;;
  *) echo "usage: airgap.sh on|off|status" >&2; exit 2 ;;
esac
