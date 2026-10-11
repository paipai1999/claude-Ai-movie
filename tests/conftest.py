"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _hermetic_gemini_key(monkeypatch):
    """
    Several engine tests stub ``call_gemini`` but still need *a* key to be configured before the
    translation path runs. They used to pass only because a real key happened to live in the
    developer's ``config.json`` (and failed on CI / after moving keys to the environment).
    Supply a dummy key unless a test sets its own; no network call is ever made with it.
    """
    import os

    if not os.environ.get("GEMINI_API_KEYS") and not os.environ.get("GEMINI_API_KEY"):
        monkeypatch.setenv("GEMINI_API_KEYS", "dummy-key-for-tests")
    yield
