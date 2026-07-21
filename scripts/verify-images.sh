#!/usr/bin/env bash
# pairputer supply-chain verification — run this OUT OF BAND before (or any time after) you deploy.
#
# It proves the exact image digests the pairputer CloudFormation template pins were:
#   1. signed (cosign keyless) by the pairputer GitHub Actions CI identity, and
#   2. built by that CI with a SLSA build-provenance attestation.
#
# There is no in-stack gate that forces this on you: the deploy-time integrity guarantee is the DIGEST
# PIN itself (a @sha256: ref is immutable and content-addressed — it cannot be swapped after you read it).
# This script is how you (or your security team) independently confirm what that digest actually is.
#
# Requires: cosign (https://docs.sigstore.dev/cosign/installation). Verification runs OFFLINE against a
# pinned Sigstore trust root committed next to this script, so a network/Sigstore outage can't make a
# bad image look good — verification simply fails closed.
#
# Usage:
#   scripts/verify-images.sh                 # verify the pinned defaults below
#   scripts/verify-images.sh IMG@sha256:...  # verify specific digests
set -euo pipefail

# --- The signer identity. These MUST match how CI signs (.github/workflows/publish-images.yml). ---------
ID_ISSUER="https://token.actions.githubusercontent.com"
# Images are keyless-signed by publish-images.yml in somoore/pairputer-on-aws.
ID_REGEXP="${PAIRPUTER_SIGNER_IDENTITY_REGEXP:-^https://github.com/somoore/pairputer-on-aws/.github/workflows/.*@refs/heads/main$}"

# --- Pinned Sigstore trust root => offline, fail-closed verification (no egress at verify time). ---------
# cosign v3 pins the root via the --trusted-root FLAG (the old SIGSTORE_ROOT_FILE env var is a v2 relic and
# is silently ignored for the "new bundle format" signatures our CI produces).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUSTED_ROOT="${PAIRPUTER_TRUSTED_ROOT:-${HERE}/sigstore-trusted-root.json}"

# --- Default: verify the EXACT digests the CloudFormation template pins, read from the template itself
# so this script can never drift from what a deploy actually uses. (A previous hardcoded list here went
# stale after a digest bump — it "verified" images the template no longer deployed.)
TEMPLATE="${PAIRPUTER_TEMPLATE:-${HERE}/../substrate/cloudformation/pairputer.yaml}"
DEFAULT_IMAGES=()
if [[ -f "$TEMPLATE" ]]; then
  while IFS= read -r img; do DEFAULT_IMAGES+=("$img"); done < <(
    grep -oE 'public\.ecr\.aws/[a-z0-9/._-]+@sha256:[0-9a-f]{64}' "$TEMPLATE" | sort -u)
fi

command -v cosign >/dev/null || { echo "ERROR: cosign not found. See https://docs.sigstore.dev/cosign/installation"; exit 2; }
[[ -f "$TRUSTED_ROOT" ]] || { echo "ERROR: pinned Sigstore root not found: $TRUSTED_ROOT"; exit 2; }

IMAGES=("$@")
if [[ ${#IMAGES[@]} -eq 0 ]]; then
  [[ ${#DEFAULT_IMAGES[@]} -gt 0 ]] || { echo "ERROR: no digests given and template not found at $TEMPLATE (set PAIRPUTER_TEMPLATE or pass IMG@sha256:... args)"; exit 2; }
  echo "Verifying the digests pinned by: $TEMPLATE"
  IMAGES=("${DEFAULT_IMAGES[@]}")
fi

fail=0
for img in "${IMAGES[@]}"; do
  echo "=== $img"
  case "$img" in
    *@sha256:*) : ;;
    *) echo "  REFUSING: not a @sha256 digest (tags are mutable, not trusted)"; fail=1; continue ;;
  esac
  if cosign verify --offline --trusted-root "$TRUSTED_ROOT" \
        --certificate-oidc-issuer "$ID_ISSUER" --certificate-identity-regexp "$ID_REGEXP" \
        "$img" >/dev/null 2>&1; then
    echo "  signature:  OK"
  else
    echo "  signature:  FAILED"; fail=1
  fi
  if cosign verify-attestation --offline --trusted-root "$TRUSTED_ROOT" --type slsaprovenance \
        --certificate-oidc-issuer "$ID_ISSUER" --certificate-identity-regexp "$ID_REGEXP" \
        "$img" >/dev/null 2>&1; then
    echo "  SLSA prov:  OK"
  else
    echo "  SLSA prov:  FAILED"; fail=1
  fi
done

if [[ $fail -eq 0 ]]; then
  echo; echo "ALL IMAGES VERIFIED: signed by pairputer CI + SLSA build provenance."
else
  echo; echo "VERIFICATION FAILED — do not deploy these digests." >&2
fi
exit $fail
