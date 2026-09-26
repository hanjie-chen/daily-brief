import pytest

from daily_brief import cli
from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import material, pipeline
from fakes import FakeGeminiBackendFactory


@pytest.fixture(autouse=True)
def prevent_live_classifier_and_article_calls(monkeypatch):
    monkeypatch.setattr(cli, "GeminiBackend", FakeGeminiBackendFactory)
    monkeypatch.setattr(pipeline, "GeminiBackend", FakeGeminiBackendFactory)
    monkeypatch.setattr(
        material,
        "fetch_article",
        lambda url, **kwargs: "Test article facts.",
    )
    monkeypatch.setattr(
        material,
        "fetch_hn_discussion",
        lambda item_id: HNDiscussionResult(
            text="",
            comments=0,
            chars=0,
            requested_items=1,
            failed_items=0,
        ),
    )
