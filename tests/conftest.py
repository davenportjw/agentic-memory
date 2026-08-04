"""Pytest fixtures and configuration for electric-sheep demo."""

import os
import pytest

# Ensure all tests run in deterministic mock mode without network calls to GCP
os.environ["USE_MOCK_CLIENTS"] = os.environ.get("USE_MOCK_CLIENTS", "true")
os.environ["GOOGLE_CLOUD_PROJECT"] = os.environ.get("GOOGLE_CLOUD_PROJECT", "davenport-boutique")
os.environ["MODEL_NAME"] = os.environ.get("MODEL_NAME", "gemini-3.5-flash-lite")
os.environ["ALLOWED_EMAIL_DOMAINS"] = os.environ.get("ALLOWED_EMAIL_DOMAINS", "example.com,user@example.com")
os.environ["FIREBASE_API_KEY"] = os.environ.get("FIREBASE_API_KEY", "your_firebase_api_key_here")


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch):
    monkeypatch.setenv("USE_MOCK_CLIENTS", os.environ.get("USE_MOCK_CLIENTS", "true"))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", os.environ.get("GOOGLE_CLOUD_PROJECT", "davenport-boutique"))
    monkeypatch.setenv("MODEL_NAME", os.environ.get("MODEL_NAME", "gemini-3.5-flash-lite"))
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", os.environ.get("ALLOWED_EMAIL_DOMAINS", "example.com,user@example.com"))
    monkeypatch.setenv("FIREBASE_API_KEY", os.environ.get("FIREBASE_API_KEY", "your_firebase_api_key_here"))
