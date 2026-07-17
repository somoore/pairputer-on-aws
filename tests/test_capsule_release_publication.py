"""Immutable capsule release publication contracts."""

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STACK = (ROOT / "capsules/nested/capsule-stack.yaml").read_text()
DEPLOY = (ROOT / "substrate/deploy-capsule.sh").read_text()


def _publisher_source() -> str:
    marker = "  CapsuleReleasePublisherFunction:"
    block = STACK[STACK.index(marker):STACK.index("\n  CapsuleReleasePublisher:\n")]
    raw = block[block.index("        ZipFile: |\n") + len("        ZipFile: |\n"):]
    return "\n".join(line[10:] if line.startswith("          ") else line for line in raw.splitlines())


class _ParameterError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


class _Ssm:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.puts = []

    def get_parameter(self, *, Name):
        if Name not in self.values:
            raise _ParameterError("ParameterNotFound")
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, **kwargs):
        name, value = kwargs["Name"], kwargs["Value"]
        if not kwargs.get("Overwrite") and name in self.values:
            raise _ParameterError("ParameterAlreadyExists")
        self.values[name] = value
        self.puts.append((name, value, bool(kwargs.get("Overwrite"))))


def _load_publisher(ssm):
    # Execute the actual inline Lambda code with its AWS/network edges replaced by deterministic fakes.
    namespace = {"_ParameterError": _ParameterError}
    source = _publisher_source()
    source = source.replace("import hashlib, json, urllib.request, boto3", "import hashlib, json")
    source = source.replace("from botocore.exceptions import ClientError", "ClientError = _ParameterError")
    exec(source, namespace, namespace)
    sent = []
    namespace["boto3"] = type("Boto", (), {"client": staticmethod(lambda service: ssm)})
    namespace["send"] = lambda event, context, status, data, physical_id: sent.append((status, data))
    return namespace, sent


def _event(manifest_value="gzip+base64:manifest", image_version="7"):
    capsule = "computer-use-desktop"
    manifest_hex = hashlib.sha256(manifest_value.encode()).hexdigest()
    return {
        "RequestType": "Create",
        "StackId": "stack",
        "RequestId": "request",
        "LogicalResourceId": "CapsuleReleasePublisher",
        "ResponseURL": "https://example.invalid/response",
        "ResourceProperties": {
            "CapsuleId": capsule,
            "ImageArn": "arn:aws:lambda:us-east-1:123:microvm-image:desktop",
            "ImageVersion": image_version,
            "ManifestParameter": f"/pairputer/capsules/{capsule}/manifests/sha256-{manifest_hex}",
            "ManifestDigest": f"sha256:{manifest_hex}",
            "CurrentParameter": f"/pairputer/capsules/{capsule}/current",
            "ContextSha256": "a" * 64,
            "ContextUri": "s3://artifacts/capsule.zip",
        },
    }, manifest_value


def test_release_publisher_pins_exact_image_and_commits_current_last():
    event, manifest = _event()
    manifest_name = event["ResourceProperties"]["ManifestParameter"]
    ssm = _Ssm({manifest_name: manifest})
    code, sent = _load_publisher(ssm)
    code["handler"](event, None)

    assert sent[0][0] == "SUCCESS"
    release_name = sent[0][1]["ReleaseParameter"]
    release = json.loads(ssm.values[release_name])
    declared_digest = release.pop("releaseDigest")
    canonical = json.dumps(release, separators=(",", ":"), sort_keys=True)
    assert declared_digest == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert release["imageVersion"] == "7"
    assert release["manifestDigest"] == event["ResourceProperties"]["ManifestDigest"]

    current_name = event["ResourceProperties"]["CurrentParameter"]
    assert ssm.puts[-1][0] == current_name
    assert json.loads(ssm.values[current_name]) == {
        "schemaVersion": 1,
        "capsuleId": "computer-use-desktop",
        "releaseParameter": release_name,
        "releaseDigest": declared_digest,
    }


def test_manifest_tampering_fails_before_release_or_pointer_write():
    event, _ = _event()
    manifest_name = event["ResourceProperties"]["ManifestParameter"]
    ssm = _Ssm({manifest_name: "tampered"})
    code, sent = _load_publisher(ssm)
    code["handler"](event, None)
    assert sent[0][0] == "FAILED"
    assert "manifest digest mismatch" in sent[0][1]["Error"]
    assert not ssm.puts


def test_cloudformation_rollback_repoints_to_complete_prior_release():
    old_event, old_manifest = _event(image_version="7")
    new_event, new_manifest = _event(manifest_value="gzip+base64:new-manifest", image_version="8")
    ssm = _Ssm({
        old_event["ResourceProperties"]["ManifestParameter"]: old_manifest,
        new_event["ResourceProperties"]["ManifestParameter"]: new_manifest,
    })
    code, sent = _load_publisher(ssm)
    code["handler"](new_event, None)
    new_pointer = json.loads(ssm.values[new_event["ResourceProperties"]["CurrentParameter"]])
    assert json.loads(ssm.values[new_pointer["releaseParameter"]])["imageVersion"] == "8"

    # CloudFormation invokes Update with the old resource properties during rollback.
    old_event["RequestType"] = "Update"
    code["handler"](old_event, None)
    restored = json.loads(ssm.values[old_event["ResourceProperties"]["CurrentParameter"]])
    assert json.loads(ssm.values[restored["releaseParameter"]])["imageVersion"] == "7"
    assert sent[-1][0] == "SUCCESS"


def test_deploy_and_stack_expose_atomic_content_addressed_contract():
    assert 'MANIFEST_DIGEST="sha256:' in DEPLOY
    assert '/manifests/sha256-${MANIFEST_DIGEST#sha256:}' in DEPLOY
    assert '"CapsuleReleaseSsmParam=${CURRENT_RELEASE_SSM}"' in DEPLOY
    assert DEPLOY.index("put_immutable_parameter") < DEPLOY.index("aws cloudformation deploy")
    assert "pairputer:capsule-release-ssm" in STACK
    assert "LatestActiveImageVersion" in STACK
    assert "The one mutable write is last" in STACK
    assert "immutable SSM collision" in STACK


def test_deploy_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(ROOT / "substrate/deploy-capsule.sh")], check=True)
