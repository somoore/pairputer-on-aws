#!/usr/bin/env bash
# Self-check for remove-cf.sh: the teardown recovery (retry + VM sweep + unwedge + retain) must apply
# to CAPSULE cartridge stacks, not just the root — a capsule stack whose flaky MicroVM image wedges
# used to get a naive delete+warn and left the human to hand-clean it (2026-07-16 teardown). No AWS —
# static structural assertions against the script source, plus one sourced dry-run of the helpers with
# every `aws`/`node`/`sleep` call stubbed so control flow is exercised without touching the cloud.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$HERE/substrate/remove-cf.sh"
fail() { echo "FAIL: $1" >&2; exit 1; }

# --- 1. Structural pins: the fixes must stay in place -------------------------------------------
src="$(cat "$SCRIPT")"

# force_delete_stack and its helpers are defined at TOP LEVEL (before the capsule loop), not nested
# inside the root-only `if describe-stacks` block — that nesting was the original bug.
fds_line=$(grep -n '^force_delete_stack()' "$SCRIPT" | head -1 | cut -d: -f1)
cap_line=$(grep -n 'Capsule cartridge stacks (deleted before the substrate)' "$SCRIPT" | head -1 | cut -d: -f1)
root_if_line=$(grep -n 'List the nested stacks up front' "$SCRIPT" | head -1 | cut -d: -f1)
[[ -n "$fds_line" && -n "$cap_line" ]] || fail "force_delete_stack / capsule loop markers not found"
[[ "$fds_line" -lt "$cap_line" ]] || fail "force_delete_stack must be defined BEFORE the capsule loop"
[[ "$fds_line" -lt "$root_if_line" ]] || fail "force_delete_stack must be defined OUTSIDE the root-only block"

# The capsule loop must call force_delete_stack (the recovery path), passing the capsule's own image.
grep -q 'force_delete_stack "${cs}" "${capsule_image}"' "$SCRIPT" \
  || fail "capsule loop must call force_delete_stack with the capsule image name"
grep -q 'capsule_image="${cs#"${STACK_NAME}-capsule-"}"' "$SCRIPT" \
  || fail "capsule image name must be derived from the stack name"

# Both image helpers take an image-name arg (defaulting to DOOM) so they target the RIGHT image.
grep -q 'delete_orphan_microvm_image() {' "$SCRIPT" || fail "delete_orphan_microvm_image missing"
grep -q 'sweep_microvms_on_image() {' "$SCRIPT" || fail "sweep_microvms_on_image missing"
grep -qE 'local image_name="\$\{1:-\$\{DOOM_IMAGE_NAME\}\}"' "$SCRIPT" \
  || fail "image helpers must accept an image-name arg defaulting to DOOM"
# The hardcoded DOOM-only tagging query must be gone (it silently no-op'd on a capsule image).
grep -q 'microvm-image:${DOOM_IMAGE_NAME}' "$SCRIPT" \
  && fail "a hardcoded microvm-image:\${DOOM_IMAGE_NAME} query remains — must use \${image_name}"

# The wedged-image lever: re-issue DeleteMicrovmImage on a self-reverting DELETE_FAILED, and poll to
# GONE rather than trusting a 200 "accepted".
grep -q 're-issuing DeleteMicrovmImage to unwedge it' "$SCRIPT" \
  || fail "force_delete_stack must attempt to unwedge a stuck image"
grep -q 'DELETE_FAILED" || st==="DELETION_FAILED"' "$SCRIPT" \
  || fail "orphan-image loop must re-issue on a flip back to DELETE_FAILED"

# A failed capsule delete must surface a non-zero exit so a re-run is prompted.
grep -q 'CAPSULE_DELETE_FAILED=1' "$SCRIPT" || fail "capsule failure must set CAPSULE_DELETE_FAILED"
grep -q 'CAPSULE_DELETE_FAILED:-0}" == "1"' "$SCRIPT" || fail "final exit must honor CAPSULE_DELETE_FAILED"
echo "PASS: capsule stacks use force_delete_stack with their own image + wedged-image recovery"

# --- 2. Dry-run the helpers with the cloud stubbed ----------------------------------------------
# Extract every function def and drive force_delete_stack against a stub that reports a stack which
# deletes cleanly on the 2nd attempt (proves retry loops, no AWS, no sleeps).
TMP="$(mktemp)"
{
  echo 'AWS_REGION=us-east-1; DOOM_IMAGE_NAME=pairputer-doom; RETAINED_RESOURCES=""'
  echo 'sleep() { :; }'  # no real waiting
  echo 'CALLS=0'
  # stub aws: first delete "fails" (stack still present), second "succeeds" (gone)
  cat <<'STUB'
aws() {
  case "$*" in
    *"wait stack-delete-complete"*) [ "$CALLS" -ge 2 ] && return 0 || return 1 ;;
    *"delete-stack"*) CALLS=$((CALLS+1)); return 0 ;;
    *"describe-stacks"*) [ "$CALLS" -ge 2 ] && return 1 || return 0 ;;  # gone after 2nd delete
    *"describe-stack-resources"*) echo "" ;;  # no stuck leaves
    *) return 0 ;;
  esac
}
sweep_microvms_on_image() { :; }
delete_orphan_microvm_image() { :; }
STUB
  # pull in the three stack helpers + force_delete_stack verbatim
  awk '/^delete_and_wait\(\) \{/,/^\}/' "$SCRIPT"
  awk '/^stuck_leaf_resources\(\) \{/,/^\}/' "$SCRIPT"
  awk '/^stuck_nested_stacks\(\) \{/,/^\}/' "$SCRIPT"
  awk '/^force_delete_stack\(\) \{/,/^\}$/' "$SCRIPT"
  echo 'force_delete_stack "test-stack" "test-image" && echo "DRYRUN_OK: deleted on retry" || echo "DRYRUN_FAIL"'
} > "$TMP"
out="$(bash "$TMP" 2>&1)"
rm -f "$TMP"
echo "$out" | grep -q "DRYRUN_OK: deleted on retry" \
  || { echo "$out" >&2; fail "force_delete_stack dry-run did not converge on retry"; }
echo "PASS: force_delete_stack retry loop converges (stubbed)"

echo "PASS: remove-cf.sh"
