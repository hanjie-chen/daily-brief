import pytest


@pytest.fixture(autouse=True)
def clear_jina_api_key(monkeypatch):
    """Keep optional authentication deterministic regardless of the shell environment."""
    monkeypatch.delenv('JINA_API_KEY', raising=False)


@pytest.fixture(autouse=True)
def forbid_live_connectivity_check(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must inject the HN connectivity check or its transport")

    monkeypatch.setattr("daily_brief.network_recovery.urlopen", forbidden)
