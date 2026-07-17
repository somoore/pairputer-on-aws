#!/usr/bin/env bash
#
# aws-env.sh — shared AWS credential + region resolution for the pairputer scripts.
# Source this; do not execute it.
#
# Credentials use the STANDARD AWS credential chain, so every native method works
# with zero extra config: environment variables, `AWS_PROFILE` (incl. SSO profiles
# in ~/.aws/config), shared credentials in ~/.aws/credentials, container/instance
# roles, etc. We never hardcode or inject a profile. Pick one, in your own shell,
# any of the usual ways:
#
#   aws sso login --profile my-sso-profile && export AWS_PROFILE=my-sso-profile
#   export AWS_PROFILE=my-cli-profile
#   export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
#
# PAIRPUTER_AWS_PROFILE is an optional convenience alias for AWS_PROFILE.
#
# After sourcing, call:  hb_require_aws   (verifies creds resolve, sets AWS_REGION)

# Optional alias: only if the caller set PAIRPUTER_AWS_PROFILE and not AWS_PROFILE.
if [[ -z "${AWS_PROFILE:-}" && -n "${PAIRPUTER_AWS_PROFILE:-}" ]]; then
  export AWS_PROFILE="${PAIRPUTER_AWS_PROFILE}"
fi

hb_resolve_region() {
  if [[ -n "${PAIRPUTER_AWS_REGION:-}" ]]; then echo "${PAIRPUTER_AWS_REGION}"; return; fi
  if [[ -n "${AWS_REGION:-}" ]]; then echo "${AWS_REGION}"; return; fi
  if [[ -n "${AWS_DEFAULT_REGION:-}" ]]; then echo "${AWS_DEFAULT_REGION}"; return; fi
  aws configure get region 2>/dev/null || true
}

# Verify credentials resolve and a region is set. Exports AWS_REGION,
# AWS_DEFAULT_REGION, and AWS_ACCOUNT_ID. Exits non-zero with guidance if not.
hb_require_aws() {
  AWS_REGION="$(hb_resolve_region)"
  if [[ -z "${AWS_REGION}" || "${AWS_REGION}" == "None" ]]; then
    echo "ERROR: no AWS region configured." >&2
    echo "       Set one, e.g.:  export AWS_REGION=us-east-1" >&2
    echo "       (pairputer deploys in a single region; use us-east-1 for the CloudFront WAF.)" >&2
    return 1
  fi
  export AWS_REGION
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"

  local ident
  if ! ident="$(aws sts get-caller-identity --query Account --output text 2>&1)"; then
    echo "ERROR: AWS credentials are not configured or have expired." >&2
    echo "       Details: ${ident}" >&2
    echo "" >&2
    echo "       Configure credentials any standard way, then re-run:" >&2
    echo "         - SSO:     aws sso login --profile <name> && export AWS_PROFILE=<name>" >&2
    echo "         - profile: export AWS_PROFILE=<name>   (from ~/.aws/config or ~/.aws/credentials)" >&2
    echo "         - keys:    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=..." >&2
    return 1
  fi
  export AWS_ACCOUNT_ID="${ident}"
}
