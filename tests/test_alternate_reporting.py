import hashlib
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

from daily_brief.alternate_reporting import (
    ALTERNATE_REPORTING_HOST_ALLOWLIST,
    MAX_ALTERNATE_REPORTING_CANDIDATES,
    TAVILY_MAX_RESPONSE_BYTES,
    AlternateReportingCandidate,
    AlternateReportingFinderError,
    TavilyAlternateReportingFinder,
    build_tavily_query,
    normalize_allowed_candidate_url,
    validate_alternate_reporting,
    validations_conflict,
)
from daily_brief.models import Candidate, Story

NYTIMES_URL = (
    "https://www.nytimes.com/2026/08/27/technology/"
    "anthropic-government-blacklisting-ruling.html"
)
YAHOO_URL = (
    "https://ca.finance.yahoo.com/news/"
    "us-judge-rules-pentagon-blacklisting-012047911.html"
)
MANIFEST_PATH = (
    Path(__file__).parent
    / "fixtures/alternate_reporting/anthropic_yahoo_manifest.json"
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


def source_candidate(url=NYTIMES_URL, created_at="2026-08-28T02:00:00Z"):
    return Candidate(
        story=Story(
            source="algolia",
            hn_item_id="49473522",
            title=(
                "Judge rules Trump administration’s blacklisting of Anthropic "
                "was illegal"
            ),
            source_url=url,
            hn_discussion_url="https://news.ycombinator.com/item?id=49473522",
            created_at=created_at,
            points=500,
            comments=300,
        )
    )


def alternate_body(*, footer=True, date_text="Aug 28"):
    beginning = (
        f"{date_text} (Reuters) - A U.S. judge ruled that the Pentagon's "
        "blacklisting of Anthropic was unlawful. The court found the government "
        "action violated the First Amendment as illegal retaliation and denied "
        "the company required Fifth Amendment process. "
    )
    ending = "(Reporting by Christian Martinez and Jasper Ward)" if footer else ""
    filler_length = 644 - len(beginning) - len(ending)
    assert filler_length >= 0
    return beginning + ("x" * filler_length) + ending


def test_query_uses_clean_event_anchors_without_exact_phrase():
    assert build_tavily_query(source_candidate()) == (
        "anthropic government blacklisting Reuters"
    )


def test_tavily_finder_is_independent_and_discards_provider_content():
    requests = []
    response = {
        "answer": "must not be used",
        "results": [
            {
                "title": "Pentagon blacklisting of Anthropic was unlawful",
                "url": YAHOO_URL,
                "content": "must not be used",
                "raw_content": "must not be used",
            }
        ],
    }

    def open_response(request, timeout):
        requests.append((request, timeout))
        return FakeResponse(json.dumps(response).encode())

    results = TavilyAlternateReportingFinder(
        api_key="test-key", opener=open_response
    ).find(source_candidate())

    assert results == [
        AlternateReportingCandidate(
            title="Pentagon blacklisting of Anthropic was unlawful",
            url=YAHOO_URL,
        )
    ]
    request, timeout = requests[0]
    payload = json.loads(request.data)
    assert timeout == 10
    assert payload["query"] == "anthropic government blacklisting Reuters"
    assert payload["include_domains"] == sorted(
        ALTERNATE_REPORTING_HOST_ALLOWLIST
    )
    assert payload["exact_match"] is False
    assert payload["include_answer"] is False
    assert payload["include_raw_content"] is False
    assert not hasattr(results[0], "content")


def test_tavily_finder_fails_closed_for_missing_key_and_oversized_response():
    with pytest.raises(AlternateReportingFinderError) as missing:
        TavilyAlternateReportingFinder(api_key="").find(source_candidate())
    assert missing.value.error_code == "not_configured"

    finder = TavilyAlternateReportingFinder(
        api_key="test-key",
        opener=lambda request, timeout: FakeResponse(
            b"x" * (TAVILY_MAX_RESPONSE_BYTES + 1)
        ),
    )
    with pytest.raises(AlternateReportingFinderError) as oversized:
        finder.find(source_candidate())
    assert oversized.value.error_code == "response_too_large"


def test_tavily_finder_bounds_results():
    response = {
        "results": [
            {"title": f"Result {index}", "url": f"{YAHOO_URL}?r={index}"}
            for index in range(MAX_ALTERNATE_REPORTING_CANDIDATES + 2)
        ]
    }
    finder = TavilyAlternateReportingFinder(
        api_key="test-key",
        opener=lambda request, timeout: FakeResponse(json.dumps(response).encode()),
    )
    assert len(finder.find(source_candidate())) == MAX_ALTERNATE_REPORTING_CANDIDATES


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/article",
        "file:///tmp/article",
        "https://user:pass@finance.yahoo.com/article",
        "https://finance.yahoo.com:8443/article",
        "https://www.reuters.com.evil.example/article",
        "not a url",
    ],
)
def test_candidate_url_filter_rejects_unsafe_or_non_allowlisted_urls(url):
    assert normalize_allowed_candidate_url(url) is None


def test_validator_accepts_production_metadata_and_synthetic_complete_copy():
    body = alternate_body()
    assert len(body) == 644

    result = validate_alternate_reporting(
        source_candidate(),
        AlternateReportingCandidate(
            "US judge rules Pentagon blacklisting of Anthropic unlawful",
            YAHOO_URL,
        ),
        body,
    )

    assert result.accepted is True
    assert result.reason == "verified"
    assert result.reporting_date.isoformat() == "2026-08-28"
    assert {"anthropic", "blacklist", "government", "judge"}.issubset(
        result.matched_anchors
    )


def test_real_body_manifest_replays_body_only_validation_when_fixture_exists():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["fixture_id"] == "hn-49473522-yahoo-reuters"
    assert manifest["body"]["chars"] == 644
    assert len(manifest["body"]["sha256"]) == 64
    assert manifest["expected_validation"]["accepted"] is True
    assert "body_only_same_event_signals" in manifest["required_evidence"]

    project_root = Path(__file__).parents[1]
    body_path = project_root / manifest["body_path"]
    if not body_path.exists():
        pytest.skip("ignored real-body fixture is not present in this checkout")

    body = body_path.read_text(encoding="utf-8").strip()
    assert len(body) == manifest["body"]["chars"]
    assert hashlib.sha256(body.encode()).hexdigest() == manifest["body"]["sha256"]

    source = manifest["source"]
    candidate = source_candidate(
        url=source["url"],
        created_at=source["created_at"],
    )
    candidate.story = replace(
        candidate.story,
        hn_item_id=source["hn_item_id"],
        title=source["title"],
    )
    alternate = manifest["alternate"]
    result = validate_alternate_reporting(
        candidate,
        AlternateReportingCandidate(
            title="Unrelated result title",
            url=alternate["url"],
        ),
        body,
    )
    expected = manifest["expected_validation"]
    assert result.accepted is expected["accepted"]
    assert result.reason == expected["reason"]
    assert result.reporting_date.isoformat() == expected["reporting_date"]
    assert list(result.matched_anchors) == expected["matched_anchors"]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("Aug 28 (Reuters) - short (Reporting by A B)", "body_too_short"),
        (
            alternate_body().replace("(Reuters)", "(News service)"),
            "missing_reuters_marker",
        ),
        (alternate_body(footer=False), "missing_reporting_footer"),
        (
            alternate_body().replace("x" * 30, "Read the full article" + "x" * 9, 1),
            "teaser_content",
        ),
        (alternate_body(date_text="March 25"), "date_mismatch"),
    ],
)
def test_validator_rejects_incomplete_or_wrong_date_material(body, reason):
    result = validate_alternate_reporting(
        source_candidate(),
        AlternateReportingCandidate("Candidate", YAHOO_URL),
        body,
    )
    assert result.accepted is False
    assert result.reason == reason


def test_validator_rejects_same_day_unrelated_anthropic_matx_report():
    beginning = (
        "Aug 28 (Reuters) - Anthropic is in talks with chip startup MatX to "
        "accelerate chip design, according to people familiar with the matter. "
    )
    body = beginning + ("Unrelated semiconductor reporting. " * 12) + (
        "(Reporting by Example Reporter)"
    )
    result = validate_alternate_reporting(
        source_candidate(),
        AlternateReportingCandidate("Anthropic in talks with MatX", YAHOO_URL),
        body,
    )
    assert result.accepted is False
    assert result.reason == "insufficient_event_signals"


def test_search_result_title_cannot_make_unrelated_body_pass():
    beginning = (
        "Aug 28 (Reuters) - Anthropic is in talks with chip startup MatX to "
        "accelerate chip design, according to people familiar with the matter. "
    )
    body = beginning + ("Unrelated semiconductor reporting. " * 12) + (
        "(Reporting by Example Reporter)"
    )
    result = validate_alternate_reporting(
        source_candidate(),
        AlternateReportingCandidate(
            "Pentagon's blacklisting of Anthropic was unlawful, US judge rules",
            YAHOO_URL,
        ),
        body,
    )
    assert result.accepted is False
    assert result.reason == "insufficient_event_signals"


def test_validator_requires_distinctive_magnitude_signals():
    candidate = source_candidate()
    candidate.story = replace(
        candidate.story,
        title="Judge rules $25 billion Anthropic blacklisting illegal",
        source_url=(
            "https://www.nytimes.com/2026/08/27/technology/"
            "anthropic-25-billion-blacklisting-ruling.html"
        ),
    )
    result = validate_alternate_reporting(
        candidate,
        AlternateReportingCandidate("Anthropic blacklisting ruling", YAHOO_URL),
        alternate_body(),
    )
    assert result.accepted is False
    assert result.reason == "insufficient_event_signals"


def test_validator_does_not_require_bare_source_numbers():
    candidate = source_candidate()
    candidate.story = replace(
        candidate.story,
        title=(
            "Judge rules on 3 counts in illegal Anthropic blacklisting case"
        ),
    )
    result = validate_alternate_reporting(
        candidate,
        AlternateReportingCandidate("Untrusted search result title", YAHOO_URL),
        alternate_body(),
    )
    assert result.accepted is True


def test_validator_uses_created_at_when_source_url_has_no_date():
    result = validate_alternate_reporting(
        source_candidate(
            url="https://example.com/anthropic-government-blacklisting",
            created_at="2026-08-28T02:00:00Z",
        ),
        AlternateReportingCandidate("Anthropic blacklisting ruling", YAHOO_URL),
        alternate_body(),
    )
    assert result.accepted is True


def test_conflicting_verified_event_identities_fail_closed():
    first = validate_alternate_reporting(
        source_candidate(),
        AlternateReportingCandidate("Anthropic blacklisting ruling", YAHOO_URL),
        alternate_body(),
    )
    conflict = type(first)(
        True,
        "verified",
        reporting_date=first.reporting_date,
        matched_anchors=("unrelated", "signals", "only"),
    )
    assert validations_conflict([first, conflict]) is True
