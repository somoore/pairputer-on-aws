"""Defense-in-depth JWT verification (_verify_jwt): re-verifies the bearer token's RS256 signature +
iss + exp against Cognito's JWKS inside the container, so the tenant model doesn't rely SOLELY on
AgentCore being the only ingress. These prove it fails closed on a forged/tampered/expired token and
is a clean no-op when no discovery URL is configured."""
import ast
import base64
import json
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "substrate/mcp-server/server.py").read_text()

crypto = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers  # noqa: E402
from cryptography.exceptions import InvalidSignature  # noqa: E402


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load_verifier(discovery_url, jwks_keys, issuer):
    """Extract _verify_jwt (+ helpers it needs) with a mocked JWKS/OIDC fetch, no network."""
    tree = ast.parse(SERVER)
    names = {"_b64url_decode", "_verify_jwt", "_refresh_jwks", "_oidc_config"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    mod = ast.Module(body=fns, type_ignores=[]); ast.fix_missing_locations(mod)
    # force-refresh (unknown kid) must not hit the network in tests: a urlopen that raises makes the
    # refetch fail cleanly, so an unknown kid stays unresolved -> "signing key not found" (fail closed).
    def _no_net(*_a, **_k):
        raise OSError("no network in tests")
    ns = {
        "os": types.SimpleNamespace(environ={}),
        "json": json, "time": time, "base64": base64, "threading": __import__("threading"),
        "urllib": types.SimpleNamespace(request=types.SimpleNamespace(urlopen=_no_net)),
        "RSAPublicNumbers": RSAPublicNumbers, "padding": padding, "hashes": hashes,
        "InvalidSignature": InvalidSignature,
        "JWT_DISCOVERY_URL": discovery_url, "LOCAL_MODE": False,
        "_JWKS_CACHE": {"keys": dict(jwks_keys), "issuer": issuer, "fetched_at": time.time()},
        "_JWKS_TTL_S": 3600, "_JWKS_LOCK": __import__("threading").Lock(),
        "_JWT_VERIFY_WARNED": [False],
        "log": types.SimpleNamespace(warning=lambda *a, **k: None),
    }
    exec(compile(mod, "server.py:jwt", "exec"), ns)
    return ns["_verify_jwt"]


def make_token(priv, kid, issuer, exp_delta=3600, alg="RS256", tamper=False):
    header = {"alg": alg, "kid": kid}
    payload = {"iss": issuer, "sub": "user-1", "token_use": "access", "exp": int(time.time()) + exp_delta}
    signing_input = _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode())
    sig = priv.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    if tamper:
        sig = sig[:-1] + bytes([sig[-1] ^ 0xFF])
    return signing_input + "." + _b64url(sig)


@pytest.fixture
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"


def test_valid_token_passes(keypair):
    priv, pub = keypair
    verify = load_verifier("https://disco", {"kid1": pub}, ISSUER)
    verify(make_token(priv, "kid1", ISSUER))  # no exception == pass


def test_tampered_signature_fails(keypair):
    priv, pub = keypair
    verify = load_verifier("https://disco", {"kid1": pub}, ISSUER)
    with pytest.raises(PermissionError, match="signature"):
        verify(make_token(priv, "kid1", ISSUER, tamper=True))


def test_forged_key_fails(keypair):
    priv, _ = keypair
    other_pub = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    verify = load_verifier("https://disco", {"kid1": other_pub}, ISSUER)  # JWKS has a DIFFERENT key
    with pytest.raises(PermissionError, match="signature"):
        verify(make_token(priv, "kid1", ISSUER))


def test_expired_token_fails(keypair):
    priv, pub = keypair
    verify = load_verifier("https://disco", {"kid1": pub}, ISSUER)
    with pytest.raises(PermissionError, match="expired"):
        verify(make_token(priv, "kid1", ISSUER, exp_delta=-3600))


def test_wrong_issuer_fails(keypair):
    priv, pub = keypair
    verify = load_verifier("https://disco", {"kid1": pub}, ISSUER)
    with pytest.raises(PermissionError, match="issuer"):
        verify(make_token(priv, "kid1", "https://evil-issuer"))


def test_unknown_kid_fails(keypair):
    priv, pub = keypair
    verify = load_verifier("https://disco", {"kid1": pub}, ISSUER)
    # unknown kid triggers a force-refresh; our mocked _refresh_jwks returns the same cache -> still miss
    with pytest.raises(PermissionError, match="signing key not found"):
        verify(make_token(priv, "other-kid", ISSUER))


def test_non_rs256_alg_rejected(keypair):
    priv, pub = keypair
    verify = load_verifier("https://disco", {"kid1": pub}, ISSUER)
    with pytest.raises(PermissionError, match="alg"):
        verify(make_token(priv, "kid1", ISSUER, alg="none"))


def test_no_discovery_url_is_a_noop(keypair):
    priv, pub = keypair
    verify = load_verifier("", {"kid1": pub}, ISSUER)  # discovery unset -> rely on AgentCore, no-op
    verify("anything.at.all")  # must not raise
