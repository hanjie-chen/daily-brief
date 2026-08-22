import json
from io import BytesIO

import pytest

from daily_brief.models import Candidate, Story
from daily_brief.syndicated_copy import (
    MAX_SYNDICATED_CANDIDATES,
    TAVILY_MAX_RESPONSE_BYTES,
    SyndicatedCandidate,
    SyndicatedFinderError,
    TavilySyndicatedCopyFinder,
    build_tavily_query,
    normalize_allowed_candidate_url,
    validate_syndicated_copy,
)

REUTERS_URL = (
    "https://www.reuters.com/business/"
    "nvidia-scales-back-250-billion-openai-data-center-guarantee-"
    "wsj-reports-2026-08-14/"
)
YAHOO_URL = (
    "https://finance.yahoo.com/technology/ai/articles/"
    "nvidia-scales-back-250-billion-234356524.html"
)


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self.body.read(size)


def source_candidate() -> Candidate:
    return Candidate(
        story=Story(
            source="algolia",
            hn_item_id="49323686",
            title=(
                "Nvidia dramatically reduces amount of OpenAI infra financing "
                "it may guarantee"
            ),
            source_url=REUTERS_URL,
            hn_discussion_url=("https://news.ycombinator.com/item?id=49323686"),
            created_at="2026-08-16T00:00:00Z",
            points=100,
            comments=20,
        )
    )


def syndicated_body() -> str:
    facts = (
        "Aug 14 (Reuters) - Nvidia has scaled back the amount of financing it "
        "may guarantee for OpenAI's Ohio data center project from a previously "
        "discussed $250 billion to less than $120 billion. Investors have been "
        "concerned about Nvidia's exposure, while OpenAI discussed leases for "
        "the complete 10GW project. "
    )
    return facts + ("Additional grounded reporting detail. " * 20)


def test_build_tavily_query_uses_reuters_slug_not_rewritten_hn_title():
    query = build_tavily_query(source_candidate())

    assert query == '"nvidia scales back" "250 billion" OpenAI Reuters'


def test_tavily_adapter_sends_bounded_discovery_request_and_ignores_content():
    requests = []
    response = {
        "answer": "must not be used",
        "results": [
            {
                "title": "Nvidia scales back funding guarantee",
                "url": YAHOO_URL,
                "content": "must not be used",
                "raw_content": "must not be used",
            }
        ],
    }

    def open_response(request, timeout):
        requests.append((request, timeout))
        return FakeResponse(json.dumps(response).encode("utf-8"))

    finder = TavilySyndicatedCopyFinder(
        api_key="test-key",
        opener=open_response,
    )
    results = finder.find(source_candidate())

    assert results == [
        SyndicatedCandidate(
            title="Nvidia scales back funding guarantee",
            url=YAHOO_URL,
        )
    ]
    request, timeout = requests[0]
    assert request.full_url == "https://api.tavily.com/search"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert timeout == 10
    payload = json.loads(request.data)
    assert payload == {
        "query": '"nvidia scales back" "250 billion" OpenAI Reuters',
        "search_depth": "basic",
        "topic": "general",
        "max_results": MAX_SYNDICATED_CANDIDATES,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "include_domains": ["finance.yahoo.com"],
        "auto_parameters": False,
        "exact_match": True,
    }
    assert not hasattr(results[0], "content")


def test_tavily_adapter_fails_closed_without_key_or_valid_json():
    with pytest.raises(SyndicatedFinderError) as missing_key:
        TavilySyndicatedCopyFinder(api_key="").find(source_candidate())
    assert missing_key.value.error_code == "not_configured"

    finder = TavilySyndicatedCopyFinder(
        api_key="test-key",
        opener=lambda request, timeout: FakeResponse(b"not-json"),
    )
    with pytest.raises(SyndicatedFinderError) as malformed:
        finder.find(source_candidate())
    assert malformed.value.error_code == "malformed_response"


def test_tavily_adapter_limits_results_and_rejects_oversized_response():
    results = [
        {"title": f"Result {index}", "url": f"{YAHOO_URL}?result={index}"}
        for index in range(5)
    ]
    finder = TavilySyndicatedCopyFinder(
        api_key="test-key",
        opener=lambda request, timeout: FakeResponse(
            json.dumps({"results": results}).encode("utf-8")
        ),
    )

    assert len(finder.find(source_candidate())) == MAX_SYNDICATED_CANDIDATES

    oversized = TavilySyndicatedCopyFinder(
        api_key="test-key",
        opener=lambda request, timeout: FakeResponse(
            b"x" * (TAVILY_MAX_RESPONSE_BYTES + 1)
        ),
    )
    with pytest.raises(SyndicatedFinderError) as caught:
        oversized.find(source_candidate())
    assert caught.value.error_code == "response_too_large"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/article",
        "file:///tmp/article",
        "https://user:pass@finance.yahoo.com/article",
        "https://finance.yahoo.com:8443/article",
        "https://finance.yahoo.com.evil.example/article",
        "not a url",
    ],
)
def test_candidate_url_filter_rejects_unsafe_or_non_allowlisted_urls(url):
    assert normalize_allowed_candidate_url(url) is None


def test_candidate_url_filter_normalizes_fragment_on_allowlisted_url():
    assert normalize_allowed_candidate_url(f"{YAHOO_URL}#comments") == YAHOO_URL


def test_validator_accepts_rewritten_hn_title_using_combined_signals():
    result = validate_syndicated_copy(
        source_candidate(),
        SyndicatedCandidate(
            title=(
                "Nvidia scales back funding guarantee for Ohio OpenAI data "
                "center, WSJ reports"
            ),
            url=YAHOO_URL,
        ),
        syndicated_body(),
    )

    assert result.accepted is True
    assert result.reason == "verified"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("Aug 14 (Reuters) - Read the full article", "body_too_short"),
        (
            "Aug 14 (Reuters) - Read the full article. " + "x" * 900,
            "teaser_content",
        ),
        (
            syndicated_body().replace("(Reuters)", "(News service)"),
            "missing_reuters_marker",
        ),
        (syndicated_body().replace("Aug 14", "July 1"), "date_mismatch"),
        (
            "Aug 14 (Reuters) - An unrelated company announced an unrelated "
            "product. " + "Unrelated reporting detail. " * 35,
            "insufficient_story_signals",
        ),
    ],
)
def test_validator_rejects_inadequate_material(body, reason):
    result = validate_syndicated_copy(
        source_candidate(),
        SyndicatedCandidate(title="Unrelated story", url=YAHOO_URL),
        body,
    )

    assert result.accepted is False
    assert result.reason == reason
