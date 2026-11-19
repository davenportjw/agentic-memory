"""Firebase session-cookie authentication tests.

Throwaway RSA keys / self-signed certificates are generated per run and injected as the
certificate source, so no Google credentials or network calls are needed.
"""

import base64
import datetime
import json
import time
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from src.common import firebase_session as fs
from src.common.auth import is_email_authorized

PROJECT = "test-firebase-project"


def _make_key_and_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-signer")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(key, payload: dict, kid: str, alg: str = "RS256") -> str:
    header = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = key.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64(sig)}"


@pytest.fixture()
def signer(monkeypatch):
    monkeypatch.setenv("FIREBASE_PROJECT_ID", PROJECT)
    monkeypatch.delenv("K_SERVICE", raising=False)
    key, pem = _make_key_and_cert()
    kid = uuid.uuid4().hex
    return {"key": key, "kid": kid, "certs": {kid: pem}}


def _session_claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": f"https://session.firebase.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "uid-123",
        "email": "person@example.com",
        "email_verified": True,
        "auth_time": now - 60,
        "iat": now - 60,
        "exp": now + 3600,
        "firebase": {"sign_in_provider": "google.com"},
    }
    claims.update(overrides)
    return claims


def _id_token_claims(**overrides):
    claims = _session_claims(iss=f"https://securetoken.google.com/{PROJECT}")
    claims.update(overrides)
    return claims


def _active_account(_uid):
    return fs.AccountState(disabled=False, valid_since=0)


def _verify(signer, token, lookup=_active_account):
    return fs.verify_firebase_session_cookie(token, get_cert=signer["certs"].get, lookup_account=lookup)


# ---------------------------------------------------------------------------
# Session cookie verification
# ---------------------------------------------------------------------------


def test_valid_session_cookie(signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"])
    identity = _verify(signer, token)
    assert identity.email == "person@example.com"
    assert identity.uid == "uid-123"


def test_forged_cookie_signed_with_wrong_key_rejected(signer):
    other_key, _ = _make_key_and_cert()
    token = _sign(other_key, _session_claims(), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="signature"):
        _verify(signer, token)


def test_unknown_kid_rejected(signer):
    token = _sign(signer["key"], _session_claims(), "unknown-kid")
    with pytest.raises(fs.FirebaseAuthError):
        _verify(signer, token)


def test_tampered_payload_rejected(signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"])
    header, _, sig = token.split(".")
    forged_body = _b64(json.dumps(_session_claims(email="admin@example.com")).encode())
    with pytest.raises(fs.FirebaseAuthError, match="signature"):
        _verify(signer, f"{header}.{forged_body}.{sig}")


def test_non_rs256_alg_rejected(signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"], alg="HS256")
    with pytest.raises(fs.FirebaseAuthError, match="algorithm"):
        _verify(signer, token)


def test_wrong_issuer_rejected(signer):
    # An ID token (securetoken issuer) must not be accepted as a session cookie.
    token = _sign(signer["key"], _session_claims(iss=f"https://securetoken.google.com/{PROJECT}"), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="issuer"):
        _verify(signer, token)


def test_wrong_audience_rejected(signer):
    token = _sign(signer["key"], _session_claims(aud="another-project"), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="audience"):
        _verify(signer, token)


def test_expired_session_rejected(signer):
    now = int(time.time())
    token = _sign(signer["key"], _session_claims(exp=now - 1, iat=now - 7200, auth_time=now - 7200), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="expired"):
        _verify(signer, token)


def test_revoked_session_rejected(signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="revoked"):
        _verify(signer, token, lookup=lambda uid: fs.AccountState(disabled=False, valid_since=int(time.time())))


def test_disabled_account_rejected(signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="disabled"):
        _verify(signer, token, lookup=lambda uid: fs.AccountState(disabled=True, valid_since=0))


def test_deleted_account_rejected(signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="no longer exists"):
        _verify(signer, token, lookup=lambda uid: None)


def test_account_lookup_failure_fails_closed(signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"])

    def broken_lookup(_uid):
        raise RuntimeError("identity toolkit unavailable")

    with pytest.raises(RuntimeError):
        _verify(signer, token, lookup=broken_lookup)


def test_non_google_provider_rejected(signer):
    token = _sign(signer["key"], _session_claims(firebase={"sign_in_provider": "password"}), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="Google"):
        _verify(signer, token)


def test_unverified_email_rejected(signer):
    token = _sign(signer["key"], _session_claims(email_verified=False), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="verified"):
        _verify(signer, token)


# ---------------------------------------------------------------------------
# ID token verification (login exchange)
# ---------------------------------------------------------------------------


def test_valid_id_token(signer):
    token = _sign(signer["key"], _id_token_claims(), signer["kid"])
    identity = fs.verify_firebase_id_token(token, max_auth_age_seconds=600, get_cert=signer["certs"].get)
    assert identity.email == "person@example.com"


def test_id_token_stale_sign_in_rejected(signer):
    now = int(time.time())
    token = _sign(signer["key"], _id_token_claims(auth_time=now - 3600), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="too old"):
        fs.verify_firebase_id_token(token, max_auth_age_seconds=600, get_cert=signer["certs"].get)


def test_id_token_wrong_audience_and_provider_rejected(signer):
    bad_aud = _sign(signer["key"], _id_token_claims(aud="another-project"), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="audience"):
        fs.verify_firebase_id_token(bad_aud, get_cert=signer["certs"].get)
    anon = _sign(signer["key"], _id_token_claims(firebase={"sign_in_provider": "anonymous"}), signer["kid"])
    with pytest.raises(fs.FirebaseAuthError, match="Google"):
        fs.verify_firebase_id_token(anon, get_cert=signer["certs"].get)


def test_google_oidc_token_requires_service_audience(signer):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "https://ui.example.com",
        "sub": "123",
        "email": "runner@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 600,
    }
    token = _sign(signer["key"], claims, signer["kid"])
    assert fs.verify_google_oidc_token(token, ["https://ui.example.com"], get_cert=signer["certs"].get).email == "runner@example.com"
    with pytest.raises(fs.FirebaseAuthError, match="audience"):
        fs.verify_google_oidc_token(token, ["https://other.example.com"], get_cert=signer["certs"].get)


# ---------------------------------------------------------------------------
# Environment guards and allowlist
# ---------------------------------------------------------------------------


def test_local_dev_login_impossible_on_cloud_run(monkeypatch):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv("K_SERVICE", "ui-service")
    assert fs.is_local_dev_auth_allowed() is False
    with pytest.raises(fs.FirebaseAuthError):
        fs.create_local_dev_session("person@example.com")


def test_local_dev_session_not_honored_on_cloud_run(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    token = fs.create_local_dev_session("person@example.com")
    assert fs.verify_local_dev_session(token) == "person@example.com"
    monkeypatch.setenv("K_SERVICE", "ui-service")
    assert fs.verify_local_dev_session(token) is None


def test_empty_allowlist_fails_closed():
    assert is_email_authorized("person@example.com", "") is False
    assert is_email_authorized("person@example.com", " , ") is False
    assert is_email_authorized("person@example.com", "*") is True
    assert is_email_authorized("person@example.com", "example.com") is True
    assert is_email_authorized("person@other.example", "example.com") is False


# ---------------------------------------------------------------------------
# Middleware (end to end through the FastAPI app)
# ---------------------------------------------------------------------------


@pytest.fixture()
def enforced_client(signer, monkeypatch):
    """App client with Cloud Run semantics: no dev bypass, injected Google certificates."""
    from src.ui_service.server import api

    monkeypatch.setenv("K_SERVICE", "ui-service")
    real_verify = fs.verify_firebase_session_cookie

    def verify_with_test_keys(cookie, **kwargs):
        kwargs.setdefault("get_cert", signer["certs"].get)
        kwargs.setdefault("lookup_account", _active_account)
        return real_verify(cookie, **kwargs)

    monkeypatch.setattr(fs, "verify_firebase_session_cookie", verify_with_test_keys)
    import src.ui_service.server as server

    monkeypatch.setattr(server.config, "allowed_email_domains", "example.com")
    return TestClient(api, base_url="https://testserver")


# Unknown API path: 401 when unauthenticated, 404 once auth passes (no backend calls).
PROTECTED = "/api/__auth_probe__"


def test_middleware_accepts_valid_session_cookie(enforced_client, signer):
    token = _sign(signer["key"], _session_claims(), signer["kid"])
    res = enforced_client.get("/api/auth/config", cookies={"session": token})
    assert res.status_code == 200
    assert res.json()["isLocalDev"] is False
    assert enforced_client.get(PROTECTED, cookies={"session": token}).status_code == 404


def test_middleware_rejects_forged_cookie(enforced_client, signer):
    other_key, _ = _make_key_and_cert()
    forged = _sign(other_key, _session_claims(), signer["kid"])
    assert enforced_client.get(PROTECTED, cookies={"session": forged}).status_code == 401


def test_middleware_rejects_allowlist_miss(enforced_client, signer):
    token = _sign(signer["key"], _session_claims(email="outsider@other.example"), signer["kid"])
    assert enforced_client.get(PROTECTED, cookies={"session": token}).status_code == 403


def test_middleware_ignores_spoofed_identity_headers(enforced_client):
    spoofed = {
        "X-Goog-Authenticated-User-Email": "accounts.google.com:person@example.com",
        "X-User-Email": "person@example.com",
        "X-User-Id": "uid-123",
    }
    assert enforced_client.get(PROTECTED, headers=spoofed).status_code == 401
    html = enforced_client.get("/", headers=spoofed, follow_redirects=False)
    assert html.status_code == 302
    assert "/login.html" in html.headers["location"]


def test_middleware_rejects_old_dev_cookie_and_dev_token_on_cloud_run(enforced_client):
    assert enforced_client.get(PROTECTED, cookies={"session": "dev-admin-session-cookie"}).status_code == 401
    assert enforced_client.get(PROTECTED, headers={"Authorization": "Bearer dev-admin-token"}).status_code == 401
    res = enforced_client.post("/api/auth/session", json={"idToken": fs.LOCAL_DEV_LOGIN_TOKEN})
    assert res.status_code == 401


def test_static_suffix_does_not_bypass_api_auth(enforced_client):
    assert enforced_client.get("/api/logs/anything.js").status_code == 401


def test_login_exchange_sets_hardened_cookie(enforced_client, signer, monkeypatch):
    import src.ui_service.server as server

    id_token = _sign(signer["key"], _id_token_claims(), signer["kid"])
    real_verify_id = fs.verify_firebase_id_token
    monkeypatch.setattr(
        server,
        "verify_firebase_id_token",
        lambda tok, **kw: real_verify_id(tok, get_cert=signer["certs"].get, **kw),
    )
    minted = _sign(signer["key"], _session_claims(), signer["kid"])
    calls = []
    monkeypatch.setattr(server, "create_firebase_session_cookie", lambda tok, ttl: calls.append((tok, ttl)) or minted)

    res = enforced_client.post("/api/auth/session", json={"idToken": id_token})
    assert res.status_code == 200, res.text
    assert calls == [(id_token, fs.SESSION_TTL_SECONDS)]
    set_cookie = res.headers["set-cookie"].lower()
    for attr in ("httponly", "secure", "samesite=lax", "path=/", f"max-age={fs.SESSION_TTL_SECONDS}"):
        assert attr in set_cookie

    # Allowlist applies before a session is minted.
    outsider = _sign(signer["key"], _id_token_claims(email="outsider@other.example"), signer["kid"])
    assert enforced_client.post("/api/auth/session", json={"idToken": outsider}).status_code == 403
    assert len(calls) == 1
