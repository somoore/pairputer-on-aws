#!/usr/bin/env python3
"""
pairputer MCP server, hosted on Bedrock AgentCore Runtime.

This is the whole control + data plane, server-side. One server serves every capsule in IMAGE_REGISTRY
(DOOM/Hellbox is the default; capsule = the MicroVM image). It:
  - serves the inline player component (text/html;profile=mcp-app) so the capsule renders in the AI chat
  - runs / suspends / resumes / terminates the caller's MicroVM for a given capsule (via the IAM role)
  - wakes/sleeps the stateful ECS/Fargate relay
  - mints short-lived scoped relay session tokens

AgentCore contract (verified): FastMCP streamable-http on 0.0.0.0:8000/mcp, stateless. AgentCore
fronts this with OAuth (Cognito JWT) and provides the IAM execution role for AWS calls. Nothing
runs on the user's laptop; no secrets in any artifact.

Each authenticated Cognito principal gets an independent MicroVM per image. Session ownership lives in
DynamoDB so AgentCore restarts do not remap users to someone else's VM.
"""
import base64
from dataclasses import dataclass
from decimal import Decimal
import gzip
import hashlib
import hmac
import http.client
import io
import inspect
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

# Anchor sibling imports (hosts/) to this file's directory: `python server.py` adds it to sys.path
# already, but the test harness loads server.py via runpy.run_path, which does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hosts  # noqa: E402  (needs the sys.path anchor above)

import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

# Log to stdout (AgentCore captures it → CloudWatch). PYTHONUNBUFFERED=1 in the Dockerfile makes it
# real-time. Level via PAIRPUTER_LOG_LEVEL (default INFO). See wall #17 (the missing logging that made a
# VM-launch bug take hours). Tenant ids are logged truncated (never full) to avoid leaking identity.
logging.basicConfig(level=os.environ.get("PAIRPUTER_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pairputer")

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from cryptography.exceptions import InvalidSignature
except Exception:  # pragma: no cover - keeps local syntax checks usable before image rebuild.
    hashes = None
    serialization = None
    padding = None
    RSAPublicNumbers = None
    InvalidSignature = Exception

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
MAX_CAPSULE_MANIFEST_BYTES = 512 * 1024
# A capsule-empty substrate is valid: the platform runs, capsule tools just report "no capsules".
# PAIRPUTER_IMAGE_ARN is the default capsule fallback; empty when no reference capsule is bundled.
IMAGE_ARN = os.environ.get("PAIRPUTER_IMAGE_ARN", "")  # arn:aws:lambda:<region>:<acct>:microvm-image:doom

# The capsule registry maps a capsule id to its MicroVM image + display metadata. Two accepted forms
# (both back-compatible, so any registry JSON — old or new — works):
#   {"doom": "arn:..."}                                    (flat: id -> ARN string)
#   {"doom": {"arn": "arn:...", "name": "Hellbox (DOOM)", "description": "Real DOOM in a MicroVM"}}
# Internally we normalize to the object form so the platform is capsule-agnostic (no hardcoded "doom").
_KNOWN_NAMES = {"doom": ("Hellbox (DOOM)", "Real DOOM running in a Lambda MicroVM.")}


def _normalize_registry(raw: dict) -> dict:
    out = {}
    for cid, val in (raw or {}).items():
        if isinstance(val, dict):
            arn = val.get("arn") or val.get("imageArn") or ""
            name = val.get("name") or _KNOWN_NAMES.get(cid, (cid.title(), ""))[0]
            desc = val.get("description") or _KNOWN_NAMES.get(cid, ("", ""))[1]
        else:  # flat "arn" string
            arn = val
            name, desc = _KNOWN_NAMES.get(cid, (cid.title(), ""))
        out[cid] = {
            "arn": arn, "name": name, "description": desc,
            "releaseSsm": val.get("releaseSsm", "") if isinstance(val, dict) else "",
            "manifestSsm": val.get("manifestSsm", "") if isinstance(val, dict) else "",
        }
    return out


IMAGE_REGISTRY = _normalize_registry(json.loads(os.environ.get("PAIRPUTER_IMAGE_REGISTRY", "{}") or "{}"))
if not IMAGE_REGISTRY and IMAGE_ARN:
    IMAGE_REGISTRY = _normalize_registry({"doom": IMAGE_ARN})

# --- Capsule discovery by TAG (cartridge model, docs/capsule-architecture.md) ----------------------
# Capsules are cartridges: each is its own CloudFormation stack deployed AFTER the substrate, tagging its
# MicroVM image `pairputer:capsule=true` + id/name/description/manifest-ssm. The MCP discovers capsules by
# querying those tags at runtime, so inserting/removing a capsule needs NO control-plane redeploy. It
# lists ONLY pairputer-tagged capsule images — never MicroVM images created outside/for-use-outside
# pairputer. Discovery is a strict SUPERSET of the seed env registry (the env still works as a fallback).
CAPSULE_DISCOVERY = os.environ.get("PAIRPUTER_CAPSULE_DISCOVERY", "true") not in ("", "0", "false", "False")
_discovery_cache: tuple[float, dict] | None = None
_DISCOVERY_TTL_S = int(os.environ.get("PAIRPUTER_DISCOVERY_TTL_S", "30"))


def _discover_capsules_by_tag() -> dict:
    """Query tagged MicroVM images -> {id: {arn,name,description,manifestSsm}}. Cached (short TTL).
    Best-effort: on any error, returns {} so the env registry still serves."""
    global _discovery_cache
    if not CAPSULE_DISCOVERY or LOCAL_MODE:
        return {}
    now = time.time()
    if _discovery_cache and now - _discovery_cache[0] < _DISCOVERY_TTL_S:
        return _discovery_cache[1]
    found: dict = {}
    candidates: dict[str, list[dict]] = {}
    try:
        tagging = boto3.client("resourcegroupstaggingapi", region_name=REGION)
        paginator = tagging.get_paginator("get_resources")
        for page in paginator.paginate(
            ResourceTypeFilters=["lambda:microvm-image"],
            TagFilters=[{"Key": "pairputer:capsule", "Values": ["true"]}],
        ):
            for r in page.get("ResourceTagMappingList", []):
                arn = r.get("ResourceARN", "")
                tags = {t["Key"]: t["Value"] for t in r.get("Tags", [])}
                cid = tags.get("pairputer:capsule-id") or (arn.rsplit(":", 1)[-1] if arn else "")
                if not cid:
                    continue
                candidates.setdefault(cid, []).append({
                    "arn": arn,
                    "name": tags.get("pairputer:capsule-name") or _KNOWN_NAMES.get(cid, (cid.title(), ""))[0],
                    "description": tags.get("pairputer:capsule-description")
                                   or _KNOWN_NAMES.get(cid, ("", ""))[1],
                    "manifestSsm": tags.get("pairputer:capsule-manifest-ssm") or "",
                    "releaseSsm": tags.get("pairputer:capsule-release-ssm") or "",
                    "runtimeRole": tags.get("pairputer:capsule-runtime-role") or "",
                    # A memory-tier sibling names its base capsule + its size; it is folded INTO the
                    # base's memoryTiers below and never listed as its own capsule.
                    "memoryTierOf": tags.get("pairputer:capsule-memory-tier-of") or "",
                    "memoryMib": tags.get("pairputer:capsule-memory-mib") or "",
                })
        # Blue/green stacks can briefly expose two tagged images for one
        # capsule ID. Select only the image ARN named by the authoritative
        # atomic release pointer; paginator order must never decide authority.
        ssm = boto3.client("ssm", region_name=REGION)
        for cid, options in candidates.items():
            if len(options) == 1:
                found[cid] = options[0]
                continue
            expected_pointer = f"/pairputer/capsules/{cid}/current"
            if any(option.get("releaseSsm") != expected_pointer for option in options):
                log.error("capsule discovery omitted duplicate id=%s with conflicting release pointers", cid)
                continue
            pointer = json.loads(ssm.get_parameter(Name=expected_pointer)["Parameter"]["Value"])
            release_name = str(pointer.get("releaseParameter") or "")
            release = json.loads(ssm.get_parameter(Name=release_name)["Parameter"]["Value"])
            desired_arn = str(release.get("imageArn") or "")
            matches = [option for option in options if option.get("arn") == desired_arn]
            if len(matches) != 1:
                log.error("capsule discovery omitted duplicate id=%s without one pointer-selected ARN", cid)
                continue
            found[cid] = matches[0]
        # Fold memory-tier siblings into their base capsule's memoryTiers and drop them from the
        # top-level list (a tier is a size of an existing capsule, not a separate capsule). The tier
        # id (its own image name) is what play_capsule(memory_mib=...) relaunches into.
        tiers: dict[str, dict] = {}
        for cid in list(found):
            base = found[cid].get("memoryTierOf")
            mib = found[cid].get("memoryMib")
            if base and mib and str(mib).isdigit():
                tiers.setdefault(base, {})[str(int(mib))] = cid
        for base, tier_map in tiers.items():
            if base in found:
                found[base]["memoryTiers"] = tier_map
                for tier_id in tier_map.values():
                    found.pop(tier_id, None)  # hide the sibling from list_capsules
        log.info("capsule discovery: %d pairputer-tagged capsule image(s)", len(found))
    except Exception as exc:  # never let discovery break the server — env registry still serves
        log.warning("capsule discovery failed (using env registry only): %s", exc)
        return _discovery_cache[1] if _discovery_cache else {}
    _discovery_cache = (now, found)
    return found


def _effective_registry() -> dict:
    """Env registry (seed) merged with tag-discovered capsules (discovered wins — it's live truth)."""
    reg = dict(IMAGE_REGISTRY)
    reg.update(_discover_capsules_by_tag())
    return reg


def _decode_manifest_parameter(raw: str) -> dict:
    """Decode a bounded plain-JSON or gzip+base64 capsule manifest.

    SSM advanced parameters stop at 8 KiB, while a useful typed tool catalog can
    be larger. Compression is an encoding only: the manifest is still validated
    by the deployer before upload and is bounded again before JSON parsing here.
    """
    if raw.startswith("gzip+base64:"):
        compressed = base64.b64decode(raw.removeprefix("gzip+base64:"), validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            decoded = stream.read(MAX_CAPSULE_MANIFEST_BYTES + 1)
    else:
        decoded = raw.encode("utf-8")
    if len(decoded) > MAX_CAPSULE_MANIFEST_BYTES:
        raise ValueError("capsule manifest exceeds the decompressed size limit")
    value = json.loads(decoded)
    if not isinstance(value, dict):
        raise ValueError("capsule manifest must be a JSON object")
    return value


def _ssm_parameter_value(param_name: str) -> str:
    if not param_name or LOCAL_MODE:
        raise ValueError("SSM parameter name is required")
    return str(boto3.client("ssm", region_name=REGION).get_parameter(
        Name=param_name,
    )["Parameter"]["Value"])


_MANIFEST_CHUNK_HEADER = "chunked:v1:"
_MAX_MANIFEST_PARTS = 16


def _expand_chunked_manifest(param_name: str, raw: str) -> str:
    """Reassemble a manifest split across immutable /partN parameters (SSM caps one value at 8 KiB).

    The primary digest-addressed parameter holds `chunked:v1:<count>:<sha256hex>`, so integrity
    chains from the release digest (over that header) to the sha256 of the joined payload here.
    """
    if not raw.startswith(_MANIFEST_CHUNK_HEADER):
        return raw
    try:
        count_text, full_sha = raw.removeprefix(_MANIFEST_CHUNK_HEADER).split(":", 1)
        count = int(count_text)
    except ValueError as exc:
        raise ValueError("chunked manifest header is malformed") from exc
    if not 1 <= count <= _MAX_MANIFEST_PARTS or not re.fullmatch(r"[a-f0-9]{64}", full_sha):
        raise ValueError("chunked manifest header is out of bounds")
    joined = "".join(_ssm_parameter_value(f"{param_name}/part{i}") for i in range(count))
    if hashlib.sha256(joined.encode("utf-8")).hexdigest() != full_sha:
        raise ValueError("chunked manifest payload digest mismatch")
    return joined


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_object_digest(value: dict, excluded: tuple[str, ...] = ()) -> str:
    canonical = {key: item for key, item in value.items() if key not in excluded}
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256_text(raw)


_release_cache: dict[str, tuple[float, dict]] = {}
_RELEASE_TTL_S = int(os.environ.get("PAIRPUTER_RELEASE_TTL_S", "30"))


def _release_for(image_id: str) -> dict:
    """Resolve one atomic, immutable capsule release; any inconsistency fails closed."""
    if LOCAL_MODE:
        return {}
    now = time.time()
    cached = _release_cache.get(image_id)
    if cached and cached[0] > now:
        return dict(cached[1])
    meta = _effective_registry().get(image_id) or {}
    pointer_name = str(meta.get("releaseSsm") or "")
    prefix = f"/pairputer/capsules/{image_id}/"
    if pointer_name != prefix + "current":
        raise RuntimeError(f"capsule {image_id!r} has no trusted immutable release pointer")
    pointer = json.loads(_ssm_parameter_value(pointer_name))
    if (not isinstance(pointer, dict) or pointer.get("schemaVersion") != 1 or
            pointer.get("capsuleId") != image_id):
        raise RuntimeError(f"capsule {image_id!r} release pointer is invalid")
    release_name = str(pointer.get("releaseParameter") or "")
    if not release_name.startswith(prefix + "releases/sha256-"):
        raise RuntimeError(f"capsule {image_id!r} release parameter is outside its immutable namespace")
    release_raw = _ssm_parameter_value(release_name)
    release = json.loads(release_raw)
    if not isinstance(release, dict):
        raise RuntimeError(f"capsule {image_id!r} release is invalid")
    claimed_release_digest = str(release.get("releaseDigest") or "")
    if (pointer.get("releaseDigest") != claimed_release_digest or
            claimed_release_digest != _canonical_object_digest(release, ("releaseDigest",))):
        raise RuntimeError(f"capsule {image_id!r} release digest mismatch")
    manifest_name = str(release.get("manifestParameter") or "")
    image_arn = str(release.get("imageArn") or "")
    image_version = str(release.get("imageVersion") or "")
    if (release.get("schemaVersion") != 1 or release.get("capsuleId") != image_id or
            image_arn != str(meta.get("arn") or "") or not image_version or
            not manifest_name.startswith(prefix + "manifests/sha256-")):
        raise RuntimeError(f"capsule {image_id!r} immutable release fields are invalid")
    manifest_raw = _ssm_parameter_value(manifest_name)
    if str(release.get("manifestDigest") or "") != _sha256_text(manifest_raw):
        raise RuntimeError(f"capsule {image_id!r} manifest digest mismatch")
    manifest_doc = _decode_manifest_parameter(_expand_chunked_manifest(manifest_name, manifest_raw))
    manifest = manifest_doc.get("capsule", manifest_doc)
    if not isinstance(manifest, dict) or str(manifest.get("id") or "") != image_id:
        raise RuntimeError(f"capsule {image_id!r} manifest identity mismatch")
    resolved = {**release, "manifest": manifest, "releaseParameter": release_name}
    _release_cache[image_id] = (now + _RELEASE_TTL_S, dict(resolved))
    return resolved


def _read_manifest_from_ssm(param_name: str) -> dict:
    """Fetch a capsule's capability manifest JSON from its SSM param. Best-effort -> {} on any error."""
    if not param_name or LOCAL_MODE:
        return {}
    try:
        val = _expand_chunked_manifest(param_name, _ssm_parameter_value(param_name))
        m = _decode_manifest_parameter(val)
        return m.get("capsule", m) if isinstance(m, dict) else {}
    except Exception as exc:  # a missing/broken manifest just means that capsule stays Tier 0
        log.warning("manifest read failed for %s (capsule stays agent-inert): %s", param_name, exc)
        return {}
VIDEO_PORT = int(os.environ.get("PAIRPUTER_VIDEO_PORT", "6903"))
SESSION_SECRET_ARN = os.environ.get("PAIRPUTER_SESSION_SECRET_ARN", "")
CF_SIGNING_PRIVATE_KEY_SECRET_ARN = os.environ.get("PAIRPUTER_CLOUDFRONT_SIGNING_PRIVATE_KEY_SECRET_ARN", "")
CF_PUBLIC_KEY_ID = os.environ.get("PAIRPUTER_CLOUDFRONT_PUBLIC_KEY_ID", "")
RELAY_CLUSTER = os.environ.get("PAIRPUTER_RELAY_CLUSTER", "")
RELAY_SERVICE = os.environ.get("PAIRPUTER_RELAY_SERVICE", "")
RELAY_TARGET_GROUP_ARN = os.environ.get("PAIRPUTER_RELAY_TARGET_GROUP_ARN", "")
SESSION_TABLE_NAME = os.environ.get("PAIRPUTER_SESSION_TABLE", "")
try:
    RELAY_WARM_SECONDS = int(os.environ.get("PAIRPUTER_RELAY_WARM_SECONDS", "-1"))
except ValueError:
    RELAY_WARM_SECONDS = -1
_session_secret = None  # lazy-loaded + cached
_cf_private_key = None  # lazy-loaded + cached
_MIN_SESSION_SECRET_BYTES = 32
RELAY_ACTIVE_SHARDS = 64

# --- LOCAL MODE (dev loop, roadmap F) ----------------------------------------------------------------
# PAIRPUTER_LOCAL_MODE=1 runs this exact server against a capsule in LOCAL DOCKER instead of a Lambda
# MicroVM: VM launch/discovery is stubbed to a single always-RUNNING local capsule, the agent bridge and
# relay target localhost, and Cognito JWT auth is relaxed to a fixed dev identity. Same tools, same
# widget, same manifest — so what works locally works on AWS. AWS becomes the last validation step, not
# the debug loop. Nothing here runs unless the flag is set (production is byte-for-byte unchanged).
LOCAL_MODE = os.environ.get("PAIRPUTER_LOCAL_MODE", "") not in ("", "0", "false", "False")
# Where the local capsule's ports are reachable from this process (host.docker.internal on Docker Desktop,
# or localhost if the server runs on the host). Bridge/video/relay ports are the capsule's :6905/:6903.
LOCAL_CAPSULE_HOST = os.environ.get("PAIRPUTER_LOCAL_CAPSULE_HOST", "127.0.0.1")
LOCAL_BRIDGE_PORT = int(os.environ.get("PAIRPUTER_LOCAL_BRIDGE_PORT", "6905"))
LOCAL_BRIDGE_CAPABILITY = os.environ.get("PAIRPUTER_LOCAL_BRIDGE_CAPABILITY", "").strip()
BRIDGE_REQUEST_MAX_BYTES = int(os.environ.get("PAIRPUTER_BRIDGE_REQUEST_MAX_BYTES", str(9 * 1024 * 1024)))
BRIDGE_RESPONSE_MAX_BYTES = int(os.environ.get("PAIRPUTER_BRIDGE_RESPONSE_MAX_BYTES", str(2 * 1024 * 1024)))
LOCAL_TENANT = "local-dev-tenant"


@dataclass(frozen=True)
class CallerIdentity:
    tenant_id: str
    issuer: str
    sub: str
    client_id: str
    username: str
    email: str
    scope: str
    token_use: str

    def public(self) -> dict:
        return {
            "tenantId": self.tenant_id,
            "issuer": self.issuer,
            "sub": self.sub,
            "clientId": self.client_id,
            "username": self.username,
            "email": self.email,
            "scope": self.scope,
            "tokenUse": self.token_use,
        }


def _get_session_secret() -> bytes:
    """Fetch and validate the shared HMAC secret used by MCP and relay.

    An unsigned token is a local-development convention only.  Production must
    not silently substitute a process-local key: that would split approval and
    relay authority across runtime processes and could mask a broken Secrets
    Manager deployment until a user reaches the data plane.
    """
    global _session_secret
    if _session_secret is None:
        if not SESSION_SECRET_ARN:
            if LOCAL_MODE:
                return b""
            raise RuntimeError("production relay session secret ARN is not configured")
        sm = boto3.client("secretsmanager", region_name=REGION)
        value = sm.get_secret_value(SecretId=SESSION_SECRET_ARN)["SecretString"].encode()
        if len(value) < _MIN_SESSION_SECRET_BYTES:
            raise RuntimeError("relay session secret is missing or too short")
        _session_secret = value
    return _session_secret


def _get_cf_private_key():
    """Fetch and parse the CloudFront signing private key (cached)."""
    global _cf_private_key
    if not (CF_SIGNING_PRIVATE_KEY_SECRET_ARN and CF_PUBLIC_KEY_ID):
        return None
    if serialization is None:
        raise RuntimeError("cryptography is required for CloudFront signed relay URLs")
    if _cf_private_key is None:
        sm = boto3.client("secretsmanager", region_name=REGION)
        pem = sm.get_secret_value(SecretId=CF_SIGNING_PRIVATE_KEY_SECRET_ARN)["SecretString"].encode()
        _cf_private_key = serialization.load_pem_private_key(pem, password=None)
    return _cf_private_key


def _b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * ((4 - len(raw) % 4) % 4))


# --- Defense-in-depth JWT verification ------------------------------------------------------------
# AgentCore's CustomJWTAuthorizer already verifies signature/issuer/expiry/client/scope against the
# Cognito JWKS BEFORE a request reaches this container (agentcore.yaml CustomJWTAuthorizer). This is a
# BELT to that suspenders: if PAIRPUTER_JWT_DISCOVERY_URL is set, we independently re-verify the RS256
# signature + iss + exp + token_use here, so the tenant model does NOT depend solely on AgentCore
# being the only ingress. If the discovery URL is unset (LOCAL_MODE, or a not-yet-migrated deploy),
# we skip verification and rely on AgentCore alone — logging once so it's visible.
JWT_DISCOVERY_URL = os.environ.get("PAIRPUTER_JWT_DISCOVERY_URL", "").strip()
_JWKS_CACHE: dict = {"keys": {}, "issuer": "", "fetched_at": 0.0}
_JWKS_TTL_S = 3600
_JWKS_LOCK = threading.Lock()
_JWT_VERIFY_WARNED = [False]


def _oidc_config() -> dict:
    """Fetch the OIDC discovery doc (issuer + jwks_uri). Cached inside _refresh_jwks."""
    with urllib.request.urlopen(JWT_DISCOVERY_URL, timeout=5) as resp:
        return json.loads(resp.read(65536))


def _refresh_jwks(force: bool = False) -> dict:
    """Return {kid: RSAPublicKey} from Cognito's JWKS, cached for _JWKS_TTL_S. Refetched on a cache
    miss (key rotation) when force=True."""
    with _JWKS_LOCK:
        fresh = (time.time() - _JWKS_CACHE["fetched_at"]) < _JWKS_TTL_S
        if _JWKS_CACHE["keys"] and fresh and not force:
            return _JWKS_CACHE["keys"]
        cfg = _oidc_config()
        issuer = str(cfg.get("issuer") or "")
        jwks_uri = str(cfg.get("jwks_uri") or "")
        with urllib.request.urlopen(jwks_uri, timeout=5) as resp:
            jwks = json.loads(resp.read(262144))
        keys = {}
        for jwk in jwks.get("keys", []):
            kid = jwk.get("kid")
            if not kid or jwk.get("kty") != "RSA":
                continue
            n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
            e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
            keys[kid] = RSAPublicNumbers(e, n).public_key()
        _JWKS_CACHE.update({"keys": keys, "issuer": issuer, "fetched_at": time.time()})
        return keys


def _verify_jwt(token: str) -> None:
    """Fail-closed RS256 verification of the bearer JWT against Cognito's JWKS. No-op when no discovery
    URL is configured (relies on AgentCore alone, warned once). Raises PermissionError on any failure."""
    if not JWT_DISCOVERY_URL or LOCAL_MODE:
        if not _JWT_VERIFY_WARNED[0]:
            log.warning("PAIRPUTER_JWT_DISCOVERY_URL unset — relying on AgentCore's authorizer only "
                        "(no in-container JWT verification)")
            _JWT_VERIFY_WARNED[0] = True
        return
    if RSAPublicNumbers is None:
        raise PermissionError("cryptography is unavailable; cannot verify the bearer JWT")
    parts = token.split(".")
    if len(parts) != 3:
        raise PermissionError("bearer token is not a signed JWT")
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        signature = _b64url_decode(parts[2])
    except Exception as exc:
        raise PermissionError("could not parse the bearer JWT") from exc
    if header.get("alg") != "RS256":
        raise PermissionError(f"unexpected JWT alg {header.get('alg')!r}; expected RS256")
    kid = header.get("kid") or ""
    key = _refresh_jwks().get(kid)
    if key is None:
        try:
            key = _refresh_jwks(force=True).get(kid)  # refetch once on unknown kid (key rotation)
        except Exception:
            key = None
    if key is None:
        raise PermissionError("JWT signing key not found in the issuer JWKS")
    signed = (parts[0] + "." + parts[1]).encode("ascii")
    try:
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise PermissionError("JWT signature verification failed") from exc
    # Claim checks: issuer must match discovery, token not expired, and it's an access token.
    expected_issuer = _JWKS_CACHE.get("issuer") or ""
    if expected_issuer and str(payload.get("iss") or "") != expected_issuer:
        raise PermissionError("JWT issuer does not match the configured identity provider")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or time.time() > float(exp) + 60:  # 60s clock skew
        raise PermissionError("JWT is expired")
    if str(payload.get("token_use") or "") not in ("access", ""):  # Cognito access tokens; "" tolerant
        raise PermissionError("JWT is not an access token")


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("bearer token is not a JWT")
    try:
        return json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception as exc:
        raise ValueError("could not decode bearer JWT payload") from exc


def _request_header(ctx: Context | None, name: str) -> str:
    if not ctx:
        return ""
    try:
        request = ctx.request_context.request
    except Exception:
        return ""
    headers = getattr(request, "headers", None)
    if not headers:
        return ""
    try:
        return headers.get(name, "") or headers.get(name.lower(), "") or headers.get(name.title(), "")
    except AttributeError:
        lower = name.lower()
        for key, value in dict(headers).items():
            if str(key).lower() == lower:
                return str(value)
    return ""


def _caller_identity(ctx: Context | None) -> CallerIdentity:
    if LOCAL_MODE:
        # Fixed dev identity — no Cognito in the local loop. Same shape as a real caller.
        return CallerIdentity(tenant_id=LOCAL_TENANT, issuer="local", sub="dev",
                              client_id="local", username="dev", email="dev@local",
                              scope="local", token_use="access")
    auth = _request_header(ctx, "authorization")
    if not auth and os.environ.get("PAIRPUTER_DEV_BEARER_TOKEN"):
        auth = "Bearer " + os.environ["PAIRPUTER_DEV_BEARER_TOKEN"]
    if not auth.lower().startswith("bearer "):
        raise PermissionError(
            "missing forwarded Authorization header; ensure AgentCore RequestHeaderAllowlist includes Authorization"
        )
    token = auth.split(None, 1)[1].strip()
    # Defense-in-depth: re-verify the signature/iss/exp here (fail-closed) so the tenant model does not
    # rely SOLELY on AgentCore being the only ingress. No-op when no discovery URL is configured.
    _verify_jwt(token)
    claims = _decode_jwt_payload(token)
    issuer = str(claims.get("iss") or "")
    subject = str(claims.get("sub") or claims.get("username") or claims.get("client_id") or "")
    if not issuer or not subject:
        raise PermissionError("authenticated token is missing issuer or principal subject")
    tenant_id = hashlib.sha256(f"{issuer}:{subject}".encode("utf-8")).hexdigest()
    aud = claims.get("aud") or ""
    if isinstance(aud, list):
        aud = ",".join(str(x) for x in aud)
    return CallerIdentity(
        tenant_id=tenant_id,
        issuer=issuer,
        sub=subject,
        client_id=str(claims.get("client_id") or aud or ""),
        username=str(claims.get("username") or claims.get("cognito:username") or ""),
        email=str(claims.get("email") or ""),
        scope=str(claims.get("scope") or ""),
        token_use=str(claims.get("token_use") or ""),
    )


SESSION_TOKEN_TTL_SECONDS = 15 * 60
MICROVM_MAX_DURATION_SECONDS = 8 * 60 * 60
# Operator ceiling for idle-suspend. A user may pick a TIGHTER auto-suspend in the widget (cheaper /
# faster pause), never looser than this. Default 5 min.
MICROVM_MAX_IDLE_SECONDS = int(os.environ.get("PAIRPUTER_MAX_IDLE_SECONDS", str(5 * 60)))
# Lower bound so a user can't set an absurdly aggressive suspend that thrashes.
MICROVM_MIN_IDLE_SECONDS = int(os.environ.get("PAIRPUTER_MIN_IDLE_SECONDS", "60"))
MICROVM_SUSPENDED_DURATION_SECONDS = 8 * 60 * 60
# Rough per-second cost of a RUNNING MicroVM, for the widget's "~$X this session" READOUT only (never
# billing-authoritative). Operator can set it to their real rate; default is a conservative 8 GiB est.
MICROVM_COST_PER_SECOND_USD = float(os.environ.get("PAIRPUTER_VM_COST_PER_SECOND_USD", "0.0000556"))  # ~$0.20/hr


def _effective_idle_seconds(item: dict | None) -> int:
    """The auto-suspend timeout applied at run/resume. A user may store a TIGHTER preference on their
    session (widget: 'suspend after 5m/15m/1h'); it is clamped to [MIN, operator MAX ceiling] so a user
    can only ever suspend SOONER than the operator allows, never later. Unset -> the operator default."""
    pref = (item or {}).get("idle_seconds_pref")
    try:
        pref = int(pref) if pref else 0
    except (TypeError, ValueError):
        pref = 0
    if pref <= 0:
        return MICROVM_MAX_IDLE_SECONDS
    return max(MICROVM_MIN_IDLE_SECONDS, min(pref, MICROVM_MAX_IDLE_SECONDS))


def _env_connector_list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


INGRESS_NETWORK_CONNECTORS = _env_connector_list(
    "PAIRPUTER_INGRESS_NETWORK_CONNECTORS",
    f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS",
)
EGRESS_NETWORK_CONNECTORS = _env_connector_list(
    "PAIRPUTER_EGRESS_NETWORK_CONNECTORS",
    f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS",
)


def _mint_session_token(
    *,
    tenant_id: str,
    microvm_id: str,
    image_id: str,
    session_id: str,
    session_version: int = 1,
    release_digest: str = "",
    manifest_digest: str = "",
    image_arn: str = "",
    image_version: str = "",
    frozen: bool = False,
    ttl_seconds: int = SESSION_TOKEN_TTL_SECONDS,
    exp: int | None = None,
) -> str:
    """Sign a scoped relay token. The browser can see it, so keep it short-lived and narrow."""
    secret = _get_session_secret()
    if not secret:
        return ""  # explicit local development only; production retrieval fails closed above
    exp = exp or (int(time.time()) + ttl_seconds)
    payload = {
        "tenantId": tenant_id,
        "sessionId": session_id,
        "sessionVersion": int(session_version or 1),
        "microvmId": microvm_id,
        "imageId": image_id,
        "releaseDigest": release_digest,
        "manifestDigest": manifest_digest,
        "imageArn": image_arn,
        "imageVersion": image_version,
        "exp": exp,
        "frozen": frozen,
        "channels": ["player", "state", "control", "video", "audio", "input"],
        # interaction.md: the session token states whether THIS capsule permits agent interaction —
        # defense-in-depth for the data plane (the server-side tool gate is the primary enforcement).
        # Resolved lazily: the per-image capability map is built after this function is defined.
        "agentInteract": bool(globals().get("_AGENT_ALLOWED_BY_IMAGE", {}).get(image_id, False)),
    }
    if not LOCAL_MODE and not all((release_digest, manifest_digest, image_arn, image_version)):
        raise RuntimeError("refusing to mint a relay token without an immutable release binding")
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
    payload_b64 = b(payload_bytes)
    sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{b(sig)}"


def _cloudfront_b64(raw: bytes) -> str:
    """CloudFront signed URL base64 flavor."""
    return (base64.b64encode(raw).decode()
            .replace("+", "-")
            .replace("=", "_")
            .replace("/", "~"))


def _cloudfront_signed_params(exp: int) -> str:
    """Return CloudFront custom-policy signed URL params for the relay origin."""
    key = _get_cf_private_key()
    if not (key and VIDEO_RELAY_URL and CF_PUBLIC_KEY_ID):
        return ""
    policy = {
        "Statement": [{
            "Resource": VIDEO_RELAY_URL.rstrip("/") + "/*",
            "Condition": {"DateLessThan": {"AWS:EpochTime": exp}},
        }],
    }
    policy_bytes = json.dumps(policy, separators=(",", ":")).encode()
    signature = key.sign(policy_bytes, padding.PKCS1v15(), hashes.SHA256())
    return urllib.parse.urlencode({
        "Policy": _cloudfront_b64(policy_bytes),
        "Signature": _cloudfront_b64(signature),
        "Key-Pair-Id": CF_PUBLIC_KEY_ID,
        "Hash-Algorithm": "SHA256",
    })


_NO_CAPSULES = (
    "no capsules are deployed on this pairputer substrate. Deploy a capsule (or redeploy with the reference "
    "DOOM capsule bundled) and register it in PAIRPUTER_IMAGE_REGISTRY, then try again."
)


def _default_image_id() -> str:
    """The capsule to act on when the caller doesn't name one: the SOLE registered capsule.

    Capsule-agnostic — no hardcoded 'doom'. Errors cleanly on a bare substrate. With MULTIPLE
    capsules deployed an empty image_id REFUSES with the list instead of silently picking the first:
    a host that launched DOOM when the human asked for the Workbench (observed live, 2026-07-11) got
    no error to self-correct from — models reliably recover from a listed-choices error, never from
    silent-wrong."""
    reg = _effective_registry()
    if not reg:
        raise ValueError(_NO_CAPSULES)
    if len(reg) > 1:
        choices = "; ".join(f'{cid} ("{_capsule_name(cid)}")' for cid in reg)
        raise ValueError(
            f"multiple capsules are deployed — pass image_id explicitly. Available: {choices}")
    return next(iter(reg))


def _resolve_image_id(image_id: str | None) -> str:
    if not image_id:
        return _default_image_id()
    reg = _effective_registry()
    if image_id in reg:
        return image_id
    # Forgiving resolution for the names humans/models actually say ("workbench", "Agent DOOM"):
    # case-insensitive id match, then a UNIQUE substring match across ids + display names. Ambiguity
    # or no match still errors with the list — never a silent guess.
    wanted = image_id.strip().lower()
    exact = [cid for cid in reg if cid.lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [cid for cid in reg
             if wanted in cid.lower() or wanted in _capsule_name(cid).lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    available = "; ".join(f'{cid} ("{_capsule_name(cid)}")' for cid in reg) or "(none)"
    raise ValueError(f"unknown capsule image_id {image_id!r}; available: {available}")


def _resolve_recovery_image_id(identity: "CallerIdentity", image_id: str | None) -> str:
    """image_id resolver for widget recovery ops (thaw/freeze/pairputer_session).

    Same as _resolve_image_id, but when image_id is empty AND multiple capsules are deployed, recover
    the capsule from THIS caller's own live VM instead of refusing. The widget can remount without its
    imageId (a suspended card's replayed toolOutput carries none), so Thaw would otherwise hit the
    multi-capsule guard even though the caller has exactly one VM to resume — never ambiguous. Falls
    back to _resolve_image_id (which refuses with the list) if the caller has zero, or more than one,
    capsule with a live microvm. Best-effort: any lookup failure falls through to the strict resolver."""
    if image_id or LOCAL_MODE:
        return _resolve_image_id(image_id)
    reg = _effective_registry()
    if len(reg) <= 1:
        return _resolve_image_id(image_id)  # sole/no capsule: existing behavior
    try:
        table = _session_table()
        kwargs = {
            "KeyConditionExpression": Key("pk").eq(_session_pk(identity)) & Key("sk").begins_with("IMAGE#"),
            "FilterExpression": Attr("microvm_id").exists(),
        }
        live = []
        while True:
            out = table.query(**kwargs)
            for it in out.get("Items", []):
                if it.get("microvm_id") and str(it.get("state") or "") in ("RUNNING", "SUSPENDED"):
                    cid = it.get("image_id")
                    if cid in reg:
                        live.append(cid)
            last = out.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        uniq = sorted(set(live))
        if len(uniq) == 1:
            return uniq[0]
    except Exception as exc:  # never let recovery lookup mask the real resolver
        log.warning("recovery image_id lookup failed: %s", type(exc).__name__)
    return _resolve_image_id(image_id)  # zero/ambiguous -> refuse with the list, as before


def _tier_image_id(image_id: str, memory_mib: int | None) -> str:
    """Resolve a capsule to its memory-tier sibling image. Memory is fixed at MicroVM-image build
    time (RunMicrovm has no memory param), so a bigger tier is a SEPARATE image built from the same
    context. A capsule advertises its tiers as meta['memoryTiers'] = {"8192": id, "16384": id16g};
    without that map (or an unknown size) the base image_id is used unchanged."""
    if not memory_mib:
        return image_id
    tiers = (_effective_registry().get(image_id) or {}).get("memoryTiers") or {}
    # tiers keyed by MiB (str or int); pick an exact match, else the base image.
    for key, tier_id in tiers.items():
        if int(key) == int(memory_mib) and tier_id in _effective_registry():
            return tier_id
    return image_id


def _image_arn(image_id: str) -> str:
    if not LOCAL_MODE:
        return str(_release_for(image_id)["imageArn"])
    reg = _effective_registry()
    if not reg:
        raise ValueError(_NO_CAPSULES)
    try:
        return reg[image_id]["arn"]
    except KeyError as exc:
        available = ", ".join(sorted(reg)) or "(none)"
        raise ValueError(f"unknown image_id: {image_id}. Available capsules: {available}") from exc


def _capsule_run_role(image_id: str = "") -> str:
    """The MicroVM execution role THIS capsule's manifest declares (interaction.md permissions.iamRole).
    Empty / 'none' -> no AWS access (the DOOM default). A bare name resolves via the deploy-supplied
    PAIRPUTER_CAPSULE_ROLE_ARN_MAP (name -> ARN); a full arn: is used as-is. The special value 'logs'
    resolves to the capsule stack's own least-priv CloudWatch-logs role (name key "<image_id>:logs" or
    "logs" in the map). Per-image so a cartridge (manifest from SSM) gets its own role, not the env one."""
    # Prefer the per-image manifest (cartridge, from SSM); fall back to the env manifest (bundled).
    try:
        man = _manifest_for(image_id) if image_id else {}
    except Exception:
        man = {}
    if not man:
        man = (_decode_manifest_parameter(os.environ.get("PAIRPUTER_CAPSULE_MANIFEST", "") or "{}").get("capsule", {}) or {})
    role = str(((man.get("permissions") or {}).get("iamRole")) or "").strip()
    if not role or role.lower() == "none":
        return ""
    if role.startswith("arn:"):
        return role
    # 'logs' -> the capsule stack's own least-priv runtime role, discovered from the image tag (cartridge
    # model: no MCP redeploy when a capsule ships later). This is the only AWS access DOOM gets.
    if role.lower() == "logs" and image_id:
        tag_role = (_discover_capsules_by_tag().get(image_id) or {}).get("runtimeRole") or ""
        if tag_role:
            return tag_role
    try:
        arn_map = json.loads(os.environ.get("PAIRPUTER_CAPSULE_ROLE_ARN_MAP", "") or "{}")
    except Exception:
        return ""
    # Fallback: a deploy-supplied name->ARN map (per-capsule key first).
    return str(arn_map.get(f"{image_id}:{role}") or arn_map.get(role) or "")


def _capsule_name(image_id: str) -> str:
    """Friendly display name for a capsule id (falls back to the id if unregistered)."""
    entry = _effective_registry().get(image_id)
    return (entry.get("name") if entry else None) or image_id


# STARTING/RUNNING/SUSPENDED/... -> a word a human reads. Keeps the chat status line clean + generic.
_FRIENDLY_STATE = {
    "STARTING": "Starting", "RUNNING": "Running", "SUSPENDED": "Frozen",
    "STOPPED": "Stopped", "TERMINATING": "Terminating", "TERMINATED": "Terminated",
}


def _friendly_state(state: str) -> str:
    return _FRIENDLY_STATE.get((state or "").upper(), (state or "Unknown").title())


def _widget_result(payload: dict, *, image_id: str = None, state: str = None) -> CallToolResult:
    """Wrap a widget payload so the CHAT shows only a clean status line, while the WIDGET still gets
    the full structuredContent (relayUrl/token/openNonce/... via window.openai.toolOutput). Without this,
    Codex renders the raw payload (tenantId, nonces, ids) as visible 'Tool Output'."""
    cid = image_id or payload.get("imageId") or ""
    st = state or payload.get("state") or payload.get("status") or ""
    line = f"{_capsule_name(cid)} — {_friendly_state(st)}"
    return CallToolResult(content=[TextContent(type="text", text=line)], structuredContent=payload)


def _now() -> int:
    return int(time.time())


def _session_table():
    if not SESSION_TABLE_NAME:
        raise RuntimeError("PAIRPUTER_SESSION_TABLE is required for per-user MicroVM sessions")
    return ddb.Table(SESSION_TABLE_NAME)


def _session_pk(identity: CallerIdentity) -> str:
    return f"TENANT#{identity.tenant_id}"


def _session_sk(image_id: str) -> str:
    return f"IMAGE#{image_id}"


def _client_error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "")
    return ""


def _session_version(item: dict) -> int:
    try:
        return int(item.get("session_version") or item.get("sessionVersion") or 1)
    except Exception:
        return 1


_SESSION_RELEASE_FIELDS = {
    "release_digest": "releaseDigest",
    "manifest_digest": "manifestDigest",
    "image_arn": "imageArn",
    "image_version": "imageVersion",
    "release_parameter": "releaseParameter",
}


def _apply_release_binding(item: dict, release: dict) -> None:
    for session_key, release_key in _SESSION_RELEASE_FIELDS.items():
        value = str(release.get(release_key) or "")
        if not value:
            raise RuntimeError(f"immutable release is missing {release_key}")
        item[session_key] = value


def _session_release_matches(item: dict, release: dict) -> bool:
    return all(str(item.get(session_key) or "") == str(release.get(release_key) or "")
               for session_key, release_key in _SESSION_RELEASE_FIELDS.items())


def _require_session_release_current(item: dict, image_id: str) -> dict:
    if LOCAL_MODE:
        return {}
    release = _release_for(image_id)
    if not _session_release_matches(item, release):
        raise RuntimeError(
            "this capsule session is legacy or pinned to a superseded release; "
            "Trash it, then launch the current verified release"
        )
    return release


def _heal_or_require_release_current(identity: "CallerIdentity", item: dict,
                                     image_id: str, release: dict) -> tuple[dict, str]:
    """Converge a stale-release session onto the current release IN THIS CALL — never error.

    THE INVARIANT (the permanent fix for the double-widget bug, 3rd occurrence 2026-07-14): an
    ensure-running call must NEVER fail because the stored session predates a capsule redeploy.
    Hosts render one widget card per tool call, so ANY first-call failure + the model's retry =
    a dead card next to a live one. Every earlier fix removed one failure mode (concurrent-launch
    race, dead VM + stale release); this closes the class:

    - stale release + DEAD/GONE VM  -> clear the record, fall through to a fresh launch;
    - stale release + LIVE VM       -> MIGRATE: trash it via the full trash path (persist-export
      barrier for a RUNNING VM, relay drain, terminate, record cleared), then fall through to a
      fresh launch on the current release. A superseded VM is a dead end by definition — every
      release-bound tool 400s against it forever — and its durable workspace/persistent/ state
      was exported at freeze (suspended) or is exported by the trash barrier now (running). The
      ephemeral remainder dies exactly as it would on the Trash the model/user was being told to
      perform anyway; the server just stops making them do the round-trip.
    - probe error other than not-found: treat the VM as live and migrate — the trash path itself
      fails loudly if AWS is truly unreachable, so nothing is silently discarded.

    Returns (item, vm_id); vm_id == "" means the session was healed/migrated and holds no VM."""
    vm_id = str(item.get("microvm_id") or "")
    if LOCAL_MODE or not vm_id or _session_release_matches(item, release):
        return item, vm_id
    alive = True
    try:
        state = mvm.get_microvm(microvmIdentifier=vm_id).get("state", "UNKNOWN")
        alive = state not in ("TERMINATED", "FAILED")
    except Exception as exc:
        if _client_error_code(exc) in ("ResourceNotFoundException", "NotFoundException"):
            alive = False
    if alive:
        log.info("migrating stale-release session: trashing live VM id=%s image=%s tenant=%s",
                 vm_id, image_id, identity.tenant_id[:12])
        _trash_microvm(identity, image_id)
        return _load_session(identity, image_id), ""
    log.info("healed stale-release session with dead VM id=%s image=%s tenant=%s",
             vm_id, image_id, identity.tenant_id[:12])
    return _clear_vm(item, state="STOPPED"), ""


def _new_session(identity: CallerIdentity, image_id: str) -> dict:
    now = _now()
    return {
        "pk": _session_pk(identity),
        "sk": _session_sk(image_id),
        "tenant_id": identity.tenant_id,
        "user_sub": identity.sub,
        "issuer": identity.issuer,
        "client_id": identity.client_id,
        "username": identity.username,
        "email": identity.email,
        "image_id": image_id,
        "session_id": uuid.uuid4().hex,
        "session_version": 1,
        "record_version": 1,
        "state": "STOPPED",
        "frozen": False,
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "relay_warm_until": 0,
        "ttl": now + 30 * 24 * 60 * 60,
    }


def _touch_session_identity(item: dict, identity: CallerIdentity) -> None:
    item["tenant_id"] = identity.tenant_id
    item["user_sub"] = identity.sub
    item["issuer"] = identity.issuer
    item["client_id"] = identity.client_id
    item["username"] = identity.username
    item["email"] = identity.email
    item["last_seen_at"] = _now()
    item["ttl"] = _now() + 30 * 24 * 60 * 60


def _apply_session_indexes(item: dict) -> None:
    microvm_id = item.get("microvm_id")
    if microvm_id:
        item["gsi1pk"] = f"MICROVM#{microvm_id}"
        item["gsi1sk"] = f"SESSION#{item.get('session_id') or ''}"
    else:
        item.pop("gsi1pk", None)
        item.pop("gsi1sk", None)

    now = _now()
    relay_warm_until = int(item.get("relay_warm_until") or 0)
    if RELAY_WARM_SECONDS >= 0 and (item.get("state") == "RUNNING" or relay_warm_until > now):
        tenant = str(item.get("tenant_id") or "0")
        try:
            shard = int(tenant[:8], 16) % RELAY_ACTIVE_SHARDS
        except ValueError:
            shard = int(hashlib.sha256(tenant.encode()).hexdigest()[:8], 16) % RELAY_ACTIVE_SHARDS
        item["gsi2pk"] = f"RELAY#ACTIVE#{shard:02d}"
        sort_until = 9999999999 if item.get("state") == "RUNNING" else relay_warm_until
        item["gsi2sk"] = f"{sort_until:010d}#{item.get('tenant_id')}#{item.get('image_id')}"
    else:
        item.pop("gsi2pk", None)
        item.pop("gsi2sk", None)


class SessionConflict(RuntimeError):
    """Optimistic session write lost a race with lifecycle or token rotation."""


def _accumulate_running_seconds(item: dict) -> None:
    """Accumulate RUNNING wall-seconds onto the session row so the widget can show usage. Centralized in
    _save_session so EVERY state transition is caught with no per-call-site instrumentation. Uses a
    dedicated `running_since` marker (updated_at is bumped on every save, so it can't stand in for it).
    Cost estimate is a readout only — never billing-authoritative."""
    now = _now()
    state = str(item.get("state") or "")
    since = item.get("running_since")
    if state == "RUNNING":
        if not since:
            item["running_since"] = now              # entered RUNNING
    else:
        if since:                                    # left RUNNING -> bank the interval
            try:
                # The record can sit "RUNNING" for hours after AWS idle-suspended the VM (billing
                # paused) if nobody touched the session — banking wall-clock here would count the
                # whole suspended gap as running (live-QA: "this session: 808m" on a box that ran
                # minutes). Bank only up to the last proof of life (updated_at is the previous
                # save's stamp; _save_session bumps it AFTER this hook) plus the idle window —
                # past that point the VM could not still have been running unattended.
                last_seen = int(item.get("updated_at") or now)
                cap_end = min(now, max(int(since), last_seen) + _effective_idle_seconds(item))
                item["running_seconds"] = int(item.get("running_seconds") or 0) + max(0, cap_end - int(since))
            except (TypeError, ValueError):
                pass
            item["running_since"] = 0
    # ONLY the two integer facts are persisted (running_seconds, running_since). The derived total and
    # the $ estimate are computed at READ time in session_settings — never stored, because a Python
    # float breaks DynamoDB put_item ("Float types are not supported; use Decimal").


def _save_session(item: dict) -> dict:
    _accumulate_running_seconds(item)
    previous_updated_at = item.get("updated_at")
    previous_record_version = item.get("record_version")
    try:
        previous_record_version = int(previous_record_version) if previous_record_version is not None else None
    except (TypeError, ValueError):
        raise SessionConflict("session record version is invalid")
    item["record_version"] = (previous_record_version or 0) + 1
    item["updated_at"] = _now()
    _apply_session_indexes(item)
    kwargs = {
        "Item": item,
        "ConditionExpression": (
            Attr("record_version").eq(previous_record_version)
            if previous_record_version is not None else Attr("record_version").not_exists()
        ),
    }
    try:
        _session_table().put_item(**kwargs)
    except ClientError as exc:
        if previous_record_version is None:
            item.pop("record_version", None)
        else:
            item["record_version"] = previous_record_version
        item["updated_at"] = previous_updated_at
        if _client_error_code(exc) == "ConditionalCheckFailedException":
            raise SessionConflict("session changed concurrently; retry from a fresh state") from exc
        raise
    return item


def _ddb_map(value: dict) -> dict:
    global _ddb_serializer
    if _ddb_serializer is None:
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        _ddb_serializer = (TypeSerializer(), TypeDeserializer())
    serializer, deserializer = _ddb_serializer
    # boto3 Table resources return plain Python values, but a defensive retry
    # path or test double may hand us one already-deserialized AttributeValue
    # map. Normalize those values before the low-level transaction client so a
    # key can never become {"M": {"S": ...}} (DynamoDB reports that as a
    # confusing "key expected S actual M" validation error).
    typed = {"S", "N", "B", "BOOL", "NULL", "M", "L", "SS", "NS", "BS"}
    plain = {}
    for key, item in value.items():
        if isinstance(item, dict) and len(item) == 1 and next(iter(item)) in typed:
            item = deserializer.deserialize(item)
        plain[str(key)] = serializer.serialize(item)
    return plain


def _ddb_value(value: Any) -> dict:
    global _ddb_serializer
    if _ddb_serializer is None:
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        _ddb_serializer = (TypeSerializer(), TypeDeserializer())
    return _ddb_serializer[0].serialize(value)


def _bind_new_vm_owner(item: dict) -> dict:
    """Atomically bind one MicroVM id to exactly one tenant/capsule session."""
    microvm_id = str(item.get("microvm_id") or "")
    if not microvm_id:
        raise ValueError("cannot bind an empty MicroVM id")
    previous_record_version = item.get("record_version")
    previous_record_version = (int(previous_record_version)
                               if previous_record_version is not None else None)
    previous_updated_at = item.get("updated_at")
    item["record_version"] = (previous_record_version or 0) + 1
    item["updated_at"] = _now()
    _apply_session_indexes(item)
    owner = {
        "pk": f"MICROVM#{microvm_id}", "sk": "OWNER",
        "tenant_id": item["tenant_id"], "image_id": item["image_id"],
        "session_id": item["session_id"], "session_version": _session_version(item),
        "microvm_id": microvm_id, "created_at": _now(),
        "ttl": _now() + MICROVM_MAX_DURATION_SECONDS + 24 * 60 * 60,
    }
    for field in _SESSION_RELEASE_FIELDS:
        value = str(item.get(field) or "")
        if not value:
            raise SessionConflict(f"MicroVM ownership binding is missing {field}")
        owner[field] = value
    condition = ("record_version = :expected" if previous_record_version is not None
                 else "attribute_not_exists(record_version)")
    session_put = {
        "TableName": SESSION_TABLE_NAME, "Item": _ddb_map(item),
        "ConditionExpression": condition,
    }
    owner_item = _ddb_map(owner)
    if previous_record_version is not None:
        session_put["ExpressionAttributeValues"] = {
            ":expected": _ddb_value(previous_record_version)
        }
    try:
        ddb_client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": SESSION_TABLE_NAME, "Item": owner_item,
                "ConditionExpression": "attribute_not_exists(pk)",
            }},
            {"Put": session_put},
        ])
    except Exception as exc:
        if previous_record_version is None:
            item.pop("record_version", None)
        else:
            item["record_version"] = previous_record_version
        item["updated_at"] = previous_updated_at
        response = getattr(exc, "response", {}) or {}
        reasons = [{"code": str(reason.get("Code") or "Unknown")[:64],
                    "message": str(reason.get("Message") or "")[:256]}
                   for reason in response.get("CancellationReasons", [])
                   if isinstance(reason, dict)]
        log.error("MicroVM ownership transaction failed type=%s code=%s reasons=%s",
                  type(exc).__name__, _client_error_code(exc) or "none", reasons)
        raise SessionConflict("MicroVM ownership binding failed atomically") from exc
    return item


def _rotate_bound_session_epoch(item: dict) -> dict:
    """Atomically revoke old relay tokens while preserving this VM's owner binding."""
    microvm_id = str(item.get("microvm_id") or "")
    if not microvm_id:
        raise ValueError("cannot rotate a session epoch without a bound MicroVM")
    previous_session_id = str(item.get("session_id") or "")
    previous_session_version = _session_version(item)
    previous_record_version = int(item.get("record_version") or 0)
    previous_updated_at = item.get("updated_at")
    item["session_id"] = uuid.uuid4().hex
    item["session_version"] = previous_session_version + 1
    item["record_version"] = previous_record_version + 1
    item["updated_at"] = _now()
    _apply_session_indexes(item)
    owner = {
        "pk": f"MICROVM#{microvm_id}", "sk": "OWNER",
        "tenant_id": item["tenant_id"], "image_id": item["image_id"],
        "session_id": item["session_id"], "session_version": _session_version(item),
        "microvm_id": microvm_id, "created_at": _now(),
        "ttl": _now() + MICROVM_MAX_DURATION_SECONDS + 24 * 60 * 60,
    }
    for field in _SESSION_RELEASE_FIELDS:
        value = str(item.get(field) or "")
        if not value:
            raise SessionConflict(f"session epoch rotation is missing {field}")
        owner[field] = value
    try:
        ddb_client.transact_write_items(TransactItems=[
            {"Put": {
                "TableName": SESSION_TABLE_NAME,
                "Item": _ddb_map(item),
                "ConditionExpression": "record_version = :expected",
                "ExpressionAttributeValues": {
                    ":expected": _ddb_value(previous_record_version),
                },
            }},
            {"Put": {
                "TableName": SESSION_TABLE_NAME,
                "Item": _ddb_map(owner),
                "ConditionExpression": (
                    "tenant_id = :tenant AND image_id = :image AND microvm_id = :microvm "
                    "AND session_id = :session AND session_version = :version"
                ),
                "ExpressionAttributeValues": {
                    ":tenant": _ddb_value(item["tenant_id"]),
                    ":image": _ddb_value(item["image_id"]),
                    ":microvm": _ddb_value(microvm_id),
                    ":session": _ddb_value(previous_session_id),
                    ":version": _ddb_value(previous_session_version),
                },
            }},
        ])
    except Exception as exc:
        item["session_id"] = previous_session_id
        item["session_version"] = previous_session_version
        item["record_version"] = previous_record_version
        item["updated_at"] = previous_updated_at
        raise SessionConflict("session epoch and MicroVM owner rotation failed atomically") from exc
    return item


def _delete_vm_owner(item: dict, microvm_id: str) -> None:
    """Remove only the owner row that still names this exact tenant/session."""
    if not microvm_id or LOCAL_MODE:
        return
    try:
        _session_table().delete_item(
            Key={"pk": f"MICROVM#{microvm_id}", "sk": "OWNER"},
            ConditionExpression=(Attr("tenant_id").eq(item.get("tenant_id")) &
                                 Attr("session_id").eq(item.get("session_id")) &
                                 Attr("image_id").eq(item.get("image_id"))),
        )
    except ClientError as exc:
        if _client_error_code(exc) not in {"ConditionalCheckFailedException", "ResourceNotFoundException"}:
            raise


def _load_session(identity: CallerIdentity, image_id: str) -> dict:
    table = _session_table()
    key = {"pk": _session_pk(identity), "sk": _session_sk(image_id)}
    found = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if found:
        _touch_session_identity(found, identity)
        return found
    item = _new_session(identity, image_id)
    try:
        table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
        return item
    except ClientError as exc:
        if _client_error_code(exc) != "ConditionalCheckFailedException":
            raise
        found = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not found:
            raise
        _touch_session_identity(found, identity)
        return found


def _acquire_session_lease(item: dict) -> str:
    owner = uuid.uuid4().hex
    now = _now()
    _session_table().update_item(
        Key={"pk": item["pk"], "sk": item["sk"]},
        UpdateExpression=("SET lease_owner = :owner, lease_expires_at = :expires, "
                          "updated_at = :now ADD record_version :one"),
        ConditionExpression=Attr("lease_expires_at").not_exists() | Attr("lease_expires_at").lt(now),
        ExpressionAttributeValues={":owner": owner, ":expires": now + 5 * 60,
                                   ":now": now, ":one": 1},
    )
    return owner


def _release_session_lease(item: dict, owner: str) -> None:
    try:
        _session_table().update_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            UpdateExpression="SET updated_at = :now ADD record_version :one REMOVE lease_owner, lease_expires_at",
            ConditionExpression=Attr("lease_owner").eq(owner),
            ExpressionAttributeValues={":now": _now(), ":one": 1},
        )
    except ClientError:
        pass


def _vm_from_item(item: dict, state: str | None = None, endpoint: str | None = None) -> dict:
    vm = {
        "id": item.get("microvm_id") or None,
        "endpoint": endpoint or item.get("endpoint"),
        "image_id": item.get("image_id") or "doom",
        "state": state or item.get("state") or "UNKNOWN",
        "session_id": item.get("session_id"),
        "session_version": _session_version(item),
        "tenant_id": item.get("tenant_id"),
        # Internal transport authority.  Session payloads enumerate their
        # public fields explicitly and never serialize this value.
        "bridge_capability": item.get("bridge_capability") or "",
    }
    for field in _SESSION_RELEASE_FIELDS:
        vm[field] = str(item.get(field) or "")
    return vm


def _clear_vm(item: dict, state: str = "STOPPED") -> dict:
    previous_microvm_id = str(item.get("microvm_id") or "")
    previous_owner = dict(item)
    item.pop("microvm_id", None)
    item.pop("endpoint", None)
    item.pop("bridge_capability", None)
    # A failed external RunMicrovm/ownership transaction may have persisted a
    # launch idempotency token before the VM was bound. Once that session is
    # cleared, retaining the token would make the next legitimate launch look
    # like a parameter mismatch to AWS.
    item.pop("launch_client_token", None)
    item.pop("launch_bridge_capability", None)
    for field in _SESSION_RELEASE_FIELDS:
        item.pop(field, None)
    item["state"] = state
    item["frozen"] = state == "SUSPENDED"
    item["relay_warm_until"] = 0
    item["session_id"] = uuid.uuid4().hex
    item["session_version"] = _session_version(item) + 1
    # "this session" in the widget means THIS BOX: a cleared VM ends the meter, so the counters
    # zero here (the interval being discarded belongs to the box that just died). Without this the
    # readout is a lifetime total across every trash/relaunch — live-QA showed "808m running" on a
    # freshly booted box. Freeze/thaw keep accumulating — same box, billing merely paused.
    item["running_seconds"] = 0
    item["running_since"] = 0
    saved = _save_session(item)
    _delete_vm_owner(previous_owner, previous_microvm_id)
    return saved


def _local_vm(image_id: str) -> tuple[dict, dict]:
    """LOCAL MODE: a synthetic always-RUNNING VM whose 'endpoint' is the local capsule host.
    No Lambda, no DynamoDB — the capsule runs in Docker on this machine."""
    item = {"tenant_id": LOCAL_TENANT, "image_id": image_id, "microvm_id": f"local-{image_id}",
            "endpoint": LOCAL_CAPSULE_HOST, "state": "RUNNING", "frozen": False,
            "session_id": "local", "session_version": 1,
            "bridge_capability": LOCAL_BRIDGE_CAPABILITY}
    return item, _vm_from_item(item, state="RUNNING", endpoint=LOCAL_CAPSULE_HOST)


def _discover_vm(identity: CallerIdentity, image_id: str = "") -> tuple[dict, dict]:
    image_id = _resolve_image_id(image_id)
    if LOCAL_MODE:
        return _local_vm(image_id)
    """Read this caller's mapped VM without running or resuming it."""
    item = _load_session(identity, image_id)
    _assert_owns(identity, item, "discover_vm")  # defense-in-depth: fail closed on any mismatch
    vm_id = item.get("microvm_id")
    if not vm_id:
        return item, _vm_from_item(item, state=item.get("state") or "STOPPED")
    try:
        g = mvm.get_microvm(microvmIdentifier=vm_id)
        state = g.get("state", "UNKNOWN")
        endpoint = g.get("endpoint") or item.get("endpoint")
        if item.get("release_digest") and (
                str(g.get("imageArn") or "") != str(item.get("image_arn") or "") or
                str(g.get("imageVersion") or "") != str(item.get("image_version") or "")):
            log.error("microvm id=%s no longer matches its immutable session release", vm_id)
            vm = _vm_from_item(item, state="RELEASE_MISMATCH", endpoint=endpoint)
            for field in _SESSION_RELEASE_FIELDS:
                vm[field] = ""  # prevents relay-token minting for the mismatched VM
            return item, vm
        if state in ("TERMINATED", "FAILED"):
            item = _clear_vm(item, state=state)
            return item, _vm_from_item(item, state=state)
        item["state"] = state
        item["endpoint"] = endpoint
        item["frozen"] = state == "SUSPENDED"
        _save_session(item)
        return item, _vm_from_item(item, state=state, endpoint=endpoint)
    except Exception as exc:
        if _client_error_code(exc) in ("ResourceNotFoundException", "NotFoundException"):
            item = _clear_vm(item, state="STOPPED")
            return item, _vm_from_item(item, state="STOPPED")
        return item, _vm_from_item(item, state="UNKNOWN")


def _assert_owns(identity: CallerIdentity, item: dict, op: str) -> None:
    """Defense-in-depth ownership assertion. The session key is ALREADY TENANT#<caller>, so a loaded
    item structurally belongs to the caller — this re-checks the stored tenant_id anyway so any future
    key-derivation bug fails CLOSED (a cross-tenant op raises instead of acting). Cheap, and the switch
    flow (freeze/thaw/launch on the caller's VM) is exactly where a mistake would be catastrophic."""
    owner = str(item.get("tenant_id") or "")
    if owner and not hmac.compare_digest(owner, identity.tenant_id):
        log.error("ownership assertion FAILED op=%s caller=%s item_owner=%s",
                  op, identity.tenant_id[:12], owner[:12])
        raise PermissionError("session ownership check failed — refusing cross-tenant operation")


# CloudFront front door for the video relay (cross-origin URL the widget fetches). Set by CFN once
# the relay is deployed; "" until then. Defined up here so the resource meta + tools can reference it.
VIDEO_RELAY_URL = os.environ.get("PAIRPUTER_VIDEO_RELAY_URL", "")
RELAY_DRAIN_ATTEMPTS = 2
RELAY_DRAIN_TIMEOUT_SECONDS = 5
RELAY_DRAIN_MAX_RESPONSE_BYTES = 64 * 1024
# STABLE resource URI (no version segment). Codex caches the tool->resource BINDING in a way that
# survives logout/login and version bumps, so bumping the version just orphaned the binding ("Unknown
# resource: .../2.4.0"). A fixed URI keeps the binding valid forever; to ship new widget HTML, change
# the SERVER NAME (fresh identity) to force a clean tools/list + resource fetch. NOTE: the ui://<authority>/
# segment is a GLOBAL namespace shared with locally-installed Codex plugins, so the authority is
# deliberately 'pairputer-platform' — distinct from the 'pairputer' server/plugin name — to avoid the
# collision that bit us in v1 (a local 'hellbox' plugin captured ui://hellbox/*). Keep them distinct.
COMPONENT_VERSION = "2.12.0"  # 2.12: 320x200 display (DOOM filled corner of 640x400, left rest black); 2.11: local noVNC viewer
RESOURCE_URI = hosts.codex.PROFILE.resource_uri     # ui://pairputer-platform/app.html — NEVER change
MIME = hosts.codex.PROFILE.resource_mime            # text/html;profile=mcp-app — NEVER change
RECONNECT_COMMAND = hosts.DEFAULT.reconnect_command
RECONNECT_HINT = hosts.DEFAULT.reconnect_hint


def _reconnect(identity: "CallerIdentity") -> dict:
    """Per-host widget hints: reconnect UX strings + which display modes the host grants."""
    profile = hosts.profile_for_client_id(identity.client_id)
    return {"reconnectCommand": profile.reconnect_command, "reconnectHint": profile.reconnect_hint,
            "host": profile.id, "displayModes": list(profile.display_modes),
            "streamMode": profile.stream_mode}

mcp = FastMCP(host="0.0.0.0", stateless_http=True)

# Deprecated back-compat aliases (play_doom / play_image / doom_state / list_images) duplicate the
# generic tools (play_capsule / capsule_state / list_capsules) and cost model context in tools/list
# every turn for zero capability. Default OFF so they don't register; flip PAIRPUTER_DEPRECATED_ALIASES=1
# to restore them for an old integration that still names them. The functions are always DEFINED (so
# any in-process reference still resolves); only the tools/list registration is gated.
_DEPRECATED_ALIASES = os.environ.get("PAIRPUTER_DEPRECATED_ALIASES", "") not in ("", "0", "false", "False")


def _deprecated_alias_tool(*d_args, **d_kwargs):
    """Register as an MCP tool only when deprecated aliases are enabled; otherwise a no-op decorator
    that leaves the function defined but unregistered (absent from tools/list)."""
    if _DEPRECATED_ALIASES:
        return mcp.tool(*d_args, **d_kwargs)
    return lambda fn: fn


mvm = boto3.client("lambda-microvms", region_name=REGION)
ecs = boto3.client("ecs", region_name=REGION)
elbv2 = boto3.client("elbv2", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
ddb_client = boto3.client("dynamodb", region_name=REGION)
_ddb_serializer = None


def _ensure_running(identity: CallerIdentity, image_id: str = "") -> tuple[dict, dict]:
    image_id = _resolve_image_id(image_id)
    if LOCAL_MODE:
        return _local_vm(image_id)
    """Return this caller's RUNNING MicroVM for image_id, starting/resuming as needed."""
    release = _release_for(image_id)
    image_arn = _image_arn(image_id)
    image_version = str(release.get("imageVersion") or "")
    if not image_version:
        raise RuntimeError("capsule release has no pinned image version")
    item = _load_session(identity, image_id)

    for _attempt in range(5):
        item = _load_session(identity, image_id)
        _assert_owns(identity, item, "ensure_running")  # defense-in-depth: fail closed on any mismatch
        # Stale-release session with a DEAD VM self-heals into a fresh launch here (no "Trash it"
        # round-trip → no dead widget card); a live stale VM still raises the explicit error.
        item, vm_id = _heal_or_require_release_current(identity, item, image_id, release)
        if not vm_id:
            owner = ""
            try:
                owner = _acquire_session_lease(item)
                latest = _load_session(identity, image_id)
                item = latest
                if latest.get("microvm_id"):
                    vm_id = item["microvm_id"]
                    _require_session_release_current(item, image_id)
                else:
                    # Persist a stable launch intent before the external API
                    # call. A crash/retry reuses the same AWS idempotency token
                    # and per-VM ingress capability instead of launching an
                    # orphan duplicate.
                    bridge_capability = str(item.get("launch_bridge_capability") or "")
                    launch_client_token = str(item.get("launch_client_token") or "")
                    if (len(bridge_capability) < 32 or not launch_client_token or
                            not _session_release_matches(item, release)):
                        bridge_capability = secrets.token_urlsafe(32)
                        launch_client_token = (
                            f"pairputer-{image_id[:12]}-{identity.tenant_id[:16]}-{uuid.uuid4().hex[:12]}"
                        )
                        item.update({
                            "launch_bridge_capability": bridge_capability,
                            "launch_client_token": launch_client_token,
                            "state": "STARTING",
                        })
                        _apply_release_binding(item, release)
                        _save_session(item)
                    # AWS delivers this unique value to the snapshot through
                    # the /run lifecycle hook before external traffic is
                    # admitted.  It authenticates the second, capsule-local
                    # hop after Lambda's JWE has authenticated the MCP caller.
                    run_args = {
                        "imageIdentifier": image_arn,
                        "imageVersion": image_version,
                        "idlePolicy": {
                            "autoResumeEnabled": True,
                            "maxIdleDurationSeconds": _effective_idle_seconds(item),
                            "suspendedDurationSeconds": MICROVM_SUSPENDED_DURATION_SECONDS,
                        },
                        "maximumDurationInSeconds": MICROVM_MAX_DURATION_SECONDS,
                        "clientToken": launch_client_token,
                        "runHookPayload": json.dumps(
                            {"bridgeCapability": bridge_capability}, separators=(",", ":")
                        ),
                    }
                    if INGRESS_NETWORK_CONNECTORS:
                        run_args["ingressNetworkConnectors"] = INGRESS_NETWORK_CONNECTORS
                    if EGRESS_NETWORK_CONNECTORS:
                        run_args["egressNetworkConnectors"] = EGRESS_NETWORK_CONNECTORS
                    # Per-capsule least-privilege role (interaction.md permissions.iamRole). Only attach a
                    # real role; "none"/empty (DOOM) means the VM gets no AWS access.
                    _role = _capsule_run_role(image_id)
                    if _role:
                        run_args["executionRole"] = _role
                    log.info("run_microvm image=%s tenant=%s connectors_in=%d connectors_eg=%d role=%s",
                             image_id, identity.tenant_id[:12],
                             len(INGRESS_NETWORK_CONNECTORS), len(EGRESS_NETWORK_CONNECTORS), _role or "none")
                    run = mvm.run_microvm(**run_args)
                    log.info("run_microvm OK id=%s state=%s", run.get("microvmId"), run.get("state"))
                    item.update({
                        "microvm_id": run["microvmId"],
                        "endpoint": run.get("endpoint"),
                        "state": run.get("state") or "PENDING",
                        "frozen": False,
                        "relay_warm_until": 0,
                        "session_id": uuid.uuid4().hex,
                        "session_version": _session_version(item) + 1,
                        "bridge_capability": bridge_capability,
                    })
                    _apply_release_binding(item, release)
                    item.pop("lease_owner", None)
                    item.pop("lease_expires_at", None)
                    item.pop("launch_bridge_capability", None)
                    item.pop("launch_client_token", None)
                    try:
                        _bind_new_vm_owner(item)
                    except Exception:
                        # AWS launch is outside DynamoDB's transaction. If the
                        # unique ownership commit fails, terminate the unbound
                        # VM immediately rather than leaving a billable orphan.
                        try:
                            mvm.terminate_microvm(microvmIdentifier=run["microvmId"])
                        except Exception:
                            pass
                        raise
                    vm_id = item["microvm_id"]
            except ClientError as exc:
                if _client_error_code(exc) != "ConditionalCheckFailedException":
                    log.error("run_microvm failed image=%s tenant=%s: %s",
                              image_id, identity.tenant_id[:12], exc)
                    raise
                time.sleep(2)
                continue
            except Exception:
                if owner:
                    _release_session_lease(item, owner)
                raise
            finally:
                if owner:
                    _release_session_lease(item, owner)

        for _ in range(40):
            try:
                g = mvm.get_microvm(microvmIdentifier=vm_id)
            except Exception as exc:
                if _client_error_code(exc) in ("ResourceNotFoundException", "NotFoundException"):
                    log.warning("get_microvm id=%s vanished (ResourceNotFound) -> STOPPED; VM died on boot?", vm_id)
                    item = _clear_vm(item, state="STOPPED")
                    break
                raise
            st = g.get("state") or "UNKNOWN"
            if (str(g.get("imageArn") or image_arn) != str(item.get("image_arn") or "") or
                    str(g.get("imageVersion") or image_version) != str(item.get("image_version") or "")):
                log.error("microvm id=%s release identity mismatch; terminating", vm_id)
                try:
                    mvm.terminate_microvm(microvmIdentifier=vm_id)
                finally:
                    item = _clear_vm(item, state="FAILED")
                raise RuntimeError("launched MicroVM does not match the session's immutable release")
            item["endpoint"] = g.get("endpoint") or item.get("endpoint")
            item["state"] = st
            if st == "RUNNING":
                item["frozen"] = False
                item["relay_warm_until"] = 0
                _save_session(item)
                log.info("microvm id=%s RUNNING tenant=%s", vm_id, identity.tenant_id[:12])
                return item, _vm_from_item(item, state="RUNNING", endpoint=item.get("endpoint"))
            if st == "SUSPENDED":
                # Serialize resume with freeze/terminate and revoke every
                # browser-visible token *before* traffic can wake this VM.
                # A failed epoch transaction therefore leaves the VM safely
                # suspended and the caller can retry from fresh state.
                lease_owner = ""
                try:
                    lease_owner = _acquire_session_lease(item)
                    latest = _load_session(identity, image_id)
                    if str(latest.get("microvm_id") or "") != str(vm_id):
                        item = latest
                        break
                    _require_session_release_current(latest, image_id)
                    current = mvm.get_microvm(microvmIdentifier=vm_id).get("state") or "UNKNOWN"
                    if current == "SUSPENDED":
                        latest["state"] = "RESUMING"
                        latest["frozen"] = False
                        latest["relay_warm_until"] = 0
                        _rotate_bound_session_epoch(latest)
                        mvm.resume_microvm(microvmIdentifier=vm_id)
                    item = latest
                except ClientError as exc:
                    if _client_error_code(exc) != "ConditionalCheckFailedException":
                        raise
                    time.sleep(1)
                finally:
                    if lease_owner:
                        _release_session_lease(item, lease_owner)
                item = _load_session(identity, image_id)
            elif st in ("TERMINATED", "FAILED"):
                log.warning("microvm id=%s -> %s (cleared)", vm_id, st)
                item = _clear_vm(item, state=st)
                break
            else:
                _save_session(item)
            time.sleep(2)

    log.error("microvm tenant=%s image=%s did not reach RUNNING (timeout)", identity.tenant_id[:12], image_id)
    raise TimeoutError(f"microvm for tenant {identity.tenant_id[:12]} image {image_id} did not reach RUNNING")


def _vm_state(identity: CallerIdentity, image_id: str = "") -> str:
    image_id = _resolve_image_id(image_id)
    return _discover_vm(identity, image_id)[1].get("state", "UNKNOWN")


def _ensure_relay_running() -> None:
    """Wake the Fargate relay service and wait until ALB sees a healthy target."""
    if not (RELAY_CLUSTER and RELAY_SERVICE and RELAY_TARGET_GROUP_ARN):
        return
    ecs.update_service(cluster=RELAY_CLUSTER, service=RELAY_SERVICE, desiredCount=1)
    deadline = time.time() + 180
    last = "starting"
    while time.time() < deadline:
        try:
            svc = ecs.describe_services(cluster=RELAY_CLUSTER, services=[RELAY_SERVICE])["services"][0]
            running = svc.get("runningCount", 0)
            desired = svc.get("desiredCount", 0)
            targets = elbv2.describe_target_health(TargetGroupArn=RELAY_TARGET_GROUP_ARN).get(
                "TargetHealthDescriptions", []
            )
            healthy = [t for t in targets if t.get("TargetHealth", {}).get("State") == "healthy"]
            last = f"desired={desired} running={running} healthy={len(healthy)}"
            if running >= 1 and healthy:
                return
        except Exception as exc:
            last = str(exc)
        time.sleep(3)
    raise TimeoutError(f"relay service did not become healthy: {last}")


def _scale_relay(desired_count: int) -> None:
    if RELAY_CLUSTER and RELAY_SERVICE:
        ecs.update_service(cluster=RELAY_CLUSTER, service=RELAY_SERVICE, desiredCount=desired_count)


def _scale_relay_to_zero_if_idle() -> str:
    # Warm policy (PAIRPUTER_RELAY_WARM_SECONDS): -1 always-on; 0 scale to zero when idle now; N>0 keep
    # warm N seconds (the relay's own timer handles N>0 — here we act only for the 0/-1 cases). SAFETY:
    # the historical objection stands — never stop the relay from a STALE observation. So we scale down
    # ONLY when the count read SUCCEEDS and returns EXACTLY 0. Any failure or non-zero leaves it warm.
    if RELAY_WARM_SECONDS < 0:
        return "always_on"
    if RELAY_WARM_SECONDS > 0:
        return "kept_warm"  # the relay's N-second timer owns the delayed scale-down
    try:
        active = _active_relay_session_count()
    except Exception as exc:
        log.warning("relay idle-count read failed; staying warm (fail-safe): %s", type(exc).__name__)
        return "count_read_failed"
    if active != 0:
        return "sessions_active"
    try:
        _scale_relay(0)
        return "scaled_to_zero"
    except Exception as exc:
        log.warning("relay scale-to-zero failed: %s", type(exc).__name__)
        return "scale_failed"


def _active_relay_session_count() -> int:
    if not SESSION_TABLE_NAME:
        return 0
    now = _now()
    count = 0
    table = _session_table()
    for shard in range(RELAY_ACTIVE_SHARDS):
        kwargs = {
            "IndexName": "GSI2",
            "KeyConditionExpression": Key("gsi2pk").eq(f"RELAY#ACTIVE#{shard:02d}"),
            "FilterExpression": Attr("state").eq("RUNNING") | Attr("relay_warm_until").gt(now),
            "Select": "COUNT",
        }
        while True:
            out = table.query(**kwargs)
            count += int(out.get("Count", 0))
            last = out.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    return count


def _relay_token(identity: CallerIdentity, vm: dict, exp: int | None = None) -> str:
    session_id = vm.get("session_id") or uuid.uuid4().hex
    return _mint_session_token(
        tenant_id=identity.tenant_id,
        microvm_id=vm["id"],
        image_id=vm["image_id"],
        session_id=session_id,
        session_version=vm.get("session_version") or 1,
        release_digest=vm.get("release_digest") or "",
        manifest_digest=vm.get("manifest_digest") or "",
        image_arn=vm.get("image_arn") or "",
        image_version=vm.get("image_version") or "",
        frozen=vm.get("state") not in (None, "RUNNING"),
        exp=exp,
    )


def _drain_relay(identity: CallerIdentity, vm: dict, *, required: bool = True) -> bool:
    if not vm.get("id"):
        return True
    # Legacy sessions created before immutable release binding cannot mint a
    # relay token accepted by the current data plane. Cleanup must still be
    # able to terminate their MicroVM, but it must never synthesize a weaker
    # token or attempt an unauthenticated drain.
    if any(not vm.get(field) for field in _SESSION_RELEASE_FIELDS):
        if required:
            raise RuntimeError("relay drain requires an immutable release binding")
        return False
    if not VIDEO_RELAY_URL:
        if required:
            raise RuntimeError("relay drain failed closed before lifecycle transition (not_configured)")
        return False
    exp = int(time.time()) + SESSION_TOKEN_TTL_SECONDS
    token = _relay_token(identity, vm, exp=exp)
    params = [("t", token)]
    edge_auth = _cloudfront_signed_params(exp)
    query = urllib.parse.urlencode(params)
    if edge_auth:
        query += "&" + edge_auth
    url = VIDEO_RELAY_URL.rstrip("/") + "/drain?" + query
    last_failure = "transport"
    for attempt in range(RELAY_DRAIN_ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=RELAY_DRAIN_TIMEOUT_SECONDS) as resp:
                status = int(getattr(resp, "status", None) or resp.getcode() or 0)
                if not 200 <= status < 300:
                    last_failure = f"http_{status}" if 100 <= status <= 599 else "invalid_status"
                elif len(resp.read(RELAY_DRAIN_MAX_RESPONSE_BYTES + 1)) > RELAY_DRAIN_MAX_RESPONSE_BYTES:
                    last_failure = "response_too_large"
                else:
                    return True
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            last_failure = f"http_{status}" if 100 <= status <= 599 else "http_error"
        except (urllib.error.URLError, TimeoutError, OSError):
            last_failure = "transport"
        if attempt + 1 < RELAY_DRAIN_ATTEMPTS:
            time.sleep(0.1)
    if required:
        raise RuntimeError(f"relay drain failed closed before lifecycle transition ({last_failure})")
    # Terminating the VM is itself the safety stop: do not let an unavailable relay make Trash fail.
    # Never log the exception or signed URL because either can contain the bearer session token.
    log.warning("relay drain unavailable during safe termination; continuing (%s)", last_failure)
    return False


def _session_payload(identity: CallerIdentity, vm: dict, status: str = "running") -> dict:
    exp = int(time.time()) + SESSION_TOKEN_TTL_SECONDS
    if LOCAL_MODE:
        # No relay locally. The capsule already serves the same player shell as the shipped Hellbox
        # capsule on :6901; pass direct localhost WebSocket endpoints for video/audio/input.
        # Always RUNNING locally — never show the SUSPENDED/billing overlay (there's no MicroVM lifecycle).
        player_port = int(os.environ.get("PAIRPUTER_LOCAL_PLAYER_PORT",
                                         os.environ.get("PAIRPUTER_LOCAL_NOVNC_PORT", "6901")))
        video_port = int(os.environ.get("PAIRPUTER_LOCAL_VIDEO_PORT", os.environ.get("PAIRPUTER_VIDEO_PORT", "6903")))
        audio_port = int(os.environ.get("PAIRPUTER_LOCAL_AUDIO_PORT", os.environ.get("PAIRPUTER_AUDIO_PORT", "6902")))
        input_port = int(os.environ.get("PAIRPUTER_LOCAL_INPUT_PORT", os.environ.get("PAIRPUTER_INPUT_PORT", "6904")))
        player_qs = urllib.parse.urlencode({
            "video_ws": f"ws://{LOCAL_CAPSULE_HOST}:{video_port}",
            "audio_ws": f"ws://{LOCAL_CAPSULE_HOST}:{audio_port}",
            "input_ws": f"ws://{LOCAL_CAPSULE_HOST}:{input_port}",
        })
        return {"relayUrl": f"http://{LOCAL_CAPSULE_HOST}:{VIDEO_PORT}", "videoPort": VIDEO_PORT,
                "viewerUrl": f"http://{LOCAL_CAPSULE_HOST}:{player_port}/index.html?{player_qs}",
                "status": status, "state": "RUNNING",
                "imageId": vm.get("image_id") or _default_image_id(),
                "capsule": _capsule_metadata(vm.get("image_id") or _default_image_id()),
                "tenantId": identity.tenant_id, "tenantShort": identity.tenant_id[:12],
                "microvmId": vm.get("id"), "sessionId": vm.get("session_id"),
                "sessionVersion": vm.get("session_version") or 1, "token": "local", "edgeAuth": "",
                "expiresAt": exp, "expiresIn": SESSION_TOKEN_TTL_SECONDS,
                "local": True,
                **_reconnect(identity)}
    return {"relayUrl": VIDEO_RELAY_URL, "videoPort": VIDEO_PORT, "status": status,
            "state": vm.get("state"),
            "imageId": vm.get("image_id") or _default_image_id(),
            "capsule": _capsule_metadata(vm.get("image_id") or _default_image_id()),
            "tenantId": identity.tenant_id,
            "tenantShort": identity.tenant_id[:12],
            "microvmId": vm.get("id"),
            "sessionId": vm.get("session_id"),
            "sessionVersion": vm.get("session_version") or 1,
            "token": _relay_token(identity, vm, exp=exp), "edgeAuth": _cloudfront_signed_params(exp),
            "expiresAt": exp, "expiresIn": SESSION_TOKEN_TTL_SECONDS,
            **_reconnect(identity)}


def _mark_explicit_open(payload: dict) -> dict:
    """Stamp one successful opening tool call with a nonce the widget can persist.

    Codex may remount a widget and replay the original tool output. The stable nonce lets the
    widget distinguish that replay from a genuinely new play_capsule invocation: a new nonce is an
    explicit request to run even if localStorage still remembers an earlier Freeze/Trash, while a
    replayed nonce must continue honoring that persisted lifecycle intent.
    """
    payload["openNonce"] = uuid.uuid4().hex
    payload["openedAt"] = int(time.time())
    return payload


def _play(identity: CallerIdentity, image_id: str = "", memory_mib: int | None = None) -> dict:
    image_id = _tier_image_id(_resolve_image_id(image_id), memory_mib)
    _item, vm = _ensure_running(identity, image_id)
    _ensure_relay_running()
    payload = _session_payload(identity, vm)
    # Durable-workspace restore: background, marker-guarded (a resumed VM keeps its files and is
    # skipped; only a fresh boot with a non-empty tenant snapshot pulls from S3).
    payload["persistentRestore"] = _persist_restore_async(identity, image_id)
    return payload


def _trash_microvm(identity: CallerIdentity, image_id: str = "", relaunch: bool = False) -> dict:
    image_id = _resolve_image_id(image_id)
    item, vm = _discover_vm(identity, image_id)
    previous_id = vm.get("id")
    previous_state = vm.get("state")
    if previous_id:
        # Durable-workspace export before the VM is destroyed. Only a RUNNING VM can serve the bridge;
        # a SUSPENDED one already exported at freeze time. Best-effort: trash must never fail closed.
        persist_result = ({"enabled": False} if vm.get("state") != "RUNNING"
                          else _persist_export(identity, image_id))
        # The export reaches the capsule through _bridge(), whose discovery path performs its own
        # optimistic session refresh (same barrier freeze handles after beforeFreeze). Reload before
        # mutating, or the conditional save below rejects with "session changed concurrently".
        item = _load_session(identity, image_id)
        if str(item.get("microvm_id") or "") != str(previous_id):
            raise SessionConflict("MicroVM changed during the pre-trash persistence export")
        _drain_relay(identity, vm, required=False)
        item["state"] = "TERMINATING"
        item["frozen"] = True
        item["relay_warm_until"] = 0
        _save_session(item)
        try:
            mvm.terminate_microvm(microvmIdentifier=previous_id)
        except Exception as exc:
            if _client_error_code(exc) not in ("ResourceNotFoundException", "NotFoundException"):
                raise
        terminated = False
        for _ in range(60):
            try:
                st = mvm.get_microvm(microvmIdentifier=previous_id).get("state")
            except Exception as exc:
                if _client_error_code(exc) in ("ResourceNotFoundException", "NotFoundException"):
                    terminated = True
                    break
                st = "UNKNOWN"
            if st == "TERMINATED":
                terminated = True
                break
            time.sleep(1)
        if not terminated:
            raise TimeoutError(f"microvm {previous_id} did not reach TERMINATED")
        item = _clear_vm(item, state="STOPPED")
    else:
        persist_result = {"enabled": False}
        item = _clear_vm(item, state="STOPPED")

    relay_action = "pending_relaunch" if relaunch else _scale_relay_to_zero_if_idle()
    if relaunch:
        payload = _play(identity, image_id)
        payload.update({
            "action": "terminated_and_relaunched",
            "previousMicrovmId": previous_id,
            "previousState": previous_state,
            "relayAction": "kept_for_relaunch",
        })
        return payload

    return {
        "relayUrl": VIDEO_RELAY_URL,
        "videoPort": VIDEO_PORT,
        "status": "stopped",
        "state": "STOPPED",
        "action": "terminated" if previous_id else "none",
        "previousMicrovmId": previous_id,
        "previousState": previous_state,
        "imageId": image_id,
        "tenantId": identity.tenant_id,
        "tenantShort": identity.tenant_id[:12],
        "sessionId": item.get("session_id"),
        "sessionVersion": item.get("session_version") or 1,
        "microvmId": None,
        "relayAction": relay_action,
        "persist": persist_result,
        "expiresIn": SESSION_TOKEN_TTL_SECONDS,
        **_reconnect(identity),
    }


# --- MCP tools ------------------------------------------------------------------------------------
# The openai/outputTemplate binding goes on the TOOL's meta (like the v1 plugin's TOOL._meta), so
# Codex renders the inline component when this tool is called. The function just returns text.
@mcp.tool(meta={
    "openai/outputTemplate": RESOURCE_URI,
    "openai/widgetAccessible": True,
    "ui": {"resourceUri": RESOURCE_URI},  # MCP Apps standard (nested) — Claude reads this
    "ui/resourceUri": RESOURCE_URI,       # legacy flat form — the official ext-apps SDK emits both
})
def play_capsule(ctx: Context, image_id: str = "", memory_mib: int = 0) -> CallToolResult:
    """OPEN a capsule (render it inline + launch/resume its MicroVM). Use this ONLY to bring a capsule
    up; do NOT use it to play or act inside one that's already open.

    This is the single call that opens a capsule — it is idempotent (a second call resumes/attaches
    to the SAME VM, it does not start another) and it renders the interactive widget itself. Do NOT
    follow it with pairputer_session or another play_* in the same turn: the widget refreshes its own
    token, and a second launch in the same turn rotates the session epoch out from under the first
    widget, leaving a dead "returned no session" card next to the live one.

    ONCE A CAPSULE IS OPEN, gameplay/interaction prompts go to that capsule's OWN typed tools, NOT
    back to play_capsule. E.g. with Agent DOOM running, "fight demons" / "go to the exit" / "clear
    the room" → agent_doom__drive_goal; with the Workbench running, "open the browser" →
    browser_open. Calling play_capsule for those just spawns a duplicate window of a capsule that's
    already running. If unsure whether it's already open, call capsule_state first.

    ALWAYS pass image_id naming the capsule the human asked for (list_capsules shows the choices;
    forgiving matching accepts e.g. "workbench" or "doom"). With multiple capsules deployed an empty
    image_id is refused rather than guessed. Launches the VM synchronously and returns a full session
    payload (with relay token) so the widget can connect from this single tool call — Codex does not
    reliably deliver the widget's follow-up callTool, so the launch cannot depend on it.
    memory_mib optionally selects a memory tier (e.g. 16384 for the 16 GB workbench) when the capsule
    advertises one; ignored otherwise. If Codex reports pairputer auth expired: codex mcp login pairputer
    """
    cid = _tier_image_id(_resolve_image_id(image_id), memory_mib or None)
    identity = _caller_identity(ctx)
    # Two play_capsule calls in the SAME turn race on the session's optimistic-concurrency save; the
    # loser used to surface "session changed concurrently" and its widget rendered a dead card next to
    # the live one (the double-widget bug). A play is idempotent (one VM per tenant/image), so on a
    # concurrency conflict just retry: reload and return the WINNING session, so BOTH widgets get a
    # valid token for the same VM instead of one dying.
    payload = None
    for attempt in range(4):
        try:
            payload = _play(identity, cid)
            break
        except SessionConflict:
            if attempt == 3:
                raise
            time.sleep(0.4 * (attempt + 1))
    _mark_explicit_open(payload)
    payload["state"] = "RUNNING"
    return _widget_result(payload, image_id=cid, state="RUNNING")


# Back-compat alias for the discovery phrase "play doom" — the DOOM capsule is just image_id="doom".
@_deprecated_alias_tool(meta={
    "openai/outputTemplate": RESOURCE_URI,
    "openai/widgetAccessible": True,
    "ui": {"resourceUri": RESOURCE_URI},  # MCP Apps standard (nested) — Claude reads this
    "ui/resourceUri": RESOURCE_URI,       # legacy flat form — the official ext-apps SDK emits both
})
def play_doom(ctx: Context) -> CallToolResult:
    """Deprecated alias for play_capsule(image_id="doom"). Prefer play_capsule."""
    cid = _resolve_image_id("doom")
    payload = _mark_explicit_open(_play(_caller_identity(ctx), cid))
    payload["state"] = "RUNNING"
    return _widget_result(payload, image_id=cid, state="RUNNING")


@_deprecated_alias_tool(meta={
    "openai/outputTemplate": RESOURCE_URI,
    "openai/widgetAccessible": True,
    "ui": {"resourceUri": RESOURCE_URI},  # MCP Apps standard (nested) — Claude reads this
    "ui/resourceUri": RESOURCE_URI,       # legacy flat form — the official ext-apps SDK emits both
})
def play_image(ctx: Context, image_id: str = "") -> CallToolResult:
    """Deprecated alias for play_capsule. Render a configured capsule by image_id."""
    cid = _resolve_image_id(image_id)
    payload = _mark_explicit_open(_play(_caller_identity(ctx), cid))
    payload["state"] = "RUNNING"
    return _widget_result(payload, image_id=cid, state="RUNNING")


@mcp.tool(meta={"openai/widgetAccessible": True})
def pairputer_session(ctx: Context, image_id: str = "", ensure_running: bool = False) -> CallToolResult:
    """Mint a fresh relay session token for an already-rendered widget.

    image_id defaults to the sole/first deployed capsule. By default this does not run, resume, or
    otherwise touch the data plane. It exists so a Codex thread that stays open longer than the relay
    token TTL can recover through the authenticated MCP channel. A user-initiated Thaw may pass
    ensure_running=True to recover if the old VM aged out.
    """
    identity = _caller_identity(ctx)
    image_id = _resolve_recovery_image_id(identity, image_id)
    if ensure_running:
        # Same concurrency retry as play_capsule: the widget self-heal calls this during a double-open
        # race, so a SessionConflict must resolve to the winning session, not surface as an error.
        payload = None
        for attempt in range(4):
            try:
                payload = _play(identity, image_id)
                break
            except SessionConflict:
                if attempt == 3:
                    raise
                time.sleep(0.4 * (attempt + 1))
        payload["state"] = "RUNNING"
        return _widget_result(payload, image_id=image_id, state="RUNNING")
    _item, vm = _discover_vm(identity, image_id)
    out = {"relayUrl": VIDEO_RELAY_URL, "videoPort": VIDEO_PORT, "state": vm["state"],
           "imageId": image_id, "capsule": _capsule_metadata(image_id),
           "tenantId": identity.tenant_id, "tenantShort": identity.tenant_id[:12],
           "microvmId": vm.get("id"), "sessionId": vm.get("session_id"),
           "sessionVersion": vm.get("session_version") or 1, "expiresIn": SESSION_TOKEN_TTL_SECONDS,
           **_reconnect(identity)}
    if vm.get("id"):
        exp = int(time.time()) + SESSION_TOKEN_TTL_SECONDS
        out.update({"token": _relay_token(identity, vm, exp=exp),
                    "edgeAuth": _cloudfront_signed_params(exp), "expiresAt": exp})
    return _widget_result(out, image_id=image_id)


@mcp.tool(meta={"openai/widgetAccessible": True})
def freeze(ctx: Context, image_id: str = "") -> CallToolResult:
    """Suspend this caller's MicroVM for the requested capsule (defaults to the sole/first capsule)."""
    identity = _caller_identity(ctx)
    image_id = _resolve_recovery_image_id(identity, image_id)
    item, vm = _discover_vm(identity, image_id)
    if not vm.get("id"):
        relay_action = _scale_relay_to_zero_if_idle()
        return _widget_result({"state": "STOPPED", "action": "none", "relayWarmSeconds": 0,
                "tenantId": identity.tenant_id, "imageId": image_id,
                "relayAction": relay_action}, image_id=image_id)
    lease_owner = ""
    lifecycle = {}
    try:
        lease_owner = _acquire_session_lease(item)
        item = _load_session(identity, image_id)
        if str(item.get("microvm_id") or "") != str(vm["id"]):
            raise SessionConflict("MicroVM changed while acquiring the freeze lifecycle lease")
        _require_session_release_current(item, image_id)

        # Commit revocation before any suspension side effect. If this atomic
        # session+owner rotation conflicts, suspend is never called. If a later
        # network operation fails, all pre-freeze tokens are still revoked.
        item["state"] = "SUSPENDING"
        item["frozen"] = True
        item["relay_warm_until"] = 0
        _rotate_bound_session_epoch(item)
        rotated_session_id = str(item.get("session_id") or "")
        rotated_session_version = _session_version(item)
        vm = _vm_from_item(item, state="SUSPENDING", endpoint=item.get("endpoint"))
        lifecycle = _capsule_lifecycle_hook(identity, image_id, "beforeFreeze")
        # Durable-workspace export rides the same pre-suspend barrier (best-effort; see _persist_export).
        lifecycle["persist"] = _persist_export(identity, image_id)

        # The lifecycle hook reaches the capsule through _bridge(), whose
        # discovery path performs its own optimistic session refresh. Reload
        # after that barrier and prove it still names the exact epoch/owner we
        # rotated before making the external SuspendMicrovm side effect.
        item = _load_session(identity, image_id)
        if (str(item.get("microvm_id") or "") != str(vm["id"]) or
                str(item.get("session_id") or "") != rotated_session_id or
                _session_version(item) != rotated_session_version):
            raise SessionConflict("session binding changed during the beforeFreeze lifecycle barrier")
        _require_session_release_current(item, image_id)
        item["state"] = "SUSPENDING"
        item["frozen"] = True
        item["relay_warm_until"] = 0
        _save_session(item)
        vm = _vm_from_item(item, state="SUSPENDING", endpoint=item.get("endpoint"))
        _drain_relay(identity, vm)
        st = "UNKNOWN"
        for _ in range(6):
            mvm.suspend_microvm(microvmIdentifier=vm["id"])
            time.sleep(2.5)
            st = mvm.get_microvm(microvmIdentifier=vm["id"]).get("state")
            if st == "SUSPENDED":
                break
        else:
            log.warning("freeze: vm=%s never confirmed SUSPENDED (last state %s)", vm["id"], st)
            raise RuntimeError(f"freeze failed closed: microVM never confirmed SUSPENDED (last state {st})")
        item["state"] = "SUSPENDED"
        item["frozen"] = True
        item["relay_warm_until"] = 0
        _save_session(item)
    finally:
        if lease_owner:
            _release_session_lease(item, lease_owner)
    relay_action = _scale_relay_to_zero_if_idle()
    return _widget_result({"state": "SUSPENDED", "action": "suspended", "relayAction": relay_action,
            "relayWarmSeconds": RELAY_WARM_SECONDS, "tenantId": identity.tenant_id,
            "imageId": image_id, "microvmId": vm.get("id"), "sessionId": item.get("session_id"),
            "capsuleLifecycle": lifecycle},
            image_id=image_id)


@mcp.tool(meta={"openai/widgetAccessible": True})
def thaw(ctx: Context, image_id: str = "") -> CallToolResult:
    """Resume this caller's suspended MicroVM for the requested capsule (defaults to the sole/first)."""
    identity = _caller_identity(ctx)
    cid = _resolve_recovery_image_id(identity, image_id)
    payload = _play(identity, cid)
    payload["capsuleLifecycle"] = _capsule_lifecycle_hook(identity, cid, "afterThaw")
    return _widget_result(payload, image_id=cid, state="RUNNING")


@mcp.tool(meta={"openai/widgetAccessible": True})
def trash_microvm(ctx: Context, image_id: str = "", relaunch: bool = False) -> CallToolResult:
    """Terminate this caller's current MicroVM for the capsule (defaults to sole/first), optionally relaunch."""
    cid = _resolve_image_id(image_id)
    return _widget_result(_trash_microvm(_caller_identity(ctx), image_id=cid, relaunch=relaunch), image_id=cid)


@mcp.tool()
def capsule_state(ctx: Context, image_id: str = "") -> str:
    """Report the real MicroVM state (RUNNING / SUSPENDED / STOPPED) for a capsule (defaults to sole/first)."""
    return _vm_state(_caller_identity(ctx), _resolve_image_id(image_id))


# Back-compat alias.
@_deprecated_alias_tool()
def doom_state(ctx: Context, image_id: str = "") -> str:
    """Deprecated alias for capsule_state."""
    return _vm_state(_caller_identity(ctx), _resolve_image_id(image_id))


@mcp.tool(name="persistent_storage", meta={"openai/widgetAccessible": True})
def persistent_storage(ctx: Context, action: str, image_id: str = "", path: str = "",
                       content_base64: str = "") -> dict:
    """Your durable capsule storage (the workspace/persistent/ folder), reachable WITHOUT a running
    VM. Actions: list | read | write | delete. PREFERRED way to transfer a file (up to 8 MB) into
    the capsule: ONE write call with content_base64 — no chunking, no epoch/revision envelope — and
    it lands in the live VM's workspace/persistent/ immediately when the VM is running (plus the
    durable snapshot), or waits for the next launch. Files saved into workspace/persistent/ in the
    VM appear here after freeze/trash. read returns the last exported snapshot (liveVmRunning=true
    means the live VM may hold newer content)."""
    identity = _caller_identity(ctx)
    image_id = _resolve_image_id(image_id)
    if not _persist_enabled(image_id):
        raise RuntimeError("persistent storage is not enabled for this deployment/capsule")
    if action not in ("list", "read", "write", "delete"):
        raise ValueError("action must be one of: list, read, write, delete")
    s3 = boto3.client("s3", region_name=REGION)
    prefix = _persist_tenant_prefix(identity, image_id)
    live_running = False
    try:
        _item, vm = _discover_vm(identity, image_id)
        live_running = vm.get("state") == "RUNNING" and bool(vm.get("id"))
    except Exception:
        pass
    base = {"imageId": image_id, "folder": f"workspace/{PERSIST_DIR}/", "liveVmRunning": live_running}

    if action == "list":
        listed = s3.list_objects_v2(Bucket=PERSIST_BUCKET, Prefix=prefix, MaxKeys=1000)
        entries = [{"path": o["Key"][len(prefix):], "size": int(o["Size"]),
                    "modified": o["LastModified"].isoformat()}
                   for o in (listed.get("Contents") or []) if o["Key"] != prefix]
        return {**base, "action": "list", "entries": entries, "count": len(entries)}

    rel = _persist_safe_relpath(path)
    key = prefix + rel
    if action == "read":
        try:
            obj = s3.get_object(Bucket=PERSIST_BUCKET, Key=key)
        except Exception as exc:
            if _client_error_code(exc) in ("NoSuchKey", "404"):
                raise FileNotFoundError(f"no snapshot file at {PERSIST_DIR}/{rel}") from exc
            raise
        body = obj["Body"].read(PERSIST_MAX_FILE_BYTES)
        return {**base, "action": "read", "path": rel, "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(), "source": "snapshot",
                "snapshotAt": obj["LastModified"].isoformat(),
                "content_base64": base64.b64encode(body).decode()}

    if action == "write":
        if not content_base64:
            raise ValueError("write requires content_base64")
        body = base64.b64decode(content_base64, validate=True)
        if len(body) > PERSIST_MAX_FILE_BYTES:
            raise ValueError("file exceeds the per-file persistent-storage limit")
        sha = hashlib.sha256(body).hexdigest()
        # Written UNAPPLIED: until this exact content demonstrably reaches the VM, the export
        # mirror must never delete it (wall #29 — freeze used to destroy pending uploads).
        s3.put_object(Bucket=PERSIST_BUCKET, Key=key, Body=body, Metadata={"sha256": sha})
        wrote_live = False
        if live_running:
            try:
                wrote_live = _persist_bridge_upload(identity, image_id, rel, body)
            except Exception as exc:
                log.warning("persistent_storage live push failed: %s", type(exc).__name__)
            if wrote_live:
                _persist_mark_applied(s3, key, sha)
            else:
                # The widget tells the user "syncing into the running desktop…" — make that true.
                # The content-reconciling restore retries every pending object into the running VM
                # (idempotent; waits for the in-VM bridge) instead of silently giving up here.
                _persist_restore_async(identity, image_id)
        return {**base, "action": "write", "path": rel, "size": len(body),
                "sha256": sha,
                "wroteSnapshot": True, "wroteLiveVm": wrote_live}

    # delete: authoritative in the snapshot; best-effort reversible trash in a live VM.
    s3.delete_object(Bucket=PERSIST_BUCKET, Key=key)
    trashed_live = False
    if live_running:
        try:
            existing = _persist_bridge_data(identity, image_id, "/workspace/describe",
                                            {"path": f"{PERSIST_DIR}/{rel}"})
            obs = _persist_bridge_data(identity, image_id, "/observe", {"limit": 1})
            result = _persist_bridge_data(identity, image_id, "/workspace/trash", {
                "path": f"{PERSIST_DIR}/{rel}",
                "expected_sha256": str(existing.get("sha256") or ""),
                "action_id": f"a-{uuid.uuid4().hex[:10]}",
                "idempotency_key": f"k-{uuid.uuid4().hex[:10]}",
                "expected_human_epoch": int(obs.get("humanEpoch") or 0),
                "expected_world_revision": int(obs.get("worldRevision") or 0)})
            trashed_live = result.get("accepted") is not False
        except Exception as exc:
            log.warning("persistent_storage live trash failed: %s", type(exc).__name__)
    return {**base, "action": "delete", "path": rel,
            "deletedSnapshot": True, "trashedLiveVm": trashed_live}


@mcp.tool(name="network_airgap", meta={"openai/widgetAccessible": True})
def network_airgap(ctx: Context, action: str = "status", image_id: str = "") -> dict:
    """Control the capsule's internet air-gap. The box ships AIR-GAPPED by default: nothing inside the
    VM can reach the internet (pip/git/curl and the browser are all blocked), while streaming, the
    desktop, code-server, and local commands keep working. Toggle it live (no reboot):
      action="enable"  -> cut off internet (safe sandbox; installs/clones will fail)
      action="disable" -> open internet so pip/uv/git/npm work, then re-enable when done
      action="status"  -> report current posture
    Use disable before installing packages or cloning, and offer to re-enable after."""
    identity = _caller_identity(ctx)
    image_id = _resolve_image_id(image_id)
    if action not in ("enable", "disable", "status"):
        raise ValueError("action must be one of: enable, disable, status")
    if action == "status":
        obs = _persist_bridge_data(identity, image_id, "/observe", {"limit": 1})
        net = obs.get("network") or {}
        return {"imageId": image_id, "action": "status", **net}
    want = action == "enable"
    result = _persist_bridge_data(identity, image_id, "/network/airgap", {"enabled": want})
    result.setdefault("imageId", image_id)
    result["action"] = action
    return result


@mcp.tool(name="session_settings", meta={"openai/widgetAccessible": True})
def session_settings(ctx: Context, action: str = "status", image_id: str = "",
                     idle_seconds: int = 0) -> dict:
    """Your box's safety + cost preferences and usage. action='status' returns this session's usage
    (running time, ~estimated cost — a readout, NOT a bill) and the current/allowed auto-suspend range.
    action='set_idle' with idle_seconds sets how long the VM waits idle before auto-suspending (billing
    pauses when suspended). You can only pick a TIGHTER timeout than the operator's ceiling — never
    looser (that's the operator's guardrail). idle_seconds=0 restores the operator default. Takes effect
    on the next run/resume."""
    identity = _caller_identity(ctx)
    image_id = _resolve_image_id(image_id)
    item = _load_session(identity, image_id)
    if action == "set_idle":
        # clamp to [MIN, operator MAX]; 0 means "use operator default"
        pref = int(idle_seconds or 0)
        if pref:
            pref = max(MICROVM_MIN_IDLE_SECONDS, min(pref, MICROVM_MAX_IDLE_SECONDS))
        item["idle_seconds_pref"] = pref
        _save_session(item)
    elif action != "status":
        raise ValueError("action must be 'status' or 'set_idle'")
    # Reconcile against the REAL VM before displaying: the record can say RUNNING for hours after
    # AWS idle-suspended the box (billing paused), and trusting it counted the whole suspended gap
    # as running (live-QA: "808m running" on a box that ran minutes). _discover_vm probes AWS,
    # updates the record, and its save banks the (capped) open interval on a RUNNING->SUSPENDED
    # observation — so the two persisted facts are fresh by the time we read them.
    item, _vm = _discover_vm(identity, image_id)
    # Display total = banked running_seconds + the open interval only while GENUINELY running.
    # (Derived cost is a float, so it's computed here, never stored.)
    running_total = int(item.get("running_seconds") or 0)
    if str(item.get("state")) == "RUNNING" and item.get("running_since"):
        running_total += max(0, _now() - int(item["running_since"]))
    return {
        "imageId": image_id,
        "state": item.get("state") or "STOPPED",
        "runningSeconds": running_total,
        "estimatedCostUsd": round(running_total * MICROVM_COST_PER_SECOND_USD, 4),
        "costNote": "estimate for display only, not a bill",
        "idleSeconds": _effective_idle_seconds(item),
        "idleSecondsMin": MICROVM_MIN_IDLE_SECONDS,
        "idleSecondsMax": MICROVM_MAX_IDLE_SECONDS,   # operator ceiling — the user can't exceed this
    }


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Return the authenticated Cognito principal as seen by AgentCore."""
    return _caller_identity(ctx).public()


def _list_capsules_result() -> CallToolResult:
    """Structured capsule list for the widget/picker + a clean human line for the chat.
    Discovers pairputer-tagged capsule images at runtime (cartridge model) — merged with the env seed."""
    capsules = [_capsule_metadata(cid) for cid in _effective_registry()]
    if not capsules:
        text = ("No capsules are deployed on this substrate yet. Deploy a capsule "
                "(or redeploy with the reference DOOM capsule bundled) to launch something.")
        return CallToolResult(content=[TextContent(type="text", text=text)],
                              structuredContent={"capsules": []})
    lines = "\n".join(f"• {c['name']}  (play_capsule image_id=\"{c['imageId']}\")" for c in capsules)
    text = f"Available capsules ({len(capsules)}):\n{lines}"
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          structuredContent={"capsules": capsules})


@mcp.tool(meta={"openai/widgetAccessible": True})
def list_capsules() -> CallToolResult:
    """List the capsules this substrate can launch (friendly names + ids). Pick one, then play_capsule(image_id)."""
    return _list_capsules_result()


@mcp.tool(meta={"openai/widgetAccessible": True})
def capsule_metadata(ctx: Context, image_id: str = "") -> CallToolResult:
    """Return generic capsule-declared UX metadata: capabilities, suggested prompts, help text, and tool names."""
    cid = _resolve_image_id(image_id)
    meta = _capsule_metadata(cid)
    text = meta.get("humanHelpText") or f"{meta.get('name', cid)} declares {len(meta.get('tools') or [])} capsule tools."
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=meta)


# Back-compat alias.
@_deprecated_alias_tool(meta={"openai/widgetAccessible": True})
def list_images() -> CallToolResult:
    """Deprecated alias for list_capsules."""
    return _list_capsules_result()


# --- the inline component resource ----------------------------------------------------------------
def _component_html() -> str:
    # ponytail: load the proven app.html from disk so we don't duplicate the canvas/decoder logic.
    # v2: video is cross-origin (the CloudFront relay), so the widget MUST declare that domain in
    # connectDomains. CloudFront's host is only known at deploy time (env var), so we template it in
    # here at serve time. __RELAY_ORIGIN__ -> the CloudFront origin the widget is allowed to fetch.
    path = os.path.join(os.path.dirname(__file__), "app.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace("__RELAY_ORIGIN__", VIDEO_RELAY_URL or "")


# Declare the relay origin in EVERY CSP slot (connect/resource/frame) in BOTH the modern and legacy
# meta. The probe widget tests which (if any) Codex actually honors: fetch (connect), <img>
# ===== Agent interaction (interaction.md Tier 1/2; capsules/agent-doom; dream.md) =================
# The capability manifest (capsule.yaml, JSON via PAIRPUTER_CAPSULE_MANIFEST) is what turns agent tools
# ON. No manifest -> Tier 0: none of the tools below exist and the capsule is agent-inert (the safe
# default). Tools reach the capsule's agent bridge (:6905 HTTP/JSON, fronting the in-process gRPC
# DoomAgent service) through the MicroVM's authenticated :443 proxy gateway — the same IAM-gated path
# the relay uses for video/input. Agent tools ride server-side MCP (reliable), never the widget bridge.
CAPSULE_MANIFEST: dict = {}
try:
    _raw_manifest = _decode_manifest_parameter(os.environ.get("PAIRPUTER_CAPSULE_MANIFEST", "") or "{}")
    if isinstance(_raw_manifest, dict):
        CAPSULE_MANIFEST = _raw_manifest.get("capsule", _raw_manifest) or {}
except Exception as _exc:  # a bad manifest must not take down the control plane — log + stay Tier 0
    log.error("PAIRPUTER_CAPSULE_MANIFEST invalid (%s); agent tools disabled", _exc)
    CAPSULE_MANIFEST = {}

_MANIFEST_TOOLS = {t.get("name"): t for t in (CAPSULE_MANIFEST.get("tools") or []) if isinstance(t, dict)}
_INTERACTION = CAPSULE_MANIFEST.get("interaction") or {}
_TIER1_ENABLED = bool(_INTERACTION.get("tier1"))
# interaction.md: agent interaction is a manifest-declared capability. A capsule that declares no
# interaction at all is agent-inert regardless of anything else (defense-in-depth beyond tool
# registration — the token/session claim the doc calls for, enforced at the single agent chokepoint).
_AGENT_INTERACT_ALLOWED = bool(_TIER1_ENABLED or _MANIFEST_TOOLS)
# interaction.md permissions.iamRole -> the least-priv role the capsule's MicroVM runs under. "none"/""
# means no AWS access (DOOM). A real value (role ARN, or a name resolved via PAIRPUTER_CAPSULE_ROLE_ARN_MAP)
# is attached to run_microvm. The contract lives in the manifest so a capsule can't silently run with
# more privilege than it declares.
_CAPSULE_IAM_ROLE = str((CAPSULE_MANIFEST.get("permissions") or {}).get("iamRole") or "").strip()

_vm_proxy_tokens: dict[str, tuple[str, float]] = {}


def _canonical_bridge_settings(manifest: dict, image_id: str = "capsule") -> dict:
    """Return the one canonical transport identity used for release and routing checks."""
    bridge = manifest.get("bridge") or {}
    protocol = str(bridge.get("protocol") or "http-json").strip().lower().replace("_", "-")
    if protocol not in ("http-json", "http/json", "http"):
        raise ValueError(f"capsule '{image_id}' declares unsupported bridge protocol '{protocol}'")
    try:
        port = int(bridge.get("port", 6905))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"capsule '{image_id}' declares an invalid bridge port") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"capsule '{image_id}' bridge port must be between 1 and 65535")
    # All currently accepted protocol spellings have identical HTTP/JSON semantics. Collapsing aliases
    # prevents presentation-only spelling changes from invalidating a release without weakening the bind.
    return {"protocol": "http-json", "port": port}


def _bridge_settings_for(image_id: str) -> tuple[str, int]:
    """Resolve transport settings from the *target* capsule manifest.

    The env-seeded manifest used to determine one process-wide port. That happened to work while DOOM
    was the only cartridge, but routed a discovered cartridge to the wrong port. HTTP/JSON remains the
    only supported bridge protocol; rejecting unknown protocols is safer than silently speaking HTTP to
    a service with different semantics.
    """
    settings = _canonical_bridge_settings(_manifest_for(image_id), image_id)
    return settings["protocol"], settings["port"]


def _vm_proxy_token(vm_id: str) -> str:
    """Short-lived MicroVM proxy auth token (X-aws-proxy-auth), cached per VM. IAM-gated via
    lambda:CreateMicrovmAuthToken on the capsule image — same trust model as the relay's upstream."""
    cached = _vm_proxy_tokens.get(vm_id)
    if cached and time.time() < cached[1] - 60:
        return cached[0]
    t = mvm.create_microvm_auth_token(
        microvmIdentifier=vm_id, expirationInMinutes=10, allowedPorts=[{"allPorts": {}}])
    val = t["authToken"]["X-aws-proxy-auth"]
    _vm_proxy_tokens[vm_id] = (val, time.time() + 600)
    return val


def _bridge(
    identity: CallerIdentity,
    image_id: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout_s: float = 25.0,
    expected_bridge: dict | None = None,
) -> dict:
    """One call to the capsule's agent bridge over the VM's authed :443 gateway."""
    image_id = _resolve_image_id(image_id)
    # Defense-in-depth: THIS capsule's manifest must declare agent interaction (tier1 or tools). Per-image
    # and call-time-capable (_agent_interact_for), so a hot-added cartridge works and — stricter than the
    # old any-capsule global — one capsule's declaration never opens another's bridge.
    if not _agent_interact_for(image_id):
        raise PermissionError("this capsule does not permit agent interaction (no manifest declaration)")
    item, vm = _discover_vm(identity, image_id)
    _require_session_release_current(item, image_id)
    if vm.get("state") != "RUNNING" or not vm.get("id"):
        # Deliberately NOT auto-thawing: agent tools act on the session the human is watching;
        # starting or resuming a VM (and its billing) is the human's call via open/Thaw.
        raise RuntimeError(
            f"capsule '{image_id}' is {vm.get('state') or 'STOPPED'} — ask the human to open/Thaw it first")
    endpoint = vm.get("endpoint") or item.get("endpoint")
    if not endpoint:
        raise RuntimeError(f"capsule '{image_id}' has no VM endpoint")
    protocol, bridge_port = _bridge_settings_for(image_id)
    current_bridge = {"protocol": protocol, "port": bridge_port}
    if (expected_bridge is not None and
            not hmac.compare_digest(_canonical_object_digest(expected_bridge),
                                    _canonical_object_digest(current_bridge))):
        raise RuntimeError(
            "this named tool bridge binding belongs to a superseded capsule release; "
            "use capsule_invoke or reconnect the MCP server"
        )
    log.info("bridge %s %s image=%s vm=%s actor=agent local=%s", method, path, image_id, vm["id"], LOCAL_MODE)
    if LOCAL_MODE:
        # Talk straight to the local capsule's bridge port over plain HTTP — no VM gateway, no proxy token.
        # An explicit env override supports non-identity host port mappings. Otherwise use this target's
        # manifest port, matching the deployed MicroVM route.
        local_port = LOCAL_BRIDGE_PORT if "PAIRPUTER_LOCAL_BRIDGE_PORT" in os.environ else bridge_port
        conn = http.client.HTTPConnection(endpoint, local_port, timeout=timeout_s)
        bridge_capability = vm.get("bridge_capability") or ""
        if len(bridge_capability) < 32:
            raise RuntimeError("local agent bridge capability is unavailable")
        headers = {"Content-Type": "application/json",
                   "X-Pairputer-Bridge-Capability": bridge_capability}
    else:
        bridge_capability = vm.get("bridge_capability") or item.get("bridge_capability") or ""
        if len(bridge_capability) < 32:
            raise RuntimeError("agent bridge capability is unavailable; relaunch the capsule")
        conn = http.client.HTTPSConnection(endpoint, 443, timeout=timeout_s)
        headers = {"Host": endpoint, "Content-Type": "application/json",
                   "X-aws-proxy-auth": _vm_proxy_token(vm["id"]),
                   "X-aws-proxy-port": str(bridge_port),
                   "X-Pairputer-Bridge-Capability": bridge_capability}
    try:
        payload = json.dumps(body or {}) if method == "POST" else None
        if payload is not None and len(payload.encode("utf-8")) > BRIDGE_REQUEST_MAX_BYTES:
            raise ValueError("agent bridge request exceeds the bounded transport limit")
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        declared = resp.getheader("Content-Length")
        if declared and int(declared) > BRIDGE_RESPONSE_MAX_BYTES:
            raise RuntimeError("agent bridge response exceeds the bounded transport limit")
        data = resp.read(BRIDGE_RESPONSE_MAX_BYTES + 1)
        if len(data) > BRIDGE_RESPONSE_MAX_BYTES:
            raise RuntimeError("agent bridge response exceeds the bounded transport limit")
        try:
            out = json.loads(data or b"{}")
        except Exception:
            out = {"raw": data[:400].decode(errors="replace")}
        if resp.status != 200:
            raise RuntimeError(f"agent bridge {path} -> HTTP {resp.status}: {out.get('error', out)}")
        return out
    finally:
        conn.close()


# --- Persistent workspace (control-plane S3 sync; docs/mcp-tool-efficiency.md sibling design) ------
# The VM stays credential-free: AgentCore (which owns the IAM role) mirrors ONLY the capsule's
# workspace `persistent/` subtree to a per-tenant S3 prefix through the SAME bounded, hash-verified
# bridge tools an agent uses. Export runs at freeze/trash; restore runs once per fresh VM (marker
# outside the synced subtree). Tenant isolation = the S3 key is derived from the caller's
# JWT-derived tenant hash — a caller can never choose it. Empty bucket env = feature off.
PERSIST_BUCKET = os.environ.get("PAIRPUTER_TENANT_STORAGE_BUCKET", "")
PERSIST_S3_PREFIX = "tenant-storage"
PERSIST_DIR = "persistent"
# (restore is now content-reconciling — no boot marker; see _persist_restore_async)
PERSIST_MAX_FILES = 200
PERSIST_MAX_FILE_BYTES = 8 * 1024 * 1024
PERSIST_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_PERSIST_READ_CHUNK = 1024 * 1024


def _persist_enabled(image_id: str) -> bool:
    if not PERSIST_BUCKET or LOCAL_MODE:
        return False
    tools = {str(t.get("name")) for t in (_manifest_for(image_id).get("tools") or []) if isinstance(t, dict)}
    return {"workspace_list", "workspace_read", "workspace_upload"} <= tools


def _persist_tenant_prefix(identity: CallerIdentity, image_id: str) -> str:
    # tenant_id is sha256(issuer:sub) — attacker-uncontrollable; image_id is registry-resolved.
    return f"{PERSIST_S3_PREFIX}/{identity.tenant_id}/{image_id}/"


def _persist_safe_relpath(relpath: str) -> str:
    """Reject any path that could escape persistent/ on restore. Fails closed."""
    if not relpath or len(relpath) > 512 or relpath.startswith(("/", "~")) or "\x00" in relpath:
        raise ValueError("unsafe persistent path")
    parts = [p for p in relpath.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError("unsafe persistent path")
    return "/".join(parts)


def _persist_bridge_data(identity: CallerIdentity, image_id: str, path: str, body: dict) -> dict:
    out = _bridge(identity, image_id, "POST", path, body, timeout_s=20)
    # MERGE top-level fields with the parsed dataJson. workspace_* tools nest their result in
    # dataJson, but /observe returns worldRevision/humanEpoch at the TOP level — reading only
    # dataJson dropped them, so the live upload always sent expected_world_revision=0 and was
    # rejected as stale (the "file appears on next launch instead of now" bug). Nested wins on key
    # collision (it's the tool's authoritative payload).
    merged = {k: v for k, v in out.items() if k != "dataJson"}
    data = out.get("dataJson")
    parsed = json.loads(data) if isinstance(data, str) and data else (data if isinstance(data, dict) else {})
    merged.update(parsed)
    return merged


def _persist_walk(identity: CallerIdentity, image_id: str) -> list[dict]:
    """Bounded walk of workspace persistent/: [{relpath, size}]. Best-effort shape tolerance."""
    files, queue, seen_dirs = [], [PERSIST_DIR], 0
    while queue and seen_dirs < 64 and len(files) < PERSIST_MAX_FILES:
        current = queue.pop(0)
        seen_dirs += 1
        data = _persist_bridge_data(identity, image_id, "/workspace/list",
                                    {"path": current, "limit": PERSIST_MAX_FILES})
        for entry in (data.get("entries") or data.get("items") or []):
            name = str(entry.get("path") or entry.get("name") or "")
            if not name:
                continue
            full = name if "/" in name and name.startswith(PERSIST_DIR) else f"{current}/{name}"
            kind = str(entry.get("type") or entry.get("kind") or "").lower()
            is_dir = entry.get("isDir") is True or kind in ("dir", "directory")
            if is_dir:
                queue.append(full)
                continue
            size = int(entry.get("size") or 0)
            rel = full[len(PERSIST_DIR) + 1:]
            if rel and size <= PERSIST_MAX_FILE_BYTES:
                files.append({"relpath": _persist_safe_relpath(rel), "size": size})
    return files


def _persist_read_file(identity: CallerIdentity, image_id: str, relpath: str, size: int) -> bytes:
    blob = b""
    while len(blob) < max(size, 1):
        data = _persist_bridge_data(identity, image_id, "/workspace/read",
                                    {"path": f"{PERSIST_DIR}/{relpath}",
                                     "offset": len(blob), "length": _PERSIST_READ_CHUNK})
        content = data.get("content")
        if content is None:
            break
        encoding = str(data.get("encoding") or "utf-8")
        piece = base64.b64decode(content) if encoding == "base64" else str(content).encode("utf-8")
        if not piece:
            break
        blob += piece
        if data.get("truncated") is False or len(piece) < _PERSIST_READ_CHUNK:
            break
    return blob[:PERSIST_MAX_FILE_BYTES]


def _persist_mark_applied(s3, key: str, sha256_hex: str) -> None:
    """Stamp an S3 snapshot object as APPLIED: this exact content demonstrably reached the VM's disk
    (successful live push or restore) or came FROM the VM (export). ``_persist_export`` may
    mirror-delete ONLY applied content — a missing-in-VM object whose applied-sha256 equals its
    sha256 was deleted inside the VM by the user. An UNAPPLIED object is a pending widget/tool
    upload the VM never saw; S3 holds the only copy, and deleting it at freeze destroyed real user
    uploads (live-QA data loss — wall #29). Best-effort: a failed stamp just means the object
    survives exports until a later apply succeeds."""
    try:
        s3.copy_object(Bucket=PERSIST_BUCKET, Key=key,
                       CopySource={"Bucket": PERSIST_BUCKET, "Key": key},
                       Metadata={"sha256": sha256_hex, "applied-sha256": sha256_hex},
                       MetadataDirective="REPLACE")
    except Exception as exc:
        log.warning("persist mark-applied failed: %s", type(exc).__name__)


def _persist_export(identity: CallerIdentity, image_id: str) -> dict:
    """Mirror workspace persistent/ -> the tenant's S3 prefix (upload current set, delete stale keys
    the VM demonstrably had — never a pending upload it hasn't seen)."""
    if not _persist_enabled(image_id):
        return {"enabled": False}
    try:
        s3 = boto3.client("s3", region_name=REGION)
        prefix = _persist_tenant_prefix(identity, image_id)
        files = _persist_walk(identity, image_id)
        total = 0
        exported = []
        for f in files:
            if total >= PERSIST_MAX_TOTAL_BYTES:
                log.warning("persist export truncated at %d bytes for tenant=%s", total, identity.tenant_id[:12])
                break
            blob = _persist_read_file(identity, image_id, f["relpath"], f["size"])
            total += len(blob)
            sha = hashlib.sha256(blob).hexdigest()
            # Content read FROM the VM is applied by definition.
            s3.put_object(Bucket=PERSIST_BUCKET, Key=prefix + f["relpath"], Body=blob,
                          Metadata={"sha256": sha, "applied-sha256": sha})
            exported.append(f["relpath"])
        # Mirror semantics, applied-gated: a key missing from the VM is deleted ONLY when its exact
        # content was once applied to the VM (applied-sha256 == sha256) — then its absence means the
        # user deleted it in-VM. An unapplied key is a pending upload the VM never saw (e.g. the
        # widget wrote it while the live push failed, or while the VM was frozen); S3 holds the ONLY
        # copy, so it must survive the export for the reconcile-restore to apply on thaw/boot.
        # The old unconditional delete silently destroyed such uploads at freeze (wall #29).
        listed = s3.list_objects_v2(Bucket=PERSIST_BUCKET, Prefix=prefix, MaxKeys=1000)
        for obj in listed.get("Contents") or []:
            rel = obj["Key"][len(prefix):]
            if rel and rel not in exported:
                try:
                    meta = s3.head_object(Bucket=PERSIST_BUCKET, Key=obj["Key"]).get("Metadata") or {}
                except Exception:
                    continue
                if meta.get("applied-sha256") and meta.get("applied-sha256") == meta.get("sha256"):
                    s3.delete_object(Bucket=PERSIST_BUCKET, Key=obj["Key"])
        log.info("persist export tenant=%s image=%s files=%d bytes=%d",
                 identity.tenant_id[:12], image_id, len(exported), total)
        return {"enabled": True, "ok": True, "files": len(exported), "bytes": total}
    except Exception as exc:
        # Best-effort by design: a sync failure must never trap a human in a billable VM.
        log.warning("persist export failed tenant=%s image=%s: %s",
                    identity.tenant_id[:12], image_id, type(exc).__name__)
        return {"enabled": True, "ok": False, "reason": type(exc).__name__}


def _persist_bridge_upload(identity: CallerIdentity, image_id: str, rel: str, body: bytes) -> bool:
    """Write bytes into the live VM's persistent/ via chunked workspace_upload.

    Honors the capsule's guards: fresh epoch/revision per chunk (anti-drift) and expected_sha256
    when replacing an existing file (anti-clobber). A rejection is almost always transient revision
    drift (a concurrent observe/action advanced the world between our observe and upload), so retry
    the whole file a few times with a fresh describe+observe before giving up. Returns False only
    after exhausting retries — callers treat the live push as best-effort next to the S3 write.
    """
    vm_path = f"{PERSIST_DIR}/{rel}"
    total_sha = hashlib.sha256(body).hexdigest()
    for attempt in range(4):
        expected_sha = ""
        try:
            existing = _persist_bridge_data(identity, image_id, "/workspace/describe", {"path": vm_path})
            expected_sha = str(existing.get("sha256") or "")
        except Exception:
            pass
        upload_id = f"ps-{uuid.uuid4().hex[:10]}"
        off, rejected = 0, False
        while off == 0 or off < len(body):
            piece = body[off:off + _PERSIST_READ_CHUNK]
            obs = _persist_bridge_data(identity, image_id, "/observe", {"limit": 1})
            payload = {
                "path": vm_path, "upload_id": upload_id, "offset": off,
                "chunk_base64": base64.b64encode(piece).decode(),
                "chunk_sha256": hashlib.sha256(piece).hexdigest(),
                "total_size": len(body), "total_sha256": total_sha,
                "action_id": f"a-{uuid.uuid4().hex[:10]}",
                "idempotency_key": f"k-{uuid.uuid4().hex[:10]}",
                "expected_human_epoch": int(obs.get("humanEpoch") or 0),
                "expected_world_revision": int(obs.get("worldRevision") or 0),
            }
            if expected_sha:
                payload["expected_sha256"] = expected_sha
            try:
                result = _persist_bridge_data(identity, image_id, "/workspace/upload", payload)
            except Exception:
                rejected = True
                break
            if result.get("accepted") is False:
                rejected = True
                break
            off += max(len(piece), 1)
        if not rejected:
            return True
        time.sleep(0.4)
    return False


def _persist_restore_async(identity: CallerIdentity, image_id: str) -> dict:
    """Reconcile the tenant's S3 snapshot into the VM's persistent/ (background).

    CONTENT-reconciling, not marker-skip: for each S3 object, write it into the VM only if the VM's
    copy is missing or has a different sha256. This is correct for a FRESH boot (everything missing →
    full restore) AND for THAW after a file was written to durable storage while the VM was frozen
    (that file is in S3 but not on the resumed VM's disk → it gets applied), and it's idempotent on a
    resumed VM whose files already match (nothing written). Runs on both _play and thaw.
    """
    if not _persist_enabled(image_id):
        return {"enabled": False}

    def run():
        try:
            s3 = boto3.client("s3", region_name=REGION)
            prefix = _persist_tenant_prefix(identity, image_id)
            listed = s3.list_objects_v2(Bucket=PERSIST_BUCKET, Prefix=prefix, MaxKeys=1000)
            objects = [o for o in (listed.get("Contents") or []) if o["Key"] != prefix]
            if not objects:
                return
            # Wait for the in-VM bridge + services to answer before reconciling.
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    _persist_bridge_data(identity, image_id, "/observe", {"limit": 1})
                    break
                except Exception:
                    time.sleep(5)
            restored = 0
            for obj in objects[:PERSIST_MAX_FILES]:
                rel = _persist_safe_relpath(obj["Key"][len(prefix):])
                # Only write files the VM is missing or has a DIFFERENT sha — idempotent on a resumed
                # VM whose files already match, and applies any file that entered S3 while frozen.
                try:
                    have = _persist_bridge_data(identity, image_id, "/workspace/describe",
                                                {"path": f"{PERSIST_DIR}/{rel}"})
                except Exception:
                    have = {}
                body = s3.get_object(Bucket=PERSIST_BUCKET, Key=obj["Key"])["Body"].read(PERSIST_MAX_FILE_BYTES)
                sha = hashlib.sha256(body).hexdigest()
                if str(have.get("sha256") or "") == sha:
                    # VM already holds this exact content — it IS applied; stamp it so a later
                    # in-VM delete can propagate through the export mirror.
                    _persist_mark_applied(s3, obj["Key"], sha)
                    continue
                if _persist_bridge_upload(identity, image_id, rel, body):
                    restored += 1
                    _persist_mark_applied(s3, obj["Key"], sha)
            log.info("persist reconcile tenant=%s image=%s applied=%d of=%d",
                     identity.tenant_id[:12], image_id, restored, len(objects))
        except Exception as exc:
            log.warning("persist restore failed tenant=%s image=%s: %s",
                        identity.tenant_id[:12], image_id, type(exc).__name__)

    threading.Thread(target=run, name="persist-restore", daemon=True).start()
    return {"enabled": True, "started": True}


def _capsule_lifecycle_hook(
    identity: CallerIdentity, image_id: str, hook_name: str,
) -> dict:
    """Invoke an optional manifest-declared lifecycle barrier.

    Hooks are generic capsule paths, not workload branches. A hook failure is
    surfaced but cannot trap a human in a running/billable VM: Freeze continues
    and the capsule must reconcile an unclean barrier after thaw.
    """

    manifest = _manifest_for(image_id)
    path = str((manifest.get("lifecycle") or {}).get(hook_name) or "").strip()
    if not path:
        return {"declared": False}
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-")
    if not path.startswith("/") or len(path) > 160 or ".." in path or any(ch not in allowed for ch in path):
        log.warning("capsule %s declares invalid lifecycle hook %s", image_id, hook_name)
        return {"declared": True, "ok": False, "reason": "invalid_manifest_path"}
    try:
        result = _bridge(identity, image_id, "POST", path, {}, timeout_s=10)
        return {"declared": True, "ok": True, "result": result}
    except Exception as exc:
        log.warning("capsule lifecycle %s failed for %s: %s", hook_name, image_id, type(exc).__name__)
        return {"declared": True, "ok": False, "reason": type(exc).__name__}


# The capsule's input arbiter (whose-turn + action feed) is on this port. Agent actions that reach the
# game via the gRPC/API path (not input_ws) POST here so the human still SEES them (theatre of work) and
# the whose-turn indicator lights for BOTH agent paths — the arbiter is the single source of truth.
COPLAY_STATE_PORT = int(os.environ.get("PAIRPUTER_COPLAY_STATE_PORT", "6906"))


def _note_agent_action(identity: CallerIdentity, image_id: str, label: str) -> None:
    """Best-effort: register an agent action with the capsule arbiter (theatre of work). Never blocks or
    fails the tool — coordination visibility must not break the action itself."""
    if LOCAL_MODE:
        endpoint, port, headers = LOCAL_CAPSULE_HOST, COPLAY_STATE_PORT, {"Content-Type": "application/json"}
        conn = http.client.HTTPConnection(endpoint, port, timeout=5)
    else:
        try:
            _item, vm = _discover_vm(identity, image_id)
            endpoint = vm.get("endpoint")
            if not endpoint or vm.get("state") != "RUNNING":
                return
            conn = http.client.HTTPSConnection(endpoint, 443, timeout=5)
            headers = {"Host": endpoint, "Content-Type": "application/json",
                       "X-aws-proxy-auth": _vm_proxy_token(vm["id"]),
                       "X-aws-proxy-port": str(COPLAY_STATE_PORT)}
        except Exception as exc:
            log.info("note_agent_action skipped: %s", exc)
            return
    try:
        conn.request("POST", "/note", body=json.dumps({"label": label}), headers=headers)
        conn.getresponse().read()
    except Exception as exc:
        log.info("note_agent_action failed (non-fatal): %s", exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _manifest_desc(spec: dict, fallback: str) -> str:
    d = spec.get("description") or fallback
    if spec.get("requiresApproval"):
        d += (" REQUIRES HUMAN APPROVAL: first attempt returns an exact approval_id; the host must "
              "confirm capsule_approve at the point of risk, then retry with its single-use approval_token.")
    return d


# safety.sensitivePatterns (interaction.md): substrings/regexes an agent action's args must not contain
# without human approval — the primary defense against injection-driven real-world actions (e.g. a
# cloudshell capsule with an IAM role: "aws iam", "terraform apply"). Capsule-declared, server-enforced.
# Patterns are per-capsule (each capsule's own manifest.safety), carried on the tool spec.
import re as _re


_LOCAL_APPROVAL_SECRET = os.urandom(32)
_LOCAL_APPROVALS: dict[str, dict] = {}
_LOCAL_APPROVAL_LOCK = threading.RLock()
_APPROVAL_TTL_SECONDS = 5 * 60


def _matched_sensitive_pattern(patterns: list, args: dict) -> str:
    blob = json.dumps(args, default=str).lower()
    for pat in patterns:
        try:
            hit = _re.search(pat.lower(), blob) is not None
        except _re.error:
            hit = pat.lower() in blob  # treat a bad regex as a literal substring
        if hit:
            return str(pat)
    return ""


def _approval_action_digest(identity: CallerIdentity, image_id: str, tool_name: str, args: dict) -> str:
    canonical = json.dumps({
        "tenant_id": identity.tenant_id, "image_id": image_id,
        "tool": tool_name, "args": args,
    }, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


_APPROVAL_SENSITIVE_KEY = _re.compile(
    r"(?:pass|secret|token|authorization|credential|api[_-]?key|cookie|session)", _re.I
)


def _approval_safe_preview(value: Any, key: str = "", depth: int = 0) -> Any:
    """Human-inspectable canonical action with secrets masked and large blobs hashed."""
    if depth > 12:
        return "[DEPTH_LIMIT]"
    if _APPROVAL_SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _approval_safe_preview(v, str(k), depth + 1)
                for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_approval_safe_preview(item, key, depth + 1) for item in value[:256]]
    if isinstance(value, str):
        clean = _re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/]+=*", r"\1[REDACTED]", value)
        if len(clean) > 8192:
            return {"text_prefix": clean[:2048], "sha256": hashlib.sha256(clean.encode()).hexdigest(),
                    "characters": len(clean), "truncated": True}
        return clean
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1024]


def _approval_preview_digest(value: Any) -> str:
    """Digest a displayed approval preview across JSON and DynamoDB number types.

    DynamoDB deserializes every number as ``Decimal``, including values nested
    inside the preview.  The human-confirmed MCP value is ordinary JSON and is
    therefore composed of ``int``/``float`` values.  Encode the complete typed
    tree and normalize only JSON-number representations so those two lossless
    views compare identically without stringifying arbitrary objects or
    weakening structural exactness.
    """

    def number_text(item: int | float | Decimal) -> str:
        try:
            number = item if isinstance(item, Decimal) else Decimal(str(item))
        except Exception as exc:
            raise ValueError("approval preview contains an invalid number") from exc
        if not number.is_finite():
            raise ValueError("approval preview contains a non-finite number")
        if not number:
            return "0"
        return format(number.normalize(), "f")

    def encode(item: Any) -> list:
        if item is None:
            return ["null"]
        if isinstance(item, bool):
            return ["boolean", item]
        if isinstance(item, (int, float, Decimal)):
            return ["number", number_text(item)]
        if isinstance(item, str):
            return ["string", item]
        if isinstance(item, (list, tuple)):
            return ["array", [encode(child) for child in item]]
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("approval preview object keys must be strings")
            return ["object", [[key, encode(item[key])] for key in sorted(item)]]
        raise ValueError("approval preview contains a non-JSON value")

    raw = json.dumps(encode(value), sort_keys=False, separators=(",", ":"), ensure_ascii=False)
    return _sha256_text(raw)


def _approval_sign(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    secret = _get_session_secret() or _LOCAL_APPROVAL_SECRET
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def _approval_parse(token: str) -> dict:
    try:
        encoded, signature = token.split(".", 1)
        secret = _get_session_secret() or _LOCAL_APPROVAL_SECRET
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if len(expected) != len(supplied) or not hmac.compare_digest(expected, supplied):
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(value, dict) or int(value.get("exp", 0)) <= _now():
            raise ValueError
        return value
    except Exception as exc:
        raise RuntimeError("approval token is invalid or expired") from exc


def _request_exact_approval(identity: CallerIdentity, image_id: str, tool_name: str,
                            action_digest: str, reason: str, preview: dict) -> str:
    approval_id = "approval_" + uuid.uuid4().hex
    now = _now()
    item = {
        "pk": _session_pk(identity), "sk": f"APPROVAL#{approval_id}",
        "approval_id": approval_id, "tenant_id": identity.tenant_id,
        "image_id": image_id, "tool_name": tool_name, "action_digest": action_digest,
        "preview": preview,
        "reason": reason[:500], "status": "REQUESTED", "created_at": now,
        "expires_at": now + _APPROVAL_TTL_SECONDS, "ttl": now + _APPROVAL_TTL_SECONDS,
    }
    if LOCAL_MODE:
        with _LOCAL_APPROVAL_LOCK:
            _LOCAL_APPROVALS[approval_id] = item
    else:
        _session_table().put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
    return approval_id


def _load_exact_approval(identity: CallerIdentity, approval_id: str) -> dict:
    if LOCAL_MODE:
        with _LOCAL_APPROVAL_LOCK:
            item = dict(_LOCAL_APPROVALS.get(approval_id) or {})
    else:
        item = (_session_table().get_item(
            Key={"pk": _session_pk(identity), "sk": f"APPROVAL#{approval_id}"}, ConsistentRead=True,
        ).get("Item") or {})
    if (not item or item.get("tenant_id") != identity.tenant_id or
            int(item.get("expires_at") or 0) <= _now()):
        raise RuntimeError("approval request is missing or expired")
    return item


def _consume_exact_approval(identity: CallerIdentity, approval_token: str, action_digest: str) -> None:
    # Current grants use the random server-stored approval ID itself as a copy-stable reference.
    # Long signed bearers were routinely corrupted by LLM hosts while being copied between calls.
    # The reference grants no authority before the row is atomically changed to GRANTED and remains
    # tenant-keyed, exact-action-bound, expiring, and single-use. Accept legacy signed tokens during
    # migration so an in-flight approval is not stranded by a warm runtime replacement.
    if re.fullmatch(r"approval_[0-9a-f]{32}", str(approval_token or "")):
        approval_id = approval_token
    else:
        payload = _approval_parse(approval_token)
        if payload.get("tenant_id") != identity.tenant_id or payload.get("action_digest") != action_digest:
            raise RuntimeError("approval token does not match this exact action")
        approval_id = str(payload.get("approval_id") or "")
    token_digest = hashlib.sha256(approval_token.encode()).hexdigest()
    if LOCAL_MODE:
        with _LOCAL_APPROVAL_LOCK:
            item = _LOCAL_APPROVALS.get(approval_id)
            if (not item or item.get("status") != "GRANTED" or
                    item.get("token_digest") != token_digest or
                    item.get("tenant_id") != identity.tenant_id or
                    item.get("action_digest") != action_digest or
                    int(item.get("expires_at") or 0) <= _now()):
                raise RuntimeError("approval token is stale or already consumed")
            item["status"] = "CONSUMED"
        return
    try:
        _session_table().update_item(
            Key={"pk": _session_pk(identity), "sk": f"APPROVAL#{approval_id}"},
            UpdateExpression="SET #status = :consumed, consumed_at = :now",
            ConditionExpression=(Attr("status").eq("GRANTED") &
                                 Attr("token_digest").eq(token_digest) &
                                 Attr("action_digest").eq(action_digest) &
                                 Attr("expires_at").gt(_now())),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":consumed": "CONSUMED", ":now": _now()},
        )
    except ClientError as exc:
        if _client_error_code(exc) == "ConditionalCheckFailedException":
            raise RuntimeError("approval token is stale or already consumed") from exc
        raise


def _require_approval(tool_name: str, needs_approval: bool, patterns: list,
                      identity: CallerIdentity, image_id: str, approval_token: str,
                      args: dict | None = None) -> None:
    matched = _matched_sensitive_pattern(patterns, args or {})
    if not needs_approval and not matched:
        return
    digest = _approval_action_digest(identity, image_id, tool_name, args or {})
    if approval_token:
        _consume_exact_approval(identity, approval_token, digest)
        return
    reason = "manifest requires approval" if needs_approval else f"matched sensitive pattern {matched!r}"
    preview = {
        "tool": tool_name, "image_id": image_id, "reason": reason,
        "action_digest": digest, "args": _approval_safe_preview(args or {}),
    }
    approval_id = _request_exact_approval(identity, image_id, tool_name, digest, reason, preview)
    raise RuntimeError(json.dumps({
        "error": "approval_required", "approval_id": approval_id,
        "tool": tool_name, "image_id": image_id, "action_digest": digest,
        "expires_in": _APPROVAL_TTL_SECONDS, "reason": reason, "preview": preview,
        "next": "Copy action_digest and preview into host-confirmed capsule_approve, then retry this exact call with approval_token.",
    }, separators=(",", ":")))


def _extract_capsule_image(out: dict) -> tuple[str, str] | None:
    """If a capsule tool result carries an image (base64 PNG etc.), return (base64, mimeType).
    Generic: a screenshot/crop tool returns imageBase64 in its data so a host's computer-use loop can
    SEE the frame — the file path alone is useless remotely. The image lives in `dataJson` (a JSON
    string the bridge wraps around the action's data) or directly in `data`/top-level."""
    if not isinstance(out, dict):
        return None
    candidates = [out]
    data = out.get("data")
    if isinstance(data, dict):
        candidates.append(data)
    raw = out.get("dataJson")
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                candidates.append(parsed)
        except (ValueError, TypeError):
            pass
    for c in candidates:
        b64 = c.get("imageBase64") or c.get("image_base64")
        if isinstance(b64, str) and len(b64) > 32:
            mime = str(c.get("mimeType") or c.get("mime_type") or "image/png")
            return b64, mime
    return None


# VM-internal fields a REMOTE host must not chase. `path` especially: a host that sees a file path
# in the result text tries to open it (it can't — the file lives in the VM), instead of looking at the
# inline image it was already handed. Redact them from the text/structured view when an image is present.
_IMAGE_INTERNAL_FIELDS = ("path", "sha256", "pixelSha256", "pixel_sha256", "targetProof", "target_proof")


def _strip_image_b64(value):
    """Recursively drop imageBase64 (delivered as an ImageContent block instead) AND VM-internal
    fields like `path` from the JSON text/structured view, so the host looks at the image rather than
    trying to open a file it cannot reach."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("imageBase64", "image_base64"):
                out[k] = f"<{len(v)} b64 chars, delivered as image content>" if isinstance(v, str) else v
            elif k in _IMAGE_INTERNAL_FIELDS:
                continue  # drop VM-internal path/hash so a remote host doesn't chase it
            elif k.endswith("Json") and isinstance(v, str) and v[:1] in ("{", "["):
                # Nested JSON-string fields (dataJson/evidenceJson/...) can also carry imageBase64
                # or a VM-internal path; recurse into the parsed value and re-serialize.
                try:
                    out[k] = json.dumps(_strip_image_b64(json.loads(v)), separators=(",", ":"))
                except (ValueError, TypeError):
                    out[k] = v
            else:
                out[k] = _strip_image_b64(v)
        return out
    if isinstance(value, list):
        return [_strip_image_b64(v) for v in value]
    return value


def _compact_agent_result(out: dict) -> CallToolResult:
    # If the tool returned an image (e.g. screenshot), surface it as an inline ImageContent block so
    # a frontier host's computer-use loop can actually see the desktop — not just a file path it can't
    # reach. Generic + capsule-agnostic: keyed only on a well-known imageBase64 field.
    image = _extract_capsule_image(out)
    view = _strip_image_b64(out) if image is not None else out
    text = json.dumps(view, separators=(",", ":"), ensure_ascii=False, default=str)
    content = [TextContent(type="text", text=text)]
    if image is not None:
        b64, mime = image
        content.insert(0, ImageContent(type="image", data=b64, mimeType=mime))
    return CallToolResult(content=content, structuredContent=view)


# --- Tier 2: capsule-advertised typed tools — GENERIC, manifest-driven. NO capsule-specific code lives
# here. For each tool a capsule's manifest declares, register one dispatcher that forwards {args} to that
# capsule's bridge at the tool's `path`; the capsule owns the tool->API mapping. DOOM, cloudshell, browser,
# anything — the platform server never changes. Tool names are capsule-namespaced (from the manifest) so
# N different capsules coexist on one MCP server without collision, and each tool is BOUND to its capsule's
# image_id (bind_image) so the agent needn't pass it — the tool always acts on the capsule that declared it.
def _capsule_ns(capsule_id: str) -> str:
    """Sanitize a capsule id into a tool-name-safe namespace prefix (dashes -> underscores)."""
    return _re.sub(r"[^a-zA-Z0-9]+", "_", capsule_id or "capsule").strip("_") or "capsule"


def _namespaced_tool_name(capsule_id: str, verb: str) -> str:
    """<capsule-ns>__<verb> — the PLATFORM owns tool naming so N capsules never collide and no capsule
    can pick a colliding/off-brand name. A manifest declares a bare verb ('observe'); the server prefixes
    it with the capsule id. Idempotent: an already-namespaced name is returned unchanged."""
    ns = _capsule_ns(capsule_id)
    verb = (verb or "tool").lstrip("/")
    return verb if verb.startswith(ns + "__") else f"{ns}__{verb}"


_MANIFEST_TOOL_METADATA_FIELDS = (
    "effects", "riskClass", "approvalPolicy", "interruptibility", "idempotency",
    "presentationModes", "timeoutClass", "capabilityScopes",
)


def _manifest_tool_meta(spec: dict) -> dict:
    """MCP _meta carried without teaching the substrate any workload vocabulary."""
    declared = {key: spec[key] for key in _MANIFEST_TOOL_METADATA_FIELDS if key in spec}
    if isinstance(spec.get("inputSchema"), dict):
        declared["inputSchema"] = spec["inputSchema"]
    if isinstance(spec.get("outputSchema"), dict):
        declared["outputSchema"] = spec["outputSchema"]
    return {"pairputer/tool": declared} if declared else {}


def _manifest_tool_safety_contract(spec: dict) -> dict:
    """Canonical fields that must remain identical before a warm named tool follows a new release."""

    verb = str(spec.get("name") or "")
    contract = {
        "name": verb,
        "path": spec.get("path") or ("/" + verb.lstrip("/")),
        "requiresApproval": bool(spec.get("requiresApproval")),
        "timeoutSeconds": float(spec.get("timeoutSeconds") or spec.get("timeout_s") or 25),
        "inputSchema": spec.get("inputSchema"),
        "outputSchema": spec.get("outputSchema"),
    }
    contract.update({key: spec.get(key) for key in _MANIFEST_TOOL_METADATA_FIELDS})
    return contract


def _release_has_compatible_tool(
    release: dict,
    bound_spec: dict,
    bound_patterns: list,
    bound_bridge: dict,
) -> bool:
    manifest = release.get("manifest") if isinstance(release, dict) else None
    tools = manifest.get("tools") if isinstance(manifest, dict) else None
    if not isinstance(tools, list):
        return False
    verb = str(bound_spec.get("name") or "")
    matches = [item for item in tools if isinstance(item, dict) and item.get("name") == verb]
    if len(matches) != 1:
        return False
    current_patterns = ((manifest.get("safety") or {}).get("sensitivePatterns")
                        if isinstance(manifest, dict) else None)
    if not isinstance(current_patterns, list) or any(not isinstance(item, str) for item in current_patterns):
        current_patterns = []
    try:
        current_bridge = _canonical_bridge_settings(manifest)
    except (TypeError, ValueError):
        return False
    tool_compatible = hmac.compare_digest(
        _canonical_object_digest(_manifest_tool_safety_contract(bound_spec)),
        _canonical_object_digest(_manifest_tool_safety_contract(matches[0])),
    )
    policy_compatible = hmac.compare_digest(
        _canonical_object_digest({"sensitivePatterns": [str(item) for item in bound_patterns]}),
        _canonical_object_digest({"sensitivePatterns": current_patterns}),
    )
    bridge_compatible = hmac.compare_digest(
        _canonical_object_digest(bound_bridge),
        _canonical_object_digest(current_bridge),
    )
    return tool_compatible and policy_compatible and bridge_compatible


def _schema_annotation(schema: dict) -> Any:
    """Best-effort Python annotation for FastMCP's runtime validator; MCP keeps the exact JSON schema."""
    kind = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(kind, list):
        kind = next((x for x in kind if x != "null"), None)
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }.get(kind, Any)


def _typed_tool_signature(input_schema: dict | None) -> inspect.Signature | None:
    """Build a direct-field signature while retaining the legacy ``args`` object.

    FastMCP derives call validation from a Python signature. Setting a safe synthetic signature lets a
    manifest's object properties be accepted as normal MCP arguments; arbitrary/legacy schemas fall back
    to ``args: dict`` rather than risking a registration failure.
    """
    if not isinstance(input_schema, dict) or input_schema.get("type", "object") != "object":
        return None
    props = input_schema.get("properties") or {}
    if not isinstance(props, dict):
        return None
    reserved = {"ctx", "args", "approval_token"}
    if any(not isinstance(key, str) or not key.isidentifier() or key in reserved for key in props):
        return None
    parameters = [
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context),
    ]
    for key, field_schema in props.items():
        parameters.append(inspect.Parameter(
            key,
            inspect.Parameter.KEYWORD_ONLY,
            # FastMCP validates this synthetic signature before calling us. Keep direct fields optional
            # here so the legacy {args:{...}} form remains callable; the public JSON schema below carries
            # the real required set for current hosts and the capsule remains the final validator.
            default=None,
            annotation=_schema_annotation(field_schema),
        ))
    parameters.extend([
        inspect.Parameter("args", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=dict | None),
        inspect.Parameter("approval_token", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str),
    ])
    return inspect.Signature(parameters=parameters, return_annotation=CallToolResult)


def _advertised_input_schema(input_schema: dict, patterns: list) -> dict:
    """Exact capsule schema plus the backward-compatible args/approval transport fields."""
    # JSON round-trip gives us a plain deep copy and rejects non-JSON manifest values early.
    schema = json.loads(json.dumps(input_schema))
    wrapped_schema = json.loads(json.dumps(input_schema))
    if schema.get("type", "object") != "object" or not isinstance(schema.get("properties", {}), dict):
        return schema
    schema.setdefault("type", "object")
    properties = schema.setdefault("properties", {})
    declared_required = list(schema.pop("required", []) or [])
    # Older hosts send {args:{...}}. Keep that transport shape, but advertise the exact same inner
    # authority schema as direct fields. An open-ended wrapper encouraged models to invent alternate
    # scope fields and delayed the inevitable fail-closed rejection until after human approval.
    properties.setdefault("args", {
        "anyOf": [wrapped_schema, {"type": "null"}],
        "description": "Backward-compatible wrapper; its object must match this capsule tool schema exactly.",
    })
    properties.setdefault("approval_token", {
        "type": "string",
        "default": "",
        "maxLength": 4096,
        "description": "Single-use exact-action token returned by host-confirmed capsule_approve.",
    })
    if declared_required:
        compatibility_choice = [{"required": declared_required}, {"required": ["args"]}]
        if isinstance(schema.get("anyOf"), list):
            schema["allOf"] = list(schema.get("allOf") or []) + [{"anyOf": schema.pop("anyOf")},
                                                                  {"anyOf": compatibility_choice}]
        else:
            schema["anyOf"] = compatibility_choice
    return schema


def _apply_manifest_tool_schemas(name: str, spec: dict, patterns: list) -> None:
    """Publish manifest JSON schemas on current FastMCP, with a metadata fallback on older releases."""
    manager = getattr(mcp, "_tool_manager", None)
    get_tool = getattr(manager, "get_tool", None)
    if not callable(get_tool):
        return
    info = get_tool(name)
    if info is None:
        return
    input_schema = spec.get("inputSchema")
    if isinstance(input_schema, dict):
        info.parameters = _advertised_input_schema(input_schema, patterns)
    output_schema = spec.get("outputSchema")
    if isinstance(output_schema, dict):
        # Tool.output_schema is a cached property derived from FastMCP's return annotation. These generic
        # dispatchers deliberately return CallToolResult, so there is no capsule-specific Pydantic return
        # class to infer. Populate the public cached value without altering result conversion/compatibility.
        info.__dict__["output_schema"] = json.loads(json.dumps(output_schema))


def _register_tier2_tool(spec: dict, bind_image: str, patterns: list, namespace: str,
                         bind_release_digest: str = "", bind_bridge: dict | None = None):
    # namespace = the capsule's friendly id (for the tool name); bind_image = the REGISTRY KEY that resolves
    # to an ARN (they differ for the bundled capsule: id 'agent-doom' vs registry key 'doom'). The tool name
    # reads from the friendly id; the bridge routes by the registry key.
    verb = spec["name"]                       # manifest declares a bare verb (observe/act/reset_episode)
    name = _namespaced_tool_name(namespace, verb)  # platform-owned registered name: <capsule>__<verb>
    path = spec.get("path") or ("/" + verb.lstrip("/"))  # bridge path defaults to the bare verb
    label = spec.get("label") or verb
    needs_approval = bool(spec.get("requiresApproval"))
    timeout_s = float(spec.get("timeoutSeconds") or spec.get("timeout_s") or 25)

    # All routing values are captured by closure. In particular, no public image_id is accepted: a named
    # tool is a capability of the capsule that declared it and cannot be cross-bound to another cartridge.
    def _tool(ctx: Context, args: dict | None = None, approval_token: str = "",
              **manifest_args) -> CallToolResult:
        if "image_id" in manifest_args:
            raise TypeError("named capsule tools are bound to their declaring capsule; use capsule_invoke to choose one")
        a = dict(args or {})
        # FastMCP supplies None for omitted synthetic parameters. Do not overwrite legacy args with those
        # placeholders; false/zero/empty-string remain meaningful and are retained.
        a.update({key: value for key, value in manifest_args.items() if value is not None})
        target = bind_image
        identity = _caller_identity(ctx)
        if not LOCAL_MODE:
            current_release = _release_for(target) or {}
            current_digest = str(current_release.get("releaseDigest") or "")
            if not bind_release_digest or not hmac.compare_digest(current_digest, bind_release_digest):
                if (not bind_release_digest or bind_bridge is None or
                        not _release_has_compatible_tool(current_release, spec, patterns, bind_bridge)):
                    raise RuntimeError(
                        "this named tool safety contract belongs to a superseded capsule release; "
                        "use capsule_invoke or reconnect the MCP server"
                    )
        _require_approval(name, needs_approval, patterns, identity, target, approval_token, a)
        # Theatre of work: register the action so the human SEES it and the indicator lights for the
        # gRPC/API path too (which never flows through input_ws). Also enforces human-always-wins.
        _note_agent_action(identity, target, label)
        return _compact_agent_result(_bridge(
            identity, target, "POST", path, a, timeout_s=timeout_s,
            expected_bridge=bind_bridge,
        ))

    typed_signature = _typed_tool_signature(spec.get("inputSchema"))
    # Always hide **manifest_args from FastMCP. Without a manifest schema, preserve the historical
    # args/approval_token surface exactly; current FastMCP otherwise models **kwargs as one required
    # string parameter.
    _tool.__signature__ = typed_signature or inspect.Signature(parameters=[
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context),
        inspect.Parameter("args", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=dict | None),
        inspect.Parameter("approval_token", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str),
    ], return_annotation=CallToolResult)
    decorator = mcp.tool(
        name=name,
        description=_manifest_desc(spec, spec.get("description") or verb),
        meta=_manifest_tool_meta(spec) or None,
    )
    decorator(_tool)
    _apply_manifest_tool_schemas(name, spec, patterns)


# Collect every capsule's manifest: the env seed (the bundled/default capsule) + each tag-discovered
# capsule's manifest from its SSM param. Register the UNION of their Tier 2 tools, each bound to its own
# capsule. Manifests read once at startup (FastMCP registers statically); a capsule inserted later is
# picked up on the next server start — acceptable, and the whole surface is best-effort by design.
# ponytail: startup-time registration; if hot-add of tools without restart is ever needed, add a generic
# capsule_invoke(id, tool, args) dispatcher — YAGNI until a capsule ships mid-session.
def _all_capsule_manifests() -> dict:
    """{registry_key: manifest} for every pairputer capsule known at startup (env default + discovered).

    The KEY is a valid registry/image key (so bridge routing resolves to an ARN). It can differ from the
    manifest's friendly id: the bundled capsule's registry key is 'doom' while its manifest id is
    'agent-doom'. The friendly id is used only for the tool NAMESPACE (derived per-capsule below)."""
    out: dict = {}
    if CAPSULE_MANIFEST:  # the env-seeded (bundled) capsule -> key by a REGISTRY KEY, not the manifest id.
        reg = _effective_registry()
        mid = CAPSULE_MANIFEST.get("id") or ""
        # Prefer the manifest id IF it's a real registry key (the correct, consistent config). Else bind to
        # the registry key that is NOT itself a tag-discovered cartridge — i.e. the bundled entry — so the
        # bundled manifest never collides onto a discovered capsule's key. Falls back to mid for dev/no-reg.
        if mid in reg:
            key = mid
        else:
            discovered = set(_discover_capsules_by_tag())
            bundled_keys = [k for k in reg if k not in discovered]
            key = bundled_keys[0] if bundled_keys else (mid or "_bundled")
        out[key] = CAPSULE_MANIFEST
    for cid, _meta in _discover_capsules_by_tag().items():  # cartridge: tag id == image key == manifest id
        try:
            m = (_release_for(cid) or {}).get("manifest") or {}
        except Exception as exc:
            log.error("capsule %s release rejected; capsule stays agent-inert: %s", cid, exc)
            m = {}
        if m:  # verified immutable release wins over the env seed if the same capsule
            out[cid] = m
        else:
            # A discovered production capsule may never fall back to a mutable
            # process-wide manifest after its release binding fails.
            out.pop(cid, None)
    return out


def _manifest_namespace(reg_key: str, manifest: dict) -> str:
    """Namespace for a capsule's tool names — its friendly manifest id, else the registry key."""
    return manifest.get("id") or reg_key


_REGISTERED_TIER2: set = set()
_REGISTERED_TIER2_BINDINGS: dict = {}
_TIER1_ANY = False  # any capsule declaring interaction.tier1 turns the universal primitives ON
_PATTERNS_BY_IMAGE: dict = {}  # {registry_key: sensitivePatterns} — Tier 1 screens against the TARGET capsule
_AGENT_ALLOWED_BY_IMAGE: dict = {}  # {registry_key: bool} — per-capsule agentInteract (token claim)
_MANIFESTS_BY_IMAGE: dict = _all_capsule_manifests()  # startup snapshot; capsule_invoke refreshes at call time
for _cap_image, _cap_manifest in _MANIFESTS_BY_IMAGE.items():
    _AGENT_ALLOWED_BY_IMAGE[_cap_image] = bool(
        (_cap_manifest.get("interaction") or {}).get("tier1") or (_cap_manifest.get("tools") or []))
    if (_cap_manifest.get("interaction") or {}).get("tier1"):
        _TIER1_ANY = True
    _cap_ns = _manifest_namespace(_cap_image, _cap_manifest)
    _cap_patterns = [str(p) for p in ((_cap_manifest.get("safety") or {}).get("sensitivePatterns") or [])]
    _cap_release_digest = (
        "" if LOCAL_MODE or _cap_image not in _discover_capsules_by_tag()
        else str((_release_for(_cap_image) or {}).get("releaseDigest") or "")
    )
    _PATTERNS_BY_IMAGE[_cap_image] = _cap_patterns
    _cap_bridge = None
    for _spec in (_cap_manifest.get("tools") or []):
        if not (isinstance(_spec, dict) and _spec.get("name")):
            continue
        if _spec.get("advertise") is False:
            # Not advertised in tools/list — a pure context-size optimization, NOT a capability
            # change: the tool stays in the manifest with identical gates, remains callable via
            # capsule_invoke (which re-resolves the manifest and enforces the same approval and
            # sensitive-pattern screening), and is discoverable via capsule_metadata.
            continue
        if _cap_bridge is None:
            _cap_bridge = _canonical_bridge_settings(_cap_manifest, _cap_image)
        # Dedup on the PLATFORM-namespaced name (<capsule>__<verb>). Two capsules declaring the same verb
        # get distinct tools (each namespaced to its capsule); a capsule repeating a verb is deduped.
        _reg_name = _namespaced_tool_name(_cap_ns, _spec["name"])
        prior_binding = _REGISTERED_TIER2_BINDINGS.get(_reg_name)
        if prior_binding is not None and prior_binding != _cap_image:
            raise RuntimeError(
                f"capsule tool namespace collision: {_reg_name!r} binds both {prior_binding!r} and {_cap_image!r}")
        if _reg_name not in _REGISTERED_TIER2:
            _register_tier2_tool(
                _spec, _cap_image, _cap_patterns, _cap_ns,
                bind_release_digest=_cap_release_digest,
                bind_bridge=_cap_bridge,
            )
            _REGISTERED_TIER2.add(_reg_name)
            _REGISTERED_TIER2_BINDINGS[_reg_name] = _cap_image
_TIER1_ENABLED = _TIER1_ENABLED or _TIER1_ANY  # env flag OR any discovered capsule
# The bridge gate now reflects ALL capsules: interaction is allowed if any capsule enabled Tier 1 or
# registered a Tier 2 tool. A capsule that declared nothing stays agent-inert (the safe default).
_AGENT_INTERACT_ALLOWED = bool(_TIER1_ENABLED or _REGISTERED_TIER2)


# --- Hot-add: call-time manifest resolution (capsule_invoke + the per-capsule bridge gate) ----------
# FastMCP registers tools at startup, so a cartridge inserted MID-SESSION has no registered tools until
# the next server instance. capsule_invoke closes that gap: it resolves the capsule's manifest AT CALL
# TIME (tag discovery + SSM, briefly cached) and dispatches with the exact same gates as registered tools.
_MANIFEST_TTL_S = int(os.environ.get("PAIRPUTER_MANIFEST_TTL_S", "30"))
_manifest_live_cache: dict = {}  # image_id -> (expires, manifest)


def _manifest_for(image_id: str) -> dict:
    """Verified manifest from the capsule's atomic immutable release pointer."""
    if LOCAL_MODE and image_id in _MANIFESTS_BY_IMAGE:
        return _MANIFESTS_BY_IMAGE[image_id]
    now = time.time()
    hit = _manifest_live_cache.get(image_id)
    if hit and hit[0] > now:
        return hit[1]
    try:
        m = (_release_for(image_id) or {}).get("manifest") or {}
    except Exception as exc:
        log.error("capsule %s manifest rejected; capsule stays agent-inert: %s", image_id, exc)
        m = {}
    _manifest_live_cache[image_id] = (now + _MANIFEST_TTL_S, m)
    return m


def _capsule_metadata(image_id: str) -> dict:
    """Capsule-declared UX metadata for the widget and agents.

    Generic transport only: the platform does not interpret capability names or suggested prompt text.
    """
    image_id = _resolve_image_id(image_id)
    entry = _effective_registry().get(image_id) or {}
    manifest = _manifest_for(image_id)
    exp = manifest.get("experience") or {}
    interaction = manifest.get("interaction") or {}
    display = manifest.get("display") or {}
    bridge = manifest.get("bridge") or {}
    ns = _capsule_ns(_manifest_namespace(image_id, manifest)) if manifest else _capsule_ns(image_id)
    tools = [
        _namespaced_tool_name(_manifest_namespace(image_id, manifest), str(t.get("name")))
        for t in (manifest.get("tools") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    return {
        "imageId": image_id,
        "id": manifest.get("id") or image_id,
        "name": manifest.get("name") or entry.get("name") or image_id,
        "description": manifest.get("description") or entry.get("description", ""),
        "toolPrefix": ns,
        "tools": tools,
        "capabilities": [str(x) for x in (exp.get("capabilities") or [])][:12],
        "suggestedPrompts": [str(x) for x in (exp.get("suggestedPrompts") or [])][:8],
        "humanHelpText": str(exp.get("humanHelpText") or ""),
        "statusTemplate": str(exp.get("statusTemplate") or ""),
        # Generic capsule-owned presentation/control hints. The widget may use display dimensions as an
        # initial value, then replaces them with authoritative live co-play metadata when available.
        "display": display if isinstance(display, dict) else {},
        "control": {
            "agentInteractDefault": bool(interaction.get("agentInteractDefault", False)),
            "bridgeProtocol": str(bridge.get("protocol") or "http-json"),
            "bridgePort": int(bridge.get("port", 6905)) if str(bridge.get("port", 6905)).isdigit() else 6905,
        },
        "toolMetadata": {
            _namespaced_tool_name(_manifest_namespace(image_id, manifest), str(t.get("name"))):
                (_manifest_tool_meta(t).get("pairputer/tool") or {})
            for t in (manifest.get("tools") or [])
            if isinstance(t, dict) and t.get("name")
        },
    }


def _agent_interact_for(image_id: str) -> bool:
    """Per-capsule bridge gate: THIS capsule's manifest must declare interaction (tier1 or tools).
    Stricter than the old any-capsule global — capsule X's declaration never opens capsule Y."""
    if image_id in _AGENT_ALLOWED_BY_IMAGE:
        return _AGENT_ALLOWED_BY_IMAGE[image_id]
    m = _manifest_for(image_id)
    return bool((m.get("interaction") or {}).get("tier1") or (m.get("tools") or []))


@mcp.tool(
    name="capsule_approve",
    description=("Point-of-risk human approval boundary. Call only after the host has shown the pending "
                 "approval_id and the human explicitly confirms it. Returns one exact-action, expiring, "
                 "single-use token; it never widens task scope."),
    meta={"openai/requiresApproval": "always", "pairputer/approvalBoundary": "exact_action_single_use"},
)
def capsule_approve(ctx: Context, approval_id: str, action_digest: str,
                    confirmed_preview: dict) -> CallToolResult:
    identity = _caller_identity(ctx)
    if not LOCAL_MODE and not hosts.native_approval_enforced_for_client_id(identity.client_id):
        raise PermissionError(
            "this host has no verified native human-approval boundary; take control in the shared desktop"
        )
    item = _load_exact_approval(identity, approval_id)
    if item.get("status") != "REQUESTED":
        raise RuntimeError("approval request is no longer pending")
    # The action digest binds the hidden canonical args, while the full canonical
    # preview digest proves that the host presented every human-inspectable field.
    # Accepting only tool/image/digest would let a host omit the actual event list.
    stored_preview = item.get("preview") or {}
    if (not hmac.compare_digest(str(action_digest), str(item.get("action_digest") or "")) or
            not isinstance(confirmed_preview, dict) or
            not hmac.compare_digest(_approval_preview_digest(confirmed_preview),
                                    _approval_preview_digest(stored_preview))):
        raise PermissionError("approval confirmation does not match the exact displayed action")
    # A random 128-bit row reference is more reliable for an LLM host to relay than a long bearer.
    # Authority comes from the server-side GRANTED row and its exact digest/tenant/expiry conditions.
    token = approval_id
    token_digest = hashlib.sha256(token.encode()).hexdigest()
    if LOCAL_MODE:
        with _LOCAL_APPROVAL_LOCK:
            current = _LOCAL_APPROVALS.get(approval_id)
            if not current or current.get("status") != "REQUESTED":
                raise RuntimeError("approval request is no longer pending")
            current.update({"status": "GRANTED", "token_digest": token_digest, "granted_at": _now()})
    else:
        try:
            _session_table().update_item(
                Key={"pk": _session_pk(identity), "sk": f"APPROVAL#{approval_id}"},
                UpdateExpression="SET #status = :granted, token_digest = :digest, granted_at = :now",
                ConditionExpression=(Attr("status").eq("REQUESTED") & Attr("expires_at").gt(_now())),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":granted": "GRANTED", ":digest": token_digest, ":now": _now(),
                },
            )
        except ClientError as exc:
            if _client_error_code(exc) == "ConditionalCheckFailedException":
                raise RuntimeError("approval request is no longer pending") from exc
            raise
    return _compact_agent_result({
        "approved": True, "approval_id": approval_id, "approval_token": token,
        "action_digest": item["action_digest"], "expires_at": int(item["expires_at"]),
        "single_use": True,
    })


@mcp.tool(name="capsule_invoke",
          description="Invoke a capsule-declared agent tool by capsule id + verb — the hot-add path for "
                      "a capsule inserted after this server started (its typed tools aren't registered "
                      "yet). Same safety gates as the typed tools. Prefer the <capsule>__<verb> tools "
                      "when they exist; use list_capsules to find capsule ids.")
def capsule_invoke(ctx: Context, capsule_id: str, tool: str, args: dict | None = None,
                   approval_token: str = "") -> CallToolResult:
    a = args or {}
    m = _manifest_for(capsule_id)
    if not m:
        raise RuntimeError(f"capsule '{capsule_id}' has no capability manifest — it is agent-inert. "
                           f"Use list_capsules for available capsules.")
    verb = tool.split("__", 1)[-1]  # accept both the bare verb and the namespaced form
    spec = next((t for t in (m.get("tools") or [])
                 if isinstance(t, dict) and t.get("name") == verb), None)
    if not spec:
        declared = ", ".join(t.get("name", "?") for t in (m.get("tools") or []) if isinstance(t, dict))
        raise RuntimeError(f"capsule '{capsule_id}' does not declare tool '{verb}'. Declared: {declared or '(none)'}")
    patterns = [str(p) for p in ((m.get("safety") or {}).get("sensitivePatterns") or [])]
    # Same gates as a registered tool: human approval when declared + sensitive-pattern screening always.
    identity = _caller_identity(ctx)
    _require_approval(f"{capsule_id}:{verb}", bool(spec.get("requiresApproval")), patterns,
                      identity, capsule_id, approval_token, a)
    path = spec.get("path") or ("/" + verb.lstrip("/"))
    timeout_s = float(spec.get("timeoutSeconds") or spec.get("timeout_s") or 25)
    _note_agent_action(identity, capsule_id, spec.get("label") or verb)  # theatre of work
    return _compact_agent_result(_bridge(identity, capsule_id, "POST", path, a, timeout_s=timeout_s))


# interaction.md: even Tier 1 primitives (raw keys/pointer) must be screenable — a privileged capsule
# (one with an IAM role) driven by synthetic input can still take a real-world action, so the agent's raw
# input is screened against the TARGET capsule's sensitivePatterns, same as Tier 2. Resolve the target's
# patterns at call time (image_id may be omitted -> the default capsule).
def _patterns_for(image_id: str) -> list:
    target = _resolve_image_id(image_id)
    manifest = _manifest_for(target)
    return [str(pattern) for pattern in ((manifest.get("safety") or {}).get("sensitivePatterns") or [])]


def _screen_tier1(tool_name: str, identity: CallerIdentity, image_id: str,
                  payload: str, approval_token: str) -> str:
    target = _resolve_image_id(image_id)
    manifest = _manifest_for(target)
    if not bool((manifest.get("interaction") or {}).get("tier1")):
        raise PermissionError(
            f"capsule '{target}' does not permit raw Tier-1 input; use its semantic broker tools"
        )
    _require_approval(tool_name, False, _patterns_for(target), identity, target,
                      approval_token, {"input": payload})
    return target


# --- Tier 1: universal primitives (registered when the manifest declares interaction.tier1) -------
if _TIER1_ENABLED:
    @mcp.tool(name="capsule_send_keys",
              description="Type text into the live capsule the human is watching (synthetic keys, actor=agent). "
                          "Keys use browser KeyboardEvent.key names; plain characters map directly.")
    def capsule_send_keys(ctx: Context, text: str, image_id: str = "", approval_token: str = "") -> dict:
        identity = _caller_identity(ctx)
        image_id = _screen_tier1("capsule_send_keys", identity, image_id, text, approval_token)
        events = []
        for ch in text:
            key = {"\n": "Enter", "\t": "Tab"}.get(ch, ch)
            events.append({"t": "k", "key": key, "down": True})
            events.append({"t": "k", "key": key, "down": False})
        _note_agent_action(identity, image_id, f"typed {text!r}"[:60])  # theatre of work
        return _bridge(identity, image_id, "POST", "/input", {"events": events})

    @mcp.tool(name="capsule_key_chord",
              description="Press a key combination in the live capsule, e.g. 'Control+c' or a single key "
                          "like 'Escape' or 'ArrowUp' (browser KeyboardEvent.key names, '+'-separated).")
    def capsule_key_chord(ctx: Context, chord: str, image_id: str = "", approval_token: str = "") -> dict:
        identity = _caller_identity(ctx)
        image_id = _screen_tier1("capsule_key_chord", identity, image_id, chord, approval_token)
        keys = [k for k in chord.split("+") if k]
        events = [{"t": "k", "key": k, "down": True} for k in keys]
        events += [{"t": "k", "key": k, "down": False} for k in reversed(keys)]
        _note_agent_action(identity, image_id, f"pressed {chord}")
        return _bridge(identity, image_id, "POST", "/input", {"events": events})

    @mcp.tool(name="capsule_pointer",
              description="Pointer action in the live capsule. action: 'move' | 'click' | 'down' | 'up' | "
                          "'scroll' | 'drag'. x/y are display pixels; button 0=left 1=middle 2=right. "
                          "For 'scroll', 'amount' is notches (+down/-up). For 'drag', drag from x,y to x2,y2.")
    def capsule_pointer(ctx: Context, action: str, x: int = 0, y: int = 0, button: int = 0,
                        x2: int = 0, y2: int = 0, amount: int = 1, image_id: str = "",
                        approval_token: str = "") -> dict:
        identity = _caller_identity(ctx)
        image_id = _screen_tier1("capsule_pointer", identity, image_id,
                                 f"{action} {x},{y}", approval_token)
        events: list[dict] = []
        if action in ("move", "click"):
            events.append({"t": "m", "x": x, "y": y})
        if action == "click":
            events += [{"t": "b", "button": button, "down": True},
                       {"t": "b", "button": button, "down": False}]
        elif action == "down":
            events.append({"t": "b", "button": button, "down": True})
        elif action == "up":
            events.append({"t": "b", "button": button, "down": False})
        elif action == "scroll":
            # X buttons 4 (up) / 5 (down); server maps button index +1, so send index 3=up, 4=down.
            idx = 4 if amount >= 0 else 3
            for _ in range(abs(amount) or 1):
                events += [{"t": "b", "button": idx, "down": True}, {"t": "b", "button": idx, "down": False}]
        elif action == "drag":
            events += [{"t": "m", "x": x, "y": y}, {"t": "b", "button": button, "down": True},
                       {"t": "m", "x": x2, "y": y2}, {"t": "b", "button": button, "down": False}]
        if not events:
            raise RuntimeError(f"unknown pointer action {action!r}")
        _note_agent_action(identity, image_id, f"{action} at {x},{y}" if action != "scroll" else f"scrolled {amount}")
        return _bridge(identity, image_id, "POST", "/input", {"events": events})

    @mcp.tool(name="capsule_read_screen",
              description="Grab one PNG frame of the live capsule display — the agent's eyes. Returns a "
                          "proper image content block the client renders (not base64 text).")
    def capsule_read_screen(ctx: Context, image_id: str = "") -> CallToolResult:
        # read_screen is a passive observation — no theatre entry, no focus grant (it doesn't 'drive').
        out = _bridge(_caller_identity(ctx), image_id, "GET", "/screen")
        b64 = out.get("b64") or out.get("data") or ""
        if not b64:
            # No frame (e.g. display not yet rendering) — surface the raw payload as text, flagged.
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(out)[:400])],
                                  isError=True)
        mime = "image/png" if (out.get("format") or "png") == "png" else "image/" + out["format"]
        # An MCP image content block so any client RENDERS it; keep a tiny text note for text-only logs.
        return CallToolResult(content=[
            ImageContent(type="image", data=b64, mimeType=mime),
            TextContent(type="text", text=f"live frame ({mime}, {len(b64)} b64 chars)"),
        ])


# (resource), and an embedded <iframe> (frame) — the frame path is the candidate that could carry a
# stream the sandboxed widget itself can't fetch.
def _resource_meta() -> dict:
    domains = [VIDEO_RELAY_URL] if VIDEO_RELAY_URL else []
    return {
        "ui": {"csp": {"connectDomains": domains, "resourceDomains": domains,
                       "frameDomains": domains}, "permissions": {}},
        "openai/widgetCSP": {"connect_domains": domains, "resource_domains": domains,
                             "frame_domains": domains, "redirect_domains": domains},
    }


@mcp.resource(RESOURCE_URI, mime_type=MIME, meta=_resource_meta())
def component() -> str:
    return _component_html()



if __name__ == "__main__":
    # AgentCore expects streamable-http MCP on 0.0.0.0:8000/mcp. FastMCP serves exactly that.
    mcp.run(transport="streamable-http")
