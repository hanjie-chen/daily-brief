import pytest


@pytest.fixture(autouse=True)
def clear_jina_api_key(monkeypatch):
    """Keep optional authentication deterministic regardless of the shell environment."""
    monkeypatch.delenv('JINA_API_KEY', raising=False)
