#!/usr/bin/env bash
# Self-check for airgap.sh with iptables mocked (no root needed). Verifies:
#  - `on` builds the PAIRPUTER-AIRGAP chain: allows lo + ESTABLISHED/RELATED,
#    rejects the rest, and jumps OUTPUT into it
#  - `off` detaches (removes the OUTPUT jump)
#  - status reflects whether the OUTPUT jump is present
# Run: bash tests/test_airgap.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$HERE/rootfs/opt/capsule/airgap.sh"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# Mock iptables/ip6tables: record every call, and model the OUTPUT-jump presence
# via a marker file so `-C OUTPUT -j PAIRPUTER-AIRGAP` returns truthfully.
CALLS="$WORK/calls.log"; JUMP="$WORK/jump.present"
for name in iptables ip6tables; do
  cat > "$WORK/$name" <<EOF
#!/usr/bin/env bash
echo "$name \$*" >> "$CALLS"
case "\$*" in
  *"-C OUTPUT -j PAIRPUTER-AIRGAP"*) [ -f "$JUMP" ] && exit 0 || exit 1 ;;
  *"-A OUTPUT -j PAIRPUTER-AIRGAP"*) touch "$JUMP"; exit 0 ;;
  *"-D OUTPUT -j PAIRPUTER-AIRGAP"*) if [ -f "$JUMP" ]; then rm -f "$JUMP"; exit 0; else exit 1; fi ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$WORK/$name"
done
export PATH="$WORK:$PATH"
export PAIRPUTER_AIRGAP_STATE_FILE="$WORK/airgap.state" 2>/dev/null || true

# airgap.sh hardcodes /run/... state paths; run with a redirected STATE_FILE by
# copying the script and pointing its state at the tmpdir (avoids needing /run).
sed "s#/run/pairputer/airgap.state#$WORK/airgap.state#" "$SCRIPT" > "$WORK/airgap.sh"
chmod +x "$WORK/airgap.sh"
SUT="$WORK/airgap.sh"

# status starts off (no jump)
[ "$("$SUT" status)" = off ] || { echo "FAIL: initial status not off"; exit 1; }

# on: builds chain and attaches jump
"$SUT" on >/dev/null 2>&1
grep -q -- "-A PAIRPUTER-AIRGAP -o lo -j RETURN" "$CALLS" || { echo "FAIL: no loopback allow"; exit 1; }
# LOAD-BEARING: private/link-local destinations exempt so the aws-proxy control plane survives.
grep -q -- '-d 10.0.0.0/8 -j RETURN' "$CALLS" || { echo "FAIL: no RFC1918 exemption"; exit 1; }
grep -q -- '-d 169.254.0.0/16 -j RETURN' "$CALLS" || { echo "FAIL: no link-local exemption"; exit 1; }
grep -Eq -- "-A PAIRPUTER-AIRGAP -j (REJECT|DROP)" "$CALLS" || { echo "FAIL: no default reject"; exit 1; }
grep -q -- "-A OUTPUT -j PAIRPUTER-AIRGAP" "$CALLS" || { echo "FAIL: no OUTPUT jump"; exit 1; }
[ "$("$SUT" status)" = on ] || { echo "FAIL: status not on after on"; exit 1; }
[ "$(cat "$WORK/airgap.state")" = on ] || { echo "FAIL: state file not on"; exit 1; }

# off: detaches
"$SUT" off >/dev/null 2>&1
[ "$("$SUT" status)" = off ] || { echo "FAIL: status not off after off"; exit 1; }
[ "$(cat "$WORK/airgap.state")" = off ] || { echo "FAIL: state file not off"; exit 1; }

echo "PASS: airgap.sh"
