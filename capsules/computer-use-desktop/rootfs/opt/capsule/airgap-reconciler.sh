#!/usr/bin/env bash
# Root reconciler: the unprivileged bridge (uid agent) can only WRITE a desired
# air-gap state to the intent file; this root loop is the only thing that touches
# iptables. Poll the intent, apply it when it differs from enforced truth. Keeps
# the privileged firewall op in root while the bridge just expresses intent.
set -uo pipefail
INTENT_FILE=/run/pairputer/brain/airgap.intent
AIRGAP=/opt/capsule/airgap.sh
# Default ON: the box ships air-gapped ("run without worry"). AWS-proven safe — a 15-min
# live soak (30/30 polls enforced=on + bridge reachable, mid-soak toggle clean) confirmed
# the public-only reject never severs the aws-proxy control plane. The widget/network_airgap
# tool opens egress on demand (pip/uv/git work within ~1s of disable).
default="${PAIRPUTER_AIRGAP_DEFAULT:-on}"

# Enforce the default at boot before the desktop can originate any traffic.
"$AIRGAP" "$default" || true

while :; do
  want="$default"
  if [ -r "$INTENT_FILE" ]; then
    read -r want < "$INTENT_FILE" || want="$default"
  fi
  case "$want" in on|off) ;; *) want="$default" ;; esac
  have="$("$AIRGAP" status 2>/dev/null || echo unknown)"
  if [ "$want" != "$have" ]; then
    "$AIRGAP" "$want" || true
  fi
  sleep 1
done
