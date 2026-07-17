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
# Images are keyless-signed by publish-images.yml in somoore/pairputer.
ID_REGEXP="${PAIRPUTER_SIGNER_IDENTITY_REGEXP:-^https://github.com/somoore/pairputer/.github/workflows/.*@refs/heads/main$}"

# --- Pinned Sigstore trust root => offline, fail-closed verification (no egress at verify time). ---------
# cosign v3 pins the root via the --trusted-root FLAG (the old SIGSTORE_ROOT_FILE env var is a v2 relic and
# is silently ignored for the "new bundle format" signatures our CI produces).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUSTED_ROOT="${PAIRPUTER_TRUSTED_ROOT:-${HERE}/sigstore-trusted-root.json}"

# --- The digests the template pins today. Keep in sync with substrate/cloudformation/pairputer.yaml. ------
DEFAULT_IMAGES=(
  "public.ecr.aws/b6x6x7v3/pairputer-mcp@sha256:665fcdbcdc245f18c68be220cf216440ab1772eb2a8aee3be5036238a44f5193"
  "public.ecr.aws/b6x6x7v3/pairputer-stateful-relay@sha256:7340635637cb1aa4644a90da4644986d708260752fa363522321f33710b69176"
)

command -v cosign >/dev/null || { echo "ERROR: cosign not found. See https://docs.sigstore.dev/cosign/installation"; exit 2; }
[[ -f "$TRUSTED_ROOT" ]] || { echo "ERROR: pinned Sigstore root not found: $TRUSTED_ROOT"; exit 2; }

IMAGES=("$@"); [[ ${#IMAGES[@]} -eq 0 ]] && IMAGES=("${DEFAULT_IMAGES[@]}")

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
