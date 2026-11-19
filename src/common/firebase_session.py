"""Firebase session cookies for the UI service (Google sign-in only).

Login: the browser signs in with Google through the Firebase JS SDK and POSTs the ID token to
``/api/auth/session``. The server verifies it (RS256 against Google's securetoken certificates,
``iss``/``aud`` for this project, ``exp``, ``email_verified``, ``google.com`` provider and a sign-in
no older than 10 minutes), applies the email allowlist, then asks Identity Toolkit to mint a
Firebase session cookie (``projects/{project}:createSessionCookie``, ADC bearer). Google signs the
cookie; the app holds no signing secret.

Every request: the cookie is verified against Google's session-cookie certificates
(``iss=https://session.firebase.google.com/{project}``), ``email_verified`` and the ``google.com``
provider are re-checked, and ``accounts:lookup`` confirms the account is not disabled or revoked
(cached ~5 minutes). Anything unexpected fails closed.

Off Cloud Run (``K_SERVICE`` unset) a local developer login is available for tests and local runs;
it is impossible on Cloud Run.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger("adk.firebase_session")

SESSION_COOKIE_NAME = "session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 5  # 5 days
SESSION_EXCHANGE_MAX_AUTH_AGE_SECONDS = 10 * 60
ACCOUNT_STATE_TTL_SECONDS = 5 * 60
ACCOUNT_STATE_STALE_GRACE_SECONDS = 55 * 60

ID_TOKEN_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
SESSION_COOKIE_CERTS_URL = "https://www.googleapis.com/identitytoolkit/v3/relyingparty/publicKeys"
GOOGLE_OIDC_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_OIDC_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Marker the login page sends for the local developer login (refused on Cloud Run).
LOCAL_DEV_LOGIN_TOKEN = "local-dev-login"

GetCert = Callable[[str], Optional[str]]


class FirebaseAuthError(Exception):
    """Raised when a token or session cookie is rejected."""


@dataclass(frozen=True)
class VerifiedIdentity:
    uid: str
    email: str
    name: Optional[str] = None
    picture: Optional[str] = None
    auth_time: Optional[int] = None
    exp: Optional[int] = None


@dataclass(frozen=True)
class AccountState:
    disabled: bool
    valid_since: int  # seconds since epoch; sessions signed in before this were revoked


LookupAccount = Callable[[str], Optional[AccountState]]


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def is_running_on_cloud_run() -> bool:
    return bool(os.environ.get("K_SERVICE"))


def is_local_dev_auth_allowed() -> bool:
    """Local developer login / auth bypass. Never available on Cloud Run."""
    if is_running_on_cloud_run():
        return False
    return os.environ.get("DEV_AUTH_BYPASS", "true").strip().lower() in ("1", "true", "yes")


def get_firebase_project_id() -> str:
    project = (os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    if not project:
        raise FirebaseAuthError("FIREBASE_PROJECT_ID / GOOGLE_CLOUD_PROJECT is not configured")
    return project


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


class _CertCache:
    """Caches a Google `{kid: PEM certificate}` endpoint honoring Cache-Control max-age."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._certs: Dict[str, str] = {}
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get(self, kid: str) -> Optional[str]:
        with self._lock:
            if time.time() >= self._expires_at or kid not in self._certs:
                resp = httpx.get(self.url, timeout=10.0)
                if resp.status_code != 200:
                    raise FirebaseAuthError(f"Failed to fetch certificates from {self.url}: HTTP {resp.status_code}")
                certs = resp.json()
                if not isinstance(certs, dict):
                    raise FirebaseAuthError("Unexpected certificate response")
                match = re.search(r"max-age=(\d+)", resp.headers.get("cache-control", ""), re.I)
                self._certs = {str(k): str(v) for k, v in certs.items()}
                self._expires_at = time.time() + (int(match.group(1)) if match else 3600)
            return self._certs.get(kid)


_id_token_certs = _CertCache(ID_TOKEN_CERTS_URL)
_session_cookie_certs = _CertCache(SESSION_COOKIE_CERTS_URL)
_google_oidc_certs = _CertCache(GOOGLE_OIDC_CERTS_URL)


# ---------------------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _verify_rs256_jwt(token: str, get_cert: GetCert) -> Dict[str, Any]:
    """Verify an RS256 JWT signature against the certificate for its `kid`; return the payload."""
    parts = (token or "").strip().split(".")
    if len(parts) != 3:
        raise FirebaseAuthError("Invalid token format")
    raw_header, raw_payload, raw_sig = parts
    try:
        header = json.loads(_b64url_decode(raw_header))
        payload = json.loads(_b64url_decode(raw_payload))
        signature = _b64url_decode(raw_sig)
    except Exception as exc:  # noqa: BLE001
        raise FirebaseAuthError("Malformed token") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise FirebaseAuthError("Malformed token")
    kid = header.get("kid")
    if header.get("alg") != "RS256" or not kid:
        raise FirebaseAuthError("Unsupported token algorithm or missing key ID")
    pem = get_cert(str(kid))
    if not pem:
        raise FirebaseAuthError("No matching public key for token")
    try:
        public_key = x509.load_pem_x509_certificate(pem.encode("utf-8")).public_key()
    except Exception as exc:  # noqa: BLE001
        raise FirebaseAuthError("Unusable public key certificate") from exc
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise FirebaseAuthError("Unexpected public key type")
    try:
        public_key.verify(signature, f"{raw_header}.{raw_payload}".encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise FirebaseAuthError("Invalid token signature") from exc
    return payload


def _require_google_identity(payload: Dict[str, Any]) -> Tuple[str, str]:
    uid = str(payload.get("sub") or payload.get("user_id") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    if not uid or not email:
        raise FirebaseAuthError("Token is missing uid or email")
    if payload.get("email_verified") is not True:
        raise FirebaseAuthError("Email is not verified")
    firebase_claim = payload.get("firebase") if isinstance(payload.get("firebase"), dict) else {}
    if firebase_claim.get("sign_in_provider") != "google.com":
        raise FirebaseAuthError("Sign in with Google to use this app")
    return uid, email


def verify_firebase_id_token(
    id_token: str,
    *,
    max_auth_age_seconds: Optional[int] = None,
    get_cert: Optional[GetCert] = None,
    now: Optional[int] = None,
) -> VerifiedIdentity:
    """Verify a Firebase ID token issued to this project for a verified Google account."""
    project = get_firebase_project_id()
    payload = _verify_rs256_jwt(id_token, get_cert or _id_token_certs.get)
    now_sec = int(now if now is not None else time.time())
    if payload.get("aud") != project:
        raise FirebaseAuthError("Invalid token audience")
    if payload.get("iss") != f"https://securetoken.google.com/{project}":
        raise FirebaseAuthError("Invalid token issuer")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= now_sec:
        raise FirebaseAuthError("ID token has expired")
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)) or iat > now_sec + 60:
        raise FirebaseAuthError("Invalid token iat")
    auth_time = payload.get("auth_time")
    if not isinstance(auth_time, (int, float)) or auth_time > now_sec + 60:
        raise FirebaseAuthError("Invalid token auth_time")
    uid, email = _require_google_identity(payload)
    if max_auth_age_seconds is not None and now_sec - int(auth_time) > max_auth_age_seconds:
        raise FirebaseAuthError("Sign-in is too old. Please sign in again.")
    return VerifiedIdentity(
        uid=uid,
        email=email,
        name=payload.get("name"),
        picture=payload.get("picture"),
        auth_time=int(auth_time),
        exp=int(exp),
    )


def verify_firebase_session_cookie(
    cookie: str,
    *,
    get_cert: Optional[GetCert] = None,
    lookup_account: Optional[LookupAccount] = None,
    now: Optional[int] = None,
) -> VerifiedIdentity:
    """Verify a Firebase session cookie, including disabled / revoked account checks. Fails closed."""
    project = get_firebase_project_id()
    payload = _verify_rs256_jwt(cookie, get_cert or _session_cookie_certs.get)
    now_sec = int(now if now is not None else time.time())
    if payload.get("aud") != project:
        raise FirebaseAuthError("Invalid session cookie audience")
    if payload.get("iss") != f"https://session.firebase.google.com/{project}":
        raise FirebaseAuthError("Invalid session cookie issuer")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= now_sec:
        raise FirebaseAuthError("Session has expired")
    auth_time = payload.get("auth_time")
    if not isinstance(auth_time, (int, float)) or auth_time > now_sec + 60:
        raise FirebaseAuthError("Invalid session auth_time")
    uid, email = _require_google_identity(payload)

    account = (lookup_account or lookup_firebase_account_state)(uid)
    if account is None:
        raise FirebaseAuthError("Account no longer exists")
    if account.disabled:
        raise FirebaseAuthError("Account is disabled")
    if int(auth_time) < account.valid_since:
        raise FirebaseAuthError("Session has been revoked")
    return VerifiedIdentity(
        uid=uid,
        email=email,
        name=payload.get("name"),
        picture=payload.get("picture"),
        auth_time=int(auth_time),
        exp=int(exp),
    )


def verify_google_oidc_token(
    token: str,
    audiences: Iterable[str],
    *,
    get_cert: Optional[GetCert] = None,
    now: Optional[int] = None,
) -> VerifiedIdentity:
    """Verify a Google-signed OIDC identity token minted for one of this service's audiences."""
    allowed = {a for a in audiences if a}
    if not allowed:
        raise FirebaseAuthError("No accepted OIDC audiences configured")
    payload = _verify_rs256_jwt(token, get_cert or _google_oidc_certs.get)
    now_sec = int(now if now is not None else time.time())
    if payload.get("iss") not in GOOGLE_OIDC_ISSUERS:
        raise FirebaseAuthError("Invalid OIDC token issuer")
    if payload.get("aud") not in allowed:
        raise FirebaseAuthError("Invalid OIDC token audience")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= now_sec:
        raise FirebaseAuthError("OIDC token has expired")
    email = str(payload.get("email") or "").strip().lower()
    if not email or payload.get("email_verified") is not True:
        raise FirebaseAuthError("OIDC token email is missing or unverified")
    return VerifiedIdentity(uid=str(payload.get("sub") or ""), email=email, exp=int(exp))


# ---------------------------------------------------------------------------
# Identity Toolkit (ADC)
# ---------------------------------------------------------------------------

_adc_lock = threading.Lock()
_adc_credentials: Any = None


def _get_adc_access_token() -> str:
    global _adc_credentials
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest

    with _adc_lock:
        if _adc_credentials is None:
            _adc_credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not _adc_credentials.valid or not _adc_credentials.token:
            _adc_credentials.refresh(AuthRequest())
        return str(_adc_credentials.token)


def _identity_toolkit_post(path: str, body: Dict[str, Any]) -> httpx.Response:
    project = get_firebase_project_id()
    return httpx.post(
        f"https://identitytoolkit.googleapis.com/v1/projects/{project}{path}",
        headers={
            "Authorization": f"Bearer {_get_adc_access_token()}",
            "Content-Type": "application/json",
            "x-goog-user-project": project,
        },
        json=body,
        timeout=10.0,
    )


_account_cache: Dict[str, Tuple[AccountState, float]] = {}
_account_cache_lock = threading.Lock()


def lookup_firebase_account_state(uid: str) -> Optional[AccountState]:
    """Return whether an account is disabled / when its sessions became valid (cached ~5 minutes)."""
    now = time.time()
    with _account_cache_lock:
        cached = _account_cache.get(uid)
    if cached and now < cached[1]:
        return cached[0]
    try:
        resp = _identity_toolkit_post("/accounts:lookup", {"localId": [uid]})
        if resp.status_code != 200:
            raise FirebaseAuthError(f"Account lookup failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
    except Exception:
        # Ride out a brief outage on a recent answer; otherwise fail closed.
        if cached and now < cached[1] + ACCOUNT_STATE_STALE_GRACE_SECONDS:
            logger.warning("accounts:lookup failed; using recent cached state for %s", uid)
            return cached[0]
        raise
    users = data.get("users") or []
    if not users:
        return None
    user = users[0]
    try:
        valid_since = int(user.get("validSince") or 0)
    except (TypeError, ValueError):
        valid_since = 0
    state = AccountState(disabled=user.get("disabled") is True, valid_since=valid_since)
    with _account_cache_lock:
        _account_cache[uid] = (state, now + ACCOUNT_STATE_TTL_SECONDS)
    return state


def clear_account_state_cache() -> None:
    with _account_cache_lock:
        _account_cache.clear()


def create_firebase_session_cookie(id_token: str, valid_duration_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Mint a Google-signed Firebase session cookie from a freshly verified ID token."""
    resp = _identity_toolkit_post(":createSessionCookie", {"idToken": id_token, "validDuration": str(valid_duration_seconds)})
    if resp.status_code != 200:
        raise FirebaseAuthError(f"Could not create a session: HTTP {resp.status_code} {resp.text[:200]}")
    cookie = resp.json().get("sessionCookie")
    if not cookie:
        raise FirebaseAuthError("Could not create a session: empty response")
    return str(cookie)


# ---------------------------------------------------------------------------
# Local developer sessions (off Cloud Run only)
# ---------------------------------------------------------------------------

_local_dev_sessions: Dict[str, str] = {}
_local_dev_lock = threading.Lock()


def create_local_dev_session(email: str) -> str:
    if not is_local_dev_auth_allowed():
        raise FirebaseAuthError("Local developer login is disabled")
    token = "local-dev." + secrets.token_urlsafe(32)
    with _local_dev_lock:
        _local_dev_sessions[token] = email
    return token


def verify_local_dev_session(token: str) -> Optional[str]:
    if not token or not is_local_dev_auth_allowed():
        return None
    with _local_dev_lock:
        return _local_dev_sessions.get(token)


def revoke_local_dev_session(token: Optional[str]) -> None:
    if token:
        with _local_dev_lock:
            _local_dev_sessions.pop(token, None)
