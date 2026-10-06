import pytest

from daily_brief.llm import factory as backend_factory
from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import material
from fakes import FakeGeminiBackendFactory


@pytest.fixture(autouse=True)
def prevent_live_classifier_and_article_calls(monkeypatch):
    monkeypatch.delenv("DAILY_BRIEF_MODEL_BACKEND", raising=False)
    monkeypatch.setattr(backend_factory, "GeminiBackend", FakeGeminiBackendFactory)
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
