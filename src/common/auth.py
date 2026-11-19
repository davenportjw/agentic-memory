"""Authentication and authorization helper utilities for electric-sheep ADK Demo.

Supports:
1. Firebase session cookies / Firebase ID tokens / audience-bound Google OIDC tokens
   (see `src.common.firebase_session`).
2. Cloud IAP JWT assertions, verified against IAP keys when `IAP_ENABLED=true` and
   `IAP_EXPECTED_AUDIENCE` is set. Unverified identity headers are never trusted.
3. Per-audience OIDC token caching (50-minute TTL) and 4-step ADC/IAM Credentials/gcloud retrieval.
4. Service-to-service auth header generation (`get_service_auth_headers`) with IAP header propagation.
5. Domain and email allowlist enforcement (`ALLOWED_EMAIL_DOMAINS`, fail closed when empty).
6. Local development bypass (`DEV_AUTH_BYPASS`, default on locally, impossible on Cloud Run).
"""

import logging
import os
import subprocess
import time
import urllib.parse
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx
from fastapi import Request

from src.common.config import get_config

logger = logging.getLogger("adk.auth")

# Per-audience cache: {audience_origin: (jwt_token, expiry_epoch_seconds)}
# Google OIDC tokens are valid for 3600s (1 hour); we cache for 3000s (50 minutes).
_TOKEN_CACHE: Dict[str, Tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = 3000.0


def clear_oidc_token_cache() -> None:
    """Clear the in-memory OIDC token cache."""
    _TOKEN_CACHE.clear()


def _normalize_audience(target_url: str) -> str:
    """Normalize a target URL into a canonical Cloud Run OIDC audience origin (https://host)."""
    cleaned = (target_url or "").strip()
    if not cleaned:
        return ""
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return cleaned.rstrip("/")


def is_email_authorized(email: str, allowed_list_str: str) -> bool:
    """Check if email is authorized in ALLOWED_EMAIL_DOMAINS.

    Supports both full email addresses (e.g. user@gmail.com) and domain suffixes (e.g. google.com).
    An explicit '*' allows every verified account; an empty allowlist allows nobody (fail closed).
    """
    if not email or "@" not in email:
        return False

    email = email.strip().lower()
    if not allowed_list_str or not allowed_list_str.strip():
        return False

    allowed_items = [item.strip().lower() for item in allowed_list_str.split(",") if item.strip()]
    if not allowed_items:
        return False

    email_domain = email.split("@", 1)[1]
    for item in allowed_items:
        if item == "*" or item == email or item == email_domain or item == f"@{email_domain}":
            return True
    return False


def get_oidc_token(target_url: str, force_refresh: bool = False) -> Optional[str]:
    """Acquire a Google-signed OIDC ID token for an internal or external Cloud Run service.

    Resolution order:
    0. If DEV_AUTH_BYPASS is enabled and target_url is local (non-HTTPS), return 'dev-admin-token'.
    1. Per-audience in-memory token cache (50-minute TTL) unless force_refresh=True.
    2. Compute Engine / Cloud Run metadata server or service account credentials via
       `google.oauth2.id_token.fetch_id_token(Request(), audience)`.
    3. Google Cloud ADC (`google.auth.default()`) calling IAM Credentials API
       `iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateIdToken`.
    4. Refreshed ADC user/workload credentials carrying an `id_token` attribute.
    5. Local `gcloud auth print-identity-token` CLI fallback for developer workstations.
    """
    audience = _normalize_audience(target_url)
    if not audience:
        return None

    dev_bypass = not os.environ.get("K_SERVICE") and os.environ.get("DEV_AUTH_BYPASS", "true").lower() in ("1", "true", "yes")
    if dev_bypass and not audience.startswith("https://"):
        return "dev-admin-token"

    now = time.time()
    if not force_refresh and audience in _TOKEN_CACHE:
        cached_token, expires_at = _TOKEN_CACHE[audience]
        if now < expires_at and cached_token:
            return cached_token

    # 1. Try google.oauth2.id_token.fetch_id_token (Cloud Run / GCE metadata server or SA JSON)
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2 import id_token

        token = id_token.fetch_id_token(AuthRequest(), audience)
        if token:
            _TOKEN_CACHE[audience] = (token, now + _TOKEN_TTL_SECONDS)
            return token
    except Exception as exc:
        logger.debug("fetch_id_token unavailable for %s: %s", audience, exc)

    # 2 & 3. Try Google Cloud Application Default Credentials (ADC)
    try:
        import google.auth
        from google.auth.transport.requests import Request as AuthRequest

        creds, _ = google.auth.default()
        auth_req = AuthRequest()
        creds.refresh(auth_req)

        # 2. If running as or impersonating a service account, try IAM Credentials generateIdToken
        sa_email = getattr(creds, "service_account_email", None) or getattr(creds, "_service_account_email", None)
        if sa_email and sa_email != "default" and getattr(creds, "token", None):
            iam_url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa_email}:generateIdToken"
            resp = httpx.post(
                iam_url,
                headers={"Authorization": f"Bearer {creds.token}"},
                json={"audience": audience, "includeEmail": True},
                timeout=5.0,
            )
            if resp.status_code == 200:
                iam_token = resp.json().get("token")
                if iam_token:
                    _TOKEN_CACHE[audience] = (iam_token, now + _TOKEN_TTL_SECONDS)
                    return iam_token

        # 3. Check if refreshed ADC credentials directly expose an OIDC id_token
        adc_id_token = getattr(creds, "id_token", None)
        if isinstance(adc_id_token, str) and adc_id_token.count(".") == 2:
            _TOKEN_CACHE[audience] = (adc_id_token, now + _TOKEN_TTL_SECONDS)
            return adc_id_token
    except Exception as exc:
        logger.debug("ADC id_token retrieval unavailable for %s: %s", audience, exc)

    # 4. Try gcloud CLI identity token (for local workstation invoking live Cloud Run)
    for cmd in (
        ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"],
        ["gcloud", "auth", "print-identity-token"],
    ):
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                token = res.stdout.strip()
                if token.count(".") == 2:
                    _TOKEN_CACHE[audience] = (token, now + _TOKEN_TTL_SECONDS)
                    return token
        except Exception as exc:
            logger.debug("gcloud print-identity-token failed (%s): %s", cmd, exc)

    if dev_bypass:
        return "dev-admin-token"
    return None


def extract_iap_user_email(headers: Mapping[str, str]) -> Optional[str]:
    """Extract the authenticated user email from Cloud IAP / Cloud Run headers if present."""
    raw = (
        headers.get("x-goog-authenticated-user-email")
        or headers.get("X-Goog-Authenticated-User-Email")
    )
    if not raw:
        return None
    cleaned = raw.strip()
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    return cleaned.strip() or None


def get_service_auth_headers(
    target_url: str,
    incoming_headers: Optional[Mapping[str, str]] = None,
    force_refresh: bool = False,
) -> Dict[str, str]:
    """Build outbound HTTP headers for service-to-service Cloud Run calls.

    - Attaches `Authorization: Bearer <oidc_token>` for `target_url` when available.
    - Propagates Cloud IAP identity headers (`X-Goog-Authenticated-User-Email`,
      `X-Goog-Authenticated-User-Id`, `X-Goog-IAP-JWT-Assertion`) from `incoming_headers`
      so downstream services retain end-user audit provenance.
    """
    headers: Dict[str, str] = {}
    token = get_oidc_token(target_url, force_refresh=force_refresh)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if incoming_headers:
        for iap_header in (
            "X-Goog-Authenticated-User-Email",
            "X-Goog-Authenticated-User-Id",
            "X-Goog-IAP-JWT-Assertion",
        ):
            val = incoming_headers.get(iap_header) or incoming_headers.get(iap_header.lower())
            if val:
                headers[iap_header] = val

    return headers


def get_default_dev_email(allowed_domains_str: Optional[str] = None) -> str:
    """Return a valid default developer email matching the allowlist."""
    domains_str = allowed_domains_str if allowed_domains_str is not None else os.environ.get("ALLOWED_EMAIL_DOMAINS", "")
    for entry in domains_str.split(","):
        cleaned = entry.strip().lower().lstrip("@")
        if not cleaned or cleaned == "*":
            continue
        if "@" in cleaned:
            return cleaned
        return f"dev-admin@{cleaned}"
    return "dev-admin@example.com"


IAP_PUBLIC_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key-jwk"
IAP_ISSUER = "https://cloud.google.com/iap"


def verify_iap_jwt_assertion(assertion: str, expected_audience: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Verify a Cloud IAP JWT assertion (ES256, IAP keys, iss and an explicit audience) and return its claims."""
    if not assertion or not expected_audience:
        return None
    from google.auth.transport.requests import Request as AuthRequest
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_token(
            assertion,
            AuthRequest(),
            audience=expected_audience,
            certs_url=IAP_PUBLIC_KEYS_URL,
        )
        if claims and claims.get("iss") == IAP_ISSUER and claims.get("email"):
            return claims
    except Exception as e:
        logger.debug("Failed to verify IAP JWT assertion: %s", e)
    return None


def _accepted_oidc_audiences() -> list:
    """Audiences accepted for Google-signed OIDC Bearer tokens: this service's own HTTPS origin."""
    audiences = []
    for raw in (os.environ.get("UI_SERVICE_URL", ""),):
        aud = _normalize_audience(raw)
        if aud.startswith("https://"):
            audiences.append(aud)
    return audiences


def get_authenticated_user_email(request: Request) -> Optional[str]:
    """Return the verified email for this request, or None.

    Accepted credentials (identity headers such as ``X-Goog-Authenticated-User-Email`` are never
    trusted on their own):
    1. ``session`` cookie: a Firebase session cookie (signature, issuer/audience, expiry,
       verified email, Google provider, not disabled/revoked). Off Cloud Run only, a local
       developer session created by the login page.
    2. ``Authorization: Bearer``: a Firebase ID token for this project (verified Google account),
       or a Google-signed OIDC token whose audience is this service's URL.
    3. A Cloud IAP JWT assertion, only when ``IAP_ENABLED=true`` and ``IAP_EXPECTED_AUDIENCE`` is set.
    The caller still applies the email allowlist.
    """
    from src.common import firebase_session as fs

    config = get_config()

    # 1. Session cookie
    session_cookie = request.cookies.get(fs.SESSION_COOKIE_NAME)
    if session_cookie:
        local_email = fs.verify_local_dev_session(session_cookie)
        if local_email:
            return local_email
        if session_cookie.count(".") == 2:
            try:
                return fs.verify_firebase_session_cookie(session_cookie).email
            except Exception as exc:  # noqa: BLE001 - fail closed
                logger.info("Session cookie rejected: %s", exc)

    # 2. Authorization Bearer header
    auth_header = request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token.count(".") == 2:
            try:
                return fs.verify_firebase_id_token(token).email
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bearer token is not a valid Firebase ID token: %s", exc)
            try:
                return fs.verify_google_oidc_token(token, _accepted_oidc_audiences()).email
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bearer token is not a valid OIDC token for this service: %s", exc)

    # 3. Cloud IAP (verified JWT assertion only)
    if config.iap_enabled and config.iap_expected_audience:
        iap_assertion = request.headers.get(config.iap_header_name) or request.headers.get("x-goog-iap-jwt-assertion")
        if iap_assertion:
            claims = verify_iap_jwt_assertion(iap_assertion, config.iap_expected_audience)
            if claims and claims.get("email"):
                return str(claims["email"]).strip().lower()

    return None
