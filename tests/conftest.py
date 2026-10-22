import pytest


@pytest.fixture(autouse=True)
def prevent_live_default_content_deduplicator(monkeypatch):
    from daily_brief.generation import content_deduplication
    from fakes import FakeContentDeduplicator

    monkeypatch.setattr(content_deduplication, "create_model_backend", FakeContentDeduplicator)


@pytest.fixture(autouse=True)
def clear_jina_api_key(monkeypatch):
    """Keep optional authentication deterministic regardless of the shell environment."""
    monkeypatch.delenv('JINA_API_KEY', raising=False)


@pytest.fixture(autouse=True)
def clear_github_token(monkeypatch):
    """GitHub authentication tests opt in using a synthetic credential."""
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)


@pytest.fixture(autouse=True)
def forbid_live_connectivity_check(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must inject the HN connectivity check or its transport")

    monkeypatch.setattr("daily_brief.network_recovery.urlopen", forbidden)
