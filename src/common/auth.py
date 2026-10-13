"""Authentication and authorization helper utilities for electric-sheep ADK Demo.

Implements email allowlist validation, session cookie and Bearer token verification,
and OIDC identity token generation for service-to-service Cloud Run communication.
"""

import logging
import os
import subprocess
import urllib.request
from typing import Optional, Dict, Any
from fastapi import Request

from src.common.config import get_config

logger = logging.getLogger("adk.auth")


def is_email_authorized(email: str, allowed_list_str: str) -> bool:
    """Check if email is authorized in ALLOWED_EMAIL_DOMAINS.

    Supports comma-separated list of exact email addresses or domain suffixes.
    """
    if not email:
        return False
    email = email.strip().lower()
    for entry in allowed_list_str.split(","):
        cleaned = entry.strip().lower().lstrip("@")
        if not cleaned:
            continue
        if "@" in cleaned:
            # Match specific email address exactly
            if email == cleaned:
                return True
        else:
            # Match entire domain suffix
            if email.endswith("@" + cleaned):
                return True
    return False


def get_oidc_token(target_url: str) -> Optional[str]:
    """Retrieve an OIDC identity token for service-to-service Cloud Run invocation."""
    dev_bypass = os.environ.get("DEV_AUTH_BYPASS", "true").lower() == "true"
    if dev_bypass and not (target_url.startswith("https://") and "run.app" in target_url):
        return "dev-admin-token"

    # 1. Query local GCP metadata server (inside Cloud Run container)
    try:
        req = urllib.request.Request(
            f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={target_url}",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                token = resp.read().decode("utf-8").strip()
                if token:
                    return token
    except Exception:
        pass

    # 2. Try gcloud CLI fallback for local developer environment
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    # 3. Try google-auth library fallback
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2 import id_token
        token = id_token.fetch_id_token(AuthRequest(), target_url)
        if token:
            return token
    except Exception:
        pass

    return None


def verify_token_claims(token: str) -> Optional[Dict[str, Any]]:
    """Verify a Firebase ID Token or Google OAuth2/OIDC ID Token and return its claims."""
    from google.auth.transport.requests import Request as AuthRequest
    from google.oauth2 import id_token

    # 1. Try verifying as Firebase ID Token
    try:
        claims = id_token.verify_firebase_token(token, AuthRequest(), audience=None)
        if claims and "email" in claims:
            return claims
    except Exception:
        pass

    # 2. Try verifying as standard Google OAuth2 / OIDC token
    try:
        claims = id_token.verify_oauth2_token(token, AuthRequest(), audience=None)
        if claims and "email" in claims:
            return claims
    except Exception:
        pass

    return None


def get_default_dev_email(allowed_domains_str: str) -> str:
    """Return a valid default developer email matching the allowlist."""
    for entry in allowed_domains_str.split(","):
        cleaned = entry.strip().lower().lstrip("@")
        if not cleaned:
            continue
        if "@" in cleaned:
            return cleaned
        return f"dev-admin@{cleaned}"
    return "dev-admin@example.com"


def verify_iap_jwt_assertion(assertion: str, expected_audience: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Verify Google Cloud Identity-Aware Proxy (IAP) JWT assertion and return its claims."""
    from google.auth.transport.requests import Request as AuthRequest
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_token(
            assertion,
            AuthRequest(),
            audience=expected_audience or None,
        )
        if claims and "email" in claims:
            return claims
    except Exception as e:
        logger.debug("Failed to verify IAP JWT assertion: %s", e)
    return None


def get_authenticated_user_email(request: Request) -> Optional[str]:
    """Inspect request cookies, Authorization headers, and IAP proxy headers to verify user identity.

    Returns the authenticated email address if valid and verified, else None.
    """
    config = get_config()
    default_dev_email = get_default_dev_email(config.allowed_email_domains)

    # 1. Check session cookie
    session_cookie = request.cookies.get("session")
    if session_cookie:
        dev_bypass = os.environ.get("DEV_AUTH_BYPASS", "true").lower() == "true"
        if dev_bypass and session_cookie == "dev-admin-session-cookie":
            return default_dev_email
        claims = verify_token_claims(session_cookie)
        if claims and claims.get("email"):
            return claims.get("email")

    # 2. Check Authorization Bearer header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        dev_bypass = os.environ.get("DEV_AUTH_BYPASS", "true").lower() == "true"
        if dev_bypass and token == "dev-admin-token":
            return default_dev_email
        claims = verify_token_claims(token)
        if claims and claims.get("email"):
            return claims.get("email")

    # 3. Check Google Cloud Identity-Aware Proxy (IAP) headers if enabled or present
    iap_assertion = request.headers.get(config.iap_header_name) or request.headers.get("x-goog-iap-jwt-assertion")
    if iap_assertion:
        claims = verify_iap_jwt_assertion(iap_assertion, config.iap_expected_audience)
        if claims and claims.get("email"):
            return claims.get("email")

    iap_email_header = request.headers.get("x-goog-authenticated-user-email") or request.headers.get("X-Goog-Authenticated-User-Email")
    if iap_email_header:
        # Format from IAP is 'accounts.google.com:user@example.com'
        email = iap_email_header.split(":", 1)[1].strip() if ":" in iap_email_header else iap_email_header.strip()
        if email:
            return email

    return None
