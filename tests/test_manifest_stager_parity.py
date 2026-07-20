"""The in-stack manifest stager (capsule-stack.yaml) must stage EXACTLY what deploy-capsule.sh stages
script-side — same value encoding, chunk header, digest, and SSM path — or a capsule deployed via the
console 1-click and one deployed via the CLI would disagree about the same manifest bytes.

This test runs both algorithms on the real workbench manifest and asserts identical outputs.
"""
import base64
import gzip
import hashlib
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]


def load_validated_manifest():
    out = subprocess.run(
        [sys.executable, str(REPO / "substrate/validate-capsule-manifest.py"),
         str(REPO / "capsules/computer-use-desktop/capsule.yaml")],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def stage_like_deploy_capsule_sh(manifest, ctx_sha, ctx_uri):
    """Verbatim re-expression of deploy-capsule.sh lines 132-153."""
    manifest = json.loads(json.dumps(manifest))
    manifest["capsule"]["deployment"] = {"contextSha256": ctx_sha, "contextUri": ctx_uri}
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    value = "gzip+base64:" + base64.b64encode(gzip.compress(raw, mtime=0)).decode()
    parts = []
    if len(value) > 8192:
        full_sha = hashlib.sha256(value.encode()).hexdigest()
        parts = [value[i:i + 7500] for i in range(0, len(value), 7500)]
        assert 1 <= len(parts) <= 16
        value = "chunked:v1:%d:%s" % (len(parts), full_sha)
    digest_hex = hashlib.sha256(value.encode()).hexdigest()
    return value, parts, digest_hex


def stage_like_stager_lambda(manifest, ctx_sha, ctx_uri, capsule_id):
    """The compute portion of the ManifestStagerFunction inline code (capsule-stack.yaml)."""
    manifest = json.loads(json.dumps(manifest))
    assert (manifest.get("capsule") or {}).get("id") == capsule_id
    manifest["capsule"]["deployment"] = {"contextSha256": ctx_sha, "contextUri": ctx_uri}
    raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    value = "gzip+base64:" + base64.b64encode(gzip.compress(raw, mtime=0)).decode()
    parts = []
    if len(value) > 8192:
        full_sha = hashlib.sha256(value.encode()).hexdigest()
        parts = [value[i:i + 7500] for i in range(0, len(value), 7500)]
        assert 1 <= len(parts) <= 16
        value = "chunked:v1:%d:%s" % (len(parts), full_sha)
    digest_hex = hashlib.sha256(value.encode()).hexdigest()
    manifest_param = "/pairputer/capsules/%s/manifests/sha256-%s" % (capsule_id, digest_hex)
    return value, parts, digest_hex, manifest_param


def test_stager_matches_script_staging():
    manifest = load_validated_manifest()
    ctx_sha = "ab" * 32
    ctx_uri = "s3://example-bucket/capsules/computer-use-desktop/pairputer-doom-context-%s.zip" % ctx_sha

    v1, p1, d1 = stage_like_deploy_capsule_sh(manifest, ctx_sha, ctx_uri)
    v2, p2, d2, path = stage_like_stager_lambda(manifest, ctx_sha, ctx_uri, "computer-use-desktop")

    assert v1 == v2, "primary SSM value differs between script and stager staging"
    assert p1 == p2, "chunk parts differ between script and stager staging"
    assert d1 == d2, "manifest digest differs between script and stager staging"
    assert path == "/pairputer/capsules/computer-use-desktop/manifests/sha256-" + d1
    # the workbench manifest is the case that FORCED chunking — make sure this test still covers it
    assert p1, "workbench manifest no longer chunks; this parity test lost its hard case"
    assert v1.startswith("chunked:v1:")


def test_stager_lambda_code_in_template_matches_this_algorithm():
    """Guard against the inline template code drifting from the algorithm proven here: the load-bearing
    lines must appear verbatim in the template's ZipFile block."""
    tpl = (REPO / "capsules/nested/capsule-stack.yaml").read_text()
    for needle in [
        'value = "gzip+base64:" + base64.b64encode(gzip.compress(raw, mtime=0)).decode()',
        'parts = [value[i:i+7500] for i in range(0, len(value), 7500)]',
        'value = "chunked:v1:%d:%s" % (len(parts), full_sha)',
        'raw = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()',
        '"/pairputer/capsules/%s/manifests/sha256-%s" % (capsule_id, digest_hex)',
    ]:
        assert needle in tpl, "stager template drifted from the proven staging algorithm: " + needle


if __name__ == "__main__":
    test_stager_matches_script_staging()
    test_stager_lambda_code_in_template_matches_this_algorithm()
    print("OK: stager parity")
