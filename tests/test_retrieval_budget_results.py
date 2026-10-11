"""Completed, validated material survives the deadline for starting new work."""

from dataclasses import replace
from functools import partial

import pytest

from daily_brief.article_fetcher import (
    CLASSIFICATION_FETCH_POLICY,
    SUMMARY_FETCH_POLICY,
    ArticleFetchError,
    fetch_article,
)
from daily_brief.article_fetcher import responses
from daily_brief.article_fetcher.budget import retrieval_budget
from daily_brief.generation.material import (
    RETRIEVAL_MODE_CLASSIFICATION,
    prepare_candidate_material,
)
from daily_brief.models import Candidate
from fakes import story
from test_article_fetcher import (
    FakeResponse,
    http_error,
    make_jina_payload,
    make_pdf,
    resolver_for,
)
from test_retrieval_budget import Clock


PDF_URL = "https://example.com/report.pdf"
MARKDOWN = "# Report\n\nValidated Adobe report material."


@pytest.fixture
def slow_adobe(monkeypatch):
    clock = Clock()
    calls = []

    def extract(*args, **kwargs):
        calls.append(kwargs)
        clock.advance(150)
        return MARKDOWN

    monkeypatch.setattr(responses, "adobe_credentials_status", lambda environ: "configured")
    monkeypatch.setattr(responses, "_extract_pdf_with_adobe_in_subprocess", extract)
    monkeypatch.setattr(
        responses,
        "_extract_pdf_in_subprocess",
        lambda *args, **kwargs: pytest.fail("Successful Adobe material must be retained"),
    )
    return clock, calls


def pdf_response(request, timeout):
    return FakeResponse(
        make_pdf("Downloaded PDF facts."),
        content_type="application/pdf",
        final_url=PDF_URL,
    )


@pytest.mark.parametrize("policy", [SUMMARY_FETCH_POLICY, CLASSIFICATION_FETCH_POLICY])
def test_adobe_result_after_item_deadline_keeps_full_timeout_and_material(slow_adobe, policy):
    clock, calls = slow_adobe
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return pdf_response(request, timeout)

    with retrieval_budget(timeout_seconds=120, clock=clock):
        result = fetch_article(PDF_URL, opener=opener, resolver=resolver_for({}), policy=policy)
        assert result.text == MARKDOWN
        assert result.extractor == "adobe_pdf_to_markdown"
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(PDF_URL, opener=opener, resolver=resolver_for({}), policy=policy)
        assert caught.value.error_code == "retrieval_budget_exhausted"

    assert requests == [PDF_URL]
    assert calls[0]["timeout_seconds"] == 300
    assert len(calls) == 1


def test_classification_preparation_retains_late_successful_adobe_material(slow_adobe):
    clock, calls = slow_adobe
    candidate = Candidate(story("1", "Research report", url=PDF_URL))
    fetcher = partial(fetch_article, opener=pdf_response, resolver=resolver_for({}))

    with retrieval_budget(timeout_seconds=120, clock=clock):
        assert prepare_candidate_material(
            candidate, fetcher, None, None, None,
            retrieval_mode=RETRIEVAL_MODE_CLASSIFICATION,
        )

    assert candidate.story.fetched_text == MARKDOWN
    assert candidate.article_retrieval.status == "success"
    assert candidate.article_retrieval.extractor == "adobe_pdf_to_markdown"
    assert calls[0]["timeout_seconds"] == 300


@pytest.mark.parametrize("route", ["jina", "github_readme"])
def test_validated_late_transport_result_retained_but_next_request_blocked(route):
    clock = Clock()
    source = "https://example.com/article" if route == "jina" else "https://github.com/example/project"
    requests = []
    content = "Validated complete article material."

    def opener(request, timeout):
        requests.append(request.full_url)
        if route == "jina" and request.full_url == source:
            raise http_error(source, 403)
        clock.advance(150)
        if route == "jina":
            return FakeResponse(
                make_jina_payload(content, url=source),
                content_type="application/json", final_url=request.full_url,
            )
        return FakeResponse(content.encode(), content_type="text/plain", final_url=request.full_url)

    with retrieval_budget(timeout_seconds=120, clock=clock):
        result = fetch_article(source, opener=opener, resolver=resolver_for({}))
        assert result.method == route
        assert result.text == content
        completed_requests = list(requests)
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(source, opener=opener, resolver=resolver_for({}))
        assert caught.value.error_code == "retrieval_budget_exhausted"
        assert requests == completed_requests
    assert len(requests) == (2 if route == "jina" else 1)


def test_late_reader_response_still_rejects_unsuccessful_origin_status():
    clock = Clock()
    source = "https://example.com/article"
    reader = f"https://r.jina.ai/{source}"
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == source:
            raise http_error(source, 403)
        clock.advance(150)
        return FakeResponse(
            make_jina_payload("Access denied page material.", http_status=403, url=source),
            content_type="application/json", final_url=reader,
        )

    with retrieval_budget(timeout_seconds=120, clock=clock):
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(
                source, opener=opener, resolver=resolver_for({}),
                policy=replace(SUMMARY_FETCH_POLICY, wayback_enabled=False),
            )
    assert caught.value.error_code == "jina_origin_status"
    assert caught.value.method == "jina"
    assert requests == [source, reader]
