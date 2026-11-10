"""Pytest fixtures and configuration for electric-sheep demo."""

import os
import pytest

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Ensure tests use configured GCP project from environment/.env or fall back to test-gcp-project
_default_project = (
    os.environ.get("GOOGLE_CLOUD_PROJECT")
    or os.environ.get("PROJECT_ID")
    or os.environ.get("GCP_PROJECT")
    or "test-gcp-project"
)
os.environ["GOOGLE_CLOUD_PROJECT"] = _default_project
os.environ["MODEL_NAME"] = os.environ.get("MODEL_NAME", "gemini-3.8-flash")
os.environ["ALLOWED_EMAIL_DOMAINS"] = os.environ.get("ALLOWED_EMAIL_DOMAINS", "example.com,user@example.com")
os.environ["FIREBASE_API_KEY"] = os.environ.get("FIREBASE_API_KEY", "your_firebase_api_key_here")
os.environ["UI_SERVICE_URL"] = "http://localhost:8080"
os.environ["INTERVIEW_AGENT_URL"] = ""
os.environ["PREFERENCE_AGENT_URL"] = ""
os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"

try:
    import socket
    import time

    _orig_getaddrinfo = socket.getaddrinfo
    _dns_cache = {}

    def _resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        key = (host, port, family, type, proto, flags)
        if key in _dns_cache:
            return _dns_cache[key]
        last_err = None
        for attempt in range(4):
            try:
                res = _orig_getaddrinfo(host, port, family, type, proto, flags)
                if res:
                    _dns_cache[key] = res
                return res
            except socket.gaierror as e:
                last_err = e
                time.sleep(0.15 * (attempt + 1))
        raise last_err  # type: ignore[misc]

    socket.getaddrinfo = _resilient_getaddrinfo
    for _h in ("oauth2.googleapis.com", "aiplatform.googleapis.com", "bigquery.googleapis.com", "spanner.googleapis.com"):
        try:
            socket.getaddrinfo(_h, 443, 0, socket.SOCK_STREAM)
        except Exception:
            pass
except Exception:
    pass

try:
    import google.auth
    import google.auth._cloud_sdk
    import google.auth._default
    import google.auth.transport.requests
    from google.auth.credentials import CredentialsWithQuotaProject, with_scopes_if_required

    _orig_get_project_id = google.auth._cloud_sdk.get_project_id

    def _fast_get_project_id():
        return (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("PROJECT_ID")
            or os.environ.get("GCP_PROJECT")
            or _orig_get_project_id()
        )

    google.auth._cloud_sdk.get_project_id = _fast_get_project_id

    _orig_default = google.auth._default.default
    _cached_creds = None
    _cached_project = None

    def _cached_google_auth_default(
        scopes=None, request=None, quota_project_id=None, default_scopes=None
    ):
        global _cached_creds, _cached_project
        if _cached_creds is None:
            _cached_creds, _cached_project = _orig_default()
        creds = with_scopes_if_required(
            _cached_creds, scopes, default_scopes=default_scopes
        )
        if quota_project_id and isinstance(creds, CredentialsWithQuotaProject):
            creds = creds.with_quota_project(quota_project_id)
        if not creds.valid or not creds.token:
            req = request or google.auth.transport.requests.Request()
            creds.refresh(req)
        eff_proj = os.environ.get("GOOGLE_CLOUD_PROJECT") or _cached_project
        return creds, eff_proj

    google.auth.default = _cached_google_auth_default
    google.auth._default.default = _cached_google_auth_default
    # Warm up credentials once before any gRPC threads are created
    _cached_google_auth_default()
except Exception:
    pass


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", _default_project))
    monkeypatch.setenv("MODEL_NAME", os.environ.get("MODEL_NAME", "gemini-3.8-flash"))
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", os.environ.get("ALLOWED_EMAIL_DOMAINS", "example.com,user@example.com"))
    monkeypatch.setenv("FIREBASE_API_KEY", os.environ.get("FIREBASE_API_KEY", "your_firebase_api_key_here"))
    monkeypatch.setenv("UI_SERVICE_URL", "http://localhost:8080")
    monkeypatch.setenv("INTERVIEW_AGENT_URL", "")
    monkeypatch.setenv("PREFERENCE_AGENT_URL", "")
    monkeypatch.setenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")

