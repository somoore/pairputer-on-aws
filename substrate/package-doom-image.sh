#!/usr/bin/env bash
#
# Package the pairputer DOOM MicroVM build context.
#
# The output is intentionally WAD-free. The Dockerfile fetches the pinned
# shareware DOOM1.WAD during the AWS-managed MicroVM image build and verifies
# the SHA-256 before snapshotting the image.
#
# Usage:
#   ./package-doom-image.sh                         # create local zip, print path
#   ./package-doom-image.sh <s3-bucket> [s3-prefix] # upload, print s3:// URI
#
# Optional WAD mirror override for private/org builds:
#   PAIRPUTER_DOOM1_WAD_URL=https://mirror.example/DOOM1.WAD \
#   PAIRPUTER_DOOM1_WAD_SHA256=<64-hex-sha256> \
#   ./package-doom-image.sh <s3-bucket>

set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXT_DIR="${PAIRPUTER_MICROVM_CONTEXT_DIR:-${SCRIPT_DIR}/../capsules/${PAIRPUTER_REFERENCE_CAPSULE:-computer-use-desktop}}"
UPLOAD_BUCKET="${1:-${PAIRPUTER_DOOM_CONTEXT_BUCKET:-}}"
UPLOAD_PREFIX="${2:-${PAIRPUTER_DOOM_CONTEXT_PREFIX:-pairputer/microvm-image}}"
OUTPUT_DIR="${PAIRPUTER_DOOM_CONTEXT_OUT_DIR:-${SCRIPT_DIR}/.artifacts}"

if [[ ! -f "${CONTEXT_DIR}/Dockerfile" ]]; then
  echo "ERROR: MicroVM context Dockerfile not found at ${CONTEXT_DIR}/Dockerfile" >&2
  exit 1
fi

check_tree() {
  local root="$1"
  local found

  found="$(find "${root}" -type l -print -quit)"
  if [[ -n "${found}" ]]; then
    echo "ERROR: refusing to package symlink: ${found}" >&2
    exit 1
  fi

  found="$(find "${root}" -type f -links +1 -print -quit)"
  if [[ -n "${found}" ]]; then
    echo "ERROR: refusing to package hardlinked file: ${found}" >&2
    exit 1
  fi

  found="$(find "${root}" ! -type f ! -type d ! -type l -print -quit)"
  if [[ -n "${found}" ]]; then
    echo "ERROR: refusing to package non-regular file: ${found}" >&2
    exit 1
  fi

  found="$(find "${root}" \( -iname '*.wad' -o -iname '*.WAD' \) -print -quit)"
  if [[ -n "${found}" ]]; then
    echo "ERROR: refusing to package WAD/game data: ${found}" >&2
    echo "       This release artifact must stay WAD-free; the build fetches the pinned shareware WAD." >&2
    exit 1
  fi
}

if [[ -n "${PAIRPUTER_DOOM1_WAD_URL:-}" || -n "${PAIRPUTER_DOOM1_WAD_SHA256:-}" ]]; then
  if [[ -z "${PAIRPUTER_DOOM1_WAD_URL:-}" || -z "${PAIRPUTER_DOOM1_WAD_SHA256:-}" ]]; then
    echo "ERROR: set both PAIRPUTER_DOOM1_WAD_URL and PAIRPUTER_DOOM1_WAD_SHA256." >&2
    exit 1
  fi
  if [[ "${PAIRPUTER_DOOM1_WAD_URL}" =~ [[:space:]] ]]; then
    echo "ERROR: PAIRPUTER_DOOM1_WAD_URL must not contain whitespace." >&2
    exit 1
  fi
  if [[ ! "${PAIRPUTER_DOOM1_WAD_SHA256}" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "ERROR: PAIRPUTER_DOOM1_WAD_SHA256 must be a 64-character hex SHA-256." >&2
    exit 1
  fi
fi

write_wad_source_json() {
  local dest="$1"

  python3 - "${PAIRPUTER_DOOM1_WAD_URL}" "${PAIRPUTER_DOOM1_WAD_SHA256}" > "${dest}" <<'PY'
import json
import sys

payload = {
    "DOOM1_WAD_URL": sys.argv[1],
    "DOOM1_WAD_SHA256": sys.argv[2],
}
json.dump(payload, sys.stdout, separators=(",", ":"))
sys.stdout.write("\n")
PY
}

check_tree "${CONTEXT_DIR}"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pairputer-doom-context.XXXXXX")"
cleanup() {
  rm -rf "${TMP_ROOT}"
}
trap cleanup EXIT

STAGING="${TMP_ROOT}/context"
mkdir -p "${STAGING}" "${OUTPUT_DIR}"

# A capsule may ship a .contextignore (rsync exclude patterns, one per line)
# for dev artifacts that live in its directory but are NOT build context —
# eval outputs, benchmarks, model weights. Without it, agent-doom's 4.1GB
# vision_bench/ once rode into a 3.4GB "WAD-free" context zip (vs ~35KB).
EXCLUDE_FROM=()
if [[ -f "${CONTEXT_DIR}/.contextignore" ]]; then
  EXCLUDE_FROM=(--exclude-from="${CONTEXT_DIR}/.contextignore")
fi
rsync -a \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='*.wad' \
  --exclude='*.WAD' \
  --exclude='.DS_Store' \
  --exclude='.pytest_cache/' \
  --exclude='.contextignore' \
  ${EXCLUDE_FROM[@]+"${EXCLUDE_FROM[@]}"} \
  "${CONTEXT_DIR}/" "${STAGING}/"

if [[ -n "${PAIRPUTER_DOOM1_WAD_URL:-}" ]]; then
  write_wad_source_json "${STAGING}/wad-source.json"
fi

# Agent-interactive capsules ship capsule.yaml. Embed its VALIDATED compact-JSON form as
# capsule.manifest.json so the in-stack manifest stager (capsule-stack.yaml) can register the capsule
# from the zip alone — no YAML parser in Lambda, no 4KB env-var manifest, works for a pure console
# 1-click. Part of the tree hash: a manifest change changes the context identity.
if [[ -f "${STAGING}/capsule.yaml" ]]; then
  python3 "${SCRIPT_DIR}/validate-capsule-manifest.py" "${STAGING}/capsule.yaml" > "${STAGING}/capsule.manifest.json" \
    || { echo "ERROR: capsule.yaml failed manifest validation; refusing to package." >&2; exit 1; }
  echo "==> Embedded validated capsule.manifest.json ($(wc -c < "${STAGING}/capsule.manifest.json" | tr -d ' ') bytes)"
fi

check_tree "${STAGING}"

TREE_HASH="$(
  cd "${STAGING}"
  find . -type f -print | LC_ALL=C sort | while IFS= read -r path; do
    shasum -a 256 "${path}" | awk -v p="${path}" '{print $1 "  " p}'
  done | shasum -a 256 | awk '{print $1}'
)"

CAPSULE_BASENAME="$(basename "${CONTEXT_DIR}")"
ZIP_PATH="${PAIRPUTER_DOOM_CONTEXT_ZIP:-${OUTPUT_DIR}/pairputer-${CAPSULE_BASENAME}-context-${TREE_HASH}.zip}"
rm -f "${ZIP_PATH}"

(
  cd "${STAGING}"
  find . -type f -print | LC_ALL=C sort | sed 's#^\./##' | zip -X -q "${ZIP_PATH}" -@
)

echo "==> MicroVM context: ${CONTEXT_DIR}"
echo "==> Tree SHA-256:    ${TREE_HASH}"
echo "==> Zip:             ${ZIP_PATH}"

if [[ -n "${UPLOAD_BUCKET}" ]]; then
  UPLOAD_PREFIX="${UPLOAD_PREFIX#/}"
  UPLOAD_PREFIX="${UPLOAD_PREFIX%/}"
  DEST="s3://${UPLOAD_BUCKET}/${UPLOAD_PREFIX}/pairputer-${CAPSULE_BASENAME}-context-${TREE_HASH}.zip"
  echo "==> Uploading:       ${DEST}"
  aws s3 cp "${ZIP_PATH}" "${DEST}" --only-show-errors
  echo "${DEST}"
else
  echo "${ZIP_PATH}"
fi
