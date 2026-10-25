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


@pytest.fixture(autouse=True)
def _no_tts_network(monkeypatch):
    """
    The voice step probes Microsoft's speech service once before it starts (agents/voice_agent.py). Tests never go to the
    network: the probe is off by default here, and tests/test_tts_guard.py turns it on against a fake service.
    """
    monkeypatch.setenv("PAI_SKIP_TTS_PREFLIGHT", "1")
    # Edge TTS being down must not make a test reach for Gemini with its dummy key; tests/test_gemini_tts.py turns it on.
    monkeypatch.setenv("PAI_TTS_FALLBACK", "off")
    monkeypatch.setenv("GEMINI_TTS_MODEL", "test-tts-model")
    yield

@pytest.fixture(autouse=True)
def _no_usage_statistics(monkeypatch):
    """
    Every Gemini call records which key / model answered in the project's own ``outputs/movie_metadata.db`` (the
    dashboard's key-usage view). Tests use made-up keys ("k1", "secret-key") and must never write to the real file:
    they showed up there as keys of the user. Tests of the tracker itself use their own database.
    """
    import brain.gemini_client as gemini_client

    monkeypatch.setattr(gemini_client, "_record_api_usage", lambda *args, **kwargs: None)
    yield
