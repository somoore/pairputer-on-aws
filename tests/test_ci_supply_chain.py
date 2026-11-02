"""Supply-chain regression gates for the privileged CI/publish path.

These are the controls that protect the public 1-click assets and the images customers run:
SHA-pinned actions in jobs with OIDC, an exact OIDC `sub` ref, checksum-pinned copier tools,
and least-privilege MicroVM proxy tokens. They are cheap to assert and easy to regress.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_all_workflow_actions_are_pinned_to_commit_shas():
    unpinned = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(_read(path).splitlines(), 1):
            match = re.search(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
            if not match:
                continue
            ref = match.group(1)
            if ref.startswith("./"):
                continue
            if not re.search(r"@[0-9a-f]{40}$", ref):
                unpinned.append(f"{path.name}:{number}:{ref}")
    assert unpinned == [], "mutable action refs:\n" + "\n".join(unpinned)


def test_no_pull_request_target_workflow():
    for path in WORKFLOWS.glob("*.yml"):
        assert "pull_request_target" not in _read(path), path.name


def test_publish_launch_uses_the_same_pins_as_publish_images():
    launch = _read(WORKFLOWS / "publish-launch.yml")
    images = _read(WORKFLOWS / "publish-images.yml")

    def pin(workflow: str, action: str) -> str:
        match = re.search(rf"uses:\s*{re.escape(action)}@([0-9a-f]{{40}})", workflow)
        assert match, f"{action} is not SHA-pinned"
        return match.group(1)

    for action in ("actions/checkout", "aws-actions/configure-aws-credentials"):
        assert pin(launch, action) == pin(images, action)


def test_oidc_sub_is_an_exact_ref_not_a_prefix_wildcard():
    template = _read(REPO / "ci" / "pairputer-ci.yaml")
    sub = re.search(
        r"token\.actions\.githubusercontent\.com:sub:\s*(.+)$", template, re.MULTILINE)
    assert sub, "the push role lost its sub condition"
    assert sub.group(1).rstrip().endswith(':ref:${GitHubRef}"'), sub.group(1)
    assert not sub.group(1).rstrip().endswith("*"), "ref prefix wildcard reintroduced"
    # The condition must not be StringLike (IAM requires an exact match here).
    block = template[template.index("AssumeRolePolicyDocument"):template.index("Policies:")]
    assert "StringEquals:" in block
    assert "StringLike:" not in block


def test_private_image_copier_tools_are_version_and_checksum_pinned():
    copier = _read(REPO / "substrate" / "cloudformation" / "nested" / "image-copy.yaml")
    assert "releases/latest" not in copier
    assert 'CRANE_VERSION="0.22.1"' in copier
    assert 'COSIGN_VERSION="3.1.1"' in copier
    for digest in (
        "898c0cff975f898a33e8c4580bdafb0e7c02c7faa33374e946762f97c4ab7110",  # crane linux/arm64
        "2ec865872e331c32fd12b08dae15332d3f92c0aa029219589684a4903ca85d11",  # cosign linux/arm64
    ):
        assert digest in copier
    assert copier.count("sha256sum -c -") == 2


def test_relay_authenticates_the_input_upstream_with_the_viewer_capability():
    relay = _read(REPO / "substrate" / "stateful-relay" / "index.mjs")
    assert 'const viewerCapability = ddbAttrString(current.bridge_capability)' in relay
    assert "getSession(claims).viewerCapability = viewerCapability" in relay
    assert 'JSON.stringify({ t: "auth", mode: "viewer", key: capability })' in relay
    assert "input capability unavailable" in relay


def test_microvm_proxy_tokens_enumerate_ports_instead_of_all_ports():
    relay = _read(REPO / "substrate" / "stateful-relay" / "index.mjs")
    server = _read(REPO / "substrate" / "mcp-server" / "server.py")
    assert "{ allPorts: {} }" not in relay
    assert '{"allPorts": {}}' not in server
    assert "allowedPorts: RELAY_PROXY_PORTS.map(port => ({ port }))" in relay
    ports = relay[relay.index("const RELAY_PROXY_PORTS"):]
    ports = ports[:ports.index(")]") + 2]
    for port in ("AUDIO_PORT", "VIDEO_PORT", "INPUT_PORT", "COPLAY_PORT", '"9000"'):
        assert port in ports
    for forbidden in ("5901", "9222", "50051"):
        assert forbidden not in ports
    assert "allowedPorts=[{\"port\": p} for p in allowed_ports]" in server
