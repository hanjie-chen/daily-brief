import json

import pytest

from daily_brief.article_fetcher import ArticleFetchError, ArticleFetchResult
from daily_brief.generation import run_generate
from daily_brief.models import Story
from daily_brief.recovery import AlternateReportingCandidate, SyndicatedCandidate
from fakes import (
    CapturingSummarizer,
    FakeAlternateReportingFinder,
    FakeClassifier,
    FakeSummarizer,
    FakeSyndicatedFinder,
    RaisingSummarizer,
    RaisingSyndicatedFinder,
    alternate_reporting_body,
    anthropic_nytimes_story,
    datadome_jina_failure,
    nytimes_anthropic_url,
    origin_block_failure,
    reuters_story_url,
    story,
    verified_reuters_copy_body,
    yahoo_anthropic_url,
    yahoo_story_url,
)


def test_classification_retrieval_does_not_attempt_reuters_recovery(tmp_path):
    finder = FakeSyndicatedFinder(
        [SyndicatedCandidate(title="copy", url=yahoo_story_url())]
    )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story(
                "1",
                "A market structure update",
                points=500,
                comments=20,
                url=reuters_story_url(),
            )
        ],
        hot_stories=[],
        classifier=FakeClassifier(),
        article_fetcher=lambda url, **kwargs: (_ for _ in ()).throw(datadome_jina_failure()),
        syndicated_finder=finder,
        summarizer=FakeSummarizer(),
    )

    records = json.loads(result.data_path.read_text(encoding="utf-8"))
    assert finder.calls == []
    assert records[0]["topic_route"] == "topic_unknown"
    assert records[0]["article_retrieval"]["syndicated_recovery"]["status"] == (
        "not_attempted"
    )


def test_reuters_datadome_failure_recovers_verified_yahoo_copy(tmp_path):
    reuters_url = reuters_story_url()
    yahoo_url = yahoo_story_url()
    fetched_urls = []
    finder = FakeSyndicatedFinder(
        [
            SyndicatedCandidate(
                title=(
                    "Nvidia scales back funding guarantee for Ohio OpenAI data "
                    "center, WSJ reports"
                ),
                url=yahoo_url,
            )
        ]
    )
    summarizer = CapturingSummarizer()

    def fetch(url, **kwargs):
        fetched_urls.append(url)
        if url == reuters_url:
            raise datadome_jina_failure(attempts=2)
        return ArticleFetchResult(
            text=verified_reuters_copy_body(),
            method="direct",
            extractor="trafilatura",
            attempts=2,
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia dramatically reduces amount of OpenAI infra financing it may guarantee",
                points=40,
                comments=8,
                url=reuters_url,
            )
        ],
        hot_stories=[],
        article_fetcher=fetch,
        syndicated_finder=finder,
        summarizer=summarizer,
    )

    assert finder.calls == ["49323686"]
    assert fetched_urls == [reuters_url, yahoo_url]
    assert summarizer.fetched_texts == [verified_reuters_copy_body().strip()]
    payload = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    retrieval = payload["article_retrieval"]
    assert retrieval["status"] == "success"
    assert retrieval["retrieved_url"] == yahoo_url
    assert retrieval["material_origin"] == "syndicated_copy"
    assert retrieval["method"] == "direct"
    assert retrieval["extractor"] == "trafilatura"
    assert retrieval["attempts"] == 2
    assert retrieval["origin_failure"] == {
        "method": "jina",
        "extractor": "jina",
        "attempts": 2,
        "fallback_attempted": True,
        "fallback_reason": "datadome_challenge",
        "error_type": "ArticleFetchError",
        "error_code": "http_403",
        "error_message": "Reuters blocked; Jina failed",
    }
    assert retrieval["syndicated_recovery"] == {
        "status": "success",
        "provider": "fake",
        "discovered_candidates": 1,
        "attempted_candidates": 1,
        "rejection_reasons": [],
        "error_code": "",
    }
    public_payload = json.loads(result.public_json_path.read_text(encoding="utf-8"))
    public_item = public_payload["sections"]["ai"]["items"][0]
    assert public_item["source_url"] == reuters_url
    assert public_item["content_status"] == "ok"


def test_reuters_datadome_wayback_failure_still_recovers_yahoo_copy(tmp_path):
    reuters_url = reuters_story_url()
    yahoo_url = yahoo_story_url()
    fetched_urls = []
    finder = FakeSyndicatedFinder(
        [
            SyndicatedCandidate(
                title=(
                    "Nvidia scales back funding guarantee for Ohio OpenAI data "
                    "center, WSJ reports"
                ),
                url=yahoo_url,
            )
        ]
    )

    def fetch(url, **kwargs):
        fetched_urls.append(url)
        if url == reuters_url:
            raise origin_block_failure("datadome_challenge", method="wayback")
        return ArticleFetchResult(
            text=verified_reuters_copy_body(),
            method="direct",
            extractor="trafilatura",
            attempts=2,
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia dramatically reduces amount of OpenAI infra financing it may guarantee",
                points=40,
                comments=8,
                url=reuters_url,
            )
        ],
        hot_stories=[],
        article_fetcher=fetch,
        syndicated_finder=finder,
        summarizer=FakeSummarizer(),
    )

    assert finder.calls == ["49323686"]
    assert fetched_urls == [reuters_url, yahoo_url]
    payload = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    retrieval = payload["article_retrieval"]
    assert retrieval["material_origin"] == "syndicated_copy"
    assert retrieval["origin_failure"]["method"] == "wayback"
    assert retrieval["origin_failure"]["fallback_reason"] == "datadome_challenge"


@pytest.mark.parametrize(
    ("url_kind", "failure_kind"),
    [
        ("non_reuters", "datadome"),
        ("reuters", "not_found"),
    ],
)
def test_syndicated_finder_is_not_called_outside_narrow_reuters_route(
    tmp_path, url_kind, failure_kind
):
    url = (
        "https://example.com/article"
        if url_kind == "non_reuters"
        else reuters_story_url()
    )
    failure = (
        datadome_jina_failure()
        if failure_kind == "datadome"
        else ArticleFetchError(
            "not found",
            error_code="http_404",
            method="direct",
        )
    )
    finder = FakeSyndicatedFinder([])

    def fail_fetch(requested_url, **kwargs):
        raise failure

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "1",
                "OpenAI infrastructure report",
                points=40,
                comments=8,
                url=url,
            )
        ],
        hot_stories=[],
        article_fetcher=fail_fetch,
        syndicated_finder=finder,
        summarizer=FakeSummarizer(),
    )

    assert finder.calls == []


def test_syndicated_recovery_filters_urls_and_continues_after_fetch_failure(
    tmp_path,
):
    reuters_url = reuters_story_url()
    first_yahoo = "https://finance.yahoo.com/articles/first.html"
    second_yahoo = yahoo_story_url()
    finder = FakeSyndicatedFinder(
        [
            SyndicatedCandidate("Untrusted", "https://evil.example/article"),
            SyndicatedCandidate("First Yahoo copy", first_yahoo),
            SyndicatedCandidate("Verified Yahoo copy", second_yahoo),
            SyndicatedCandidate("Over the limit", "https://finance.yahoo.com/late"),
        ]
    )
    fetched_urls = []

    def fetch(url, **kwargs):
        fetched_urls.append(url)
        if url == reuters_url:
            raise datadome_jina_failure()
        if url == first_yahoo:
            raise ArticleFetchError("Yahoo failed", error_code="http_403")
        return ArticleFetchResult(
            verified_reuters_copy_body(),
            method="direct",
            extractor="trafilatura",
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia and OpenAI financing guarantee",
                points=40,
                comments=8,
                url=reuters_url,
            )
        ],
        hot_stories=[],
        article_fetcher=fetch,
        syndicated_finder=finder,
        summarizer=FakeSummarizer(),
    )

    assert fetched_urls == [reuters_url, first_yahoo, second_yahoo]
    recovery = json.loads(result.data_path.read_text(encoding="utf-8"))[0][
        "article_retrieval"
    ]["syndicated_recovery"]
    assert recovery["status"] == "success"
    assert recovery["discovered_candidates"] == 4
    assert recovery["attempted_candidates"] == 2
    assert recovery["rejection_reasons"] == ["unsupported_url", "fetch_failed"]


def test_syndicated_recovery_rejects_cross_host_redirect(tmp_path):
    reuters_url = reuters_story_url()
    yahoo_url = yahoo_story_url()
    summarizer = FakeSummarizer()

    def fetch(url, **kwargs):
        if url == reuters_url:
            raise datadome_jina_failure()
        return ArticleFetchResult(
            verified_reuters_copy_body(),
            method="direct",
            extractor="trafilatura",
            retrieved_url="https://evil.example/reuters-copy",
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia and OpenAI financing guarantee",
                points=40,
                comments=8,
                url=reuters_url,
            )
        ],
        hot_stories=[],
        article_fetcher=fetch,
        syndicated_finder=FakeSyndicatedFinder(
            [SyndicatedCandidate("Yahoo copy", yahoo_url)]
        ),
        summarizer=summarizer,
    )

    payload = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    recovery = payload["article_retrieval"]["syndicated_recovery"]
    assert recovery["status"] == "exhausted"
    assert recovery["rejection_reasons"] == ["redirected_to_unsupported_url"]
    assert payload["summary_status"] == "skipped"
    assert summarizer.titles == []


def test_failed_syndicated_candidates_preserve_original_block_and_do_not_recurse(
    tmp_path,
):
    reuters_url = reuters_story_url()
    yahoo_url = yahoo_story_url()
    finder = FakeSyndicatedFinder([SyndicatedCandidate("Yahoo copy", yahoo_url)])

    def fetch(url, **kwargs):
        if url == reuters_url:
            raise datadome_jina_failure(attempts=2)
        raise datadome_jina_failure(attempts=3)

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia and OpenAI financing guarantee",
                points=40,
                comments=8,
                url=reuters_url,
            )
        ],
        hot_stories=[],
        article_fetcher=fetch,
        syndicated_finder=finder,
        summarizer=FakeSummarizer(),
    )

    assert finder.calls == ["49323686"]
    retrieval = json.loads(result.data_path.read_text(encoding="utf-8"))[0][
        "article_retrieval"
    ]
    assert retrieval["status"] == "failed"
    assert retrieval["attempts"] == 2
    assert retrieval["fallback_reason"] == "datadome_challenge"
    assert retrieval["origin_failure"] is None
    assert retrieval["syndicated_recovery"]["status"] == "exhausted"
    assert retrieval["syndicated_recovery"]["rejection_reasons"] == ["fetch_failed"]
    assert "来源网站阻止自动抓取" in result.brief_path.read_text(encoding="utf-8")


def test_syndicated_recovery_deduplicates_candidates_before_fetch(tmp_path):
    reuters_url = reuters_story_url()
    yahoo_url = yahoo_story_url()
    finder = FakeSyndicatedFinder(
        [
            SyndicatedCandidate("First", f"{yahoo_url}#first"),
            SyndicatedCandidate("Duplicate", f"{yahoo_url}#second"),
        ]
    )
    fetched_urls = []

    def fetch(url, **kwargs):
        fetched_urls.append(url)
        if url == reuters_url:
            raise datadome_jina_failure()
        raise ArticleFetchError("Yahoo failed", error_code="http_403")

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia and OpenAI financing guarantee",
                points=40,
                comments=8,
                url=reuters_url,
            )
        ],
        hot_stories=[],
        article_fetcher=fetch,
        syndicated_finder=finder,
        summarizer=FakeSummarizer(),
    )

    assert fetched_urls == [reuters_url, yahoo_url]
    recovery = json.loads(result.data_path.read_text(encoding="utf-8"))[0][
        "article_retrieval"
    ]["syndicated_recovery"]
    assert recovery["attempted_candidates"] == 1
    assert recovery["rejection_reasons"] == ["fetch_failed", "duplicate_url"]


def test_short_teaser_is_rejected_without_running_summarizer(tmp_path):
    summarizer = FakeSummarizer()

    def fetch(url, **kwargs):
        if url == reuters_story_url():
            raise datadome_jina_failure()
        return ArticleFetchResult(
            "Read the full article. Get unlimited access.",
            method="direct",
            extractor="trafilatura",
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia and OpenAI financing guarantee",
                points=40,
                comments=8,
                url=reuters_story_url(),
            )
        ],
        hot_stories=[],
        article_fetcher=fetch,
        syndicated_finder=FakeSyndicatedFinder(
            [SyndicatedCandidate("Teaser", yahoo_story_url())]
        ),
        summarizer=summarizer,
    )

    payload = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    assert payload["article_retrieval"]["syndicated_recovery"]["rejection_reasons"] == [
        "body_too_short"
    ]
    assert payload["summary_status"] == "skipped"
    assert summarizer.titles == []


def test_syndicated_finder_failure_is_audited_without_failing_generation(tmp_path):
    finder = RaisingSyndicatedFinder()
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia and OpenAI financing guarantee",
                points=40,
                comments=8,
                url=reuters_story_url(),
            )
        ],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: (_ for _ in ()).throw(datadome_jina_failure()),
        syndicated_finder=finder,
        summarizer=FakeSummarizer(),
    )

    recovery = json.loads(result.data_path.read_text(encoding="utf-8"))[0][
        "article_retrieval"
    ]["syndicated_recovery"]
    assert recovery["status"] == "finder_failed"
    assert recovery["error_code"] == "provider_request_failed"
    assert result.brief_path.exists()


def test_missing_tavily_key_fails_closed_without_live_search(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-18",
        algolia_stories=[
            story(
                "49323686",
                "Nvidia and OpenAI financing guarantee",
                points=40,
                comments=8,
                url=reuters_story_url(),
            )
        ],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: (_ for _ in ()).throw(datadome_jina_failure()),
        summarizer=FakeSummarizer(),
    )

    retrieval = json.loads(result.data_path.read_text(encoding="utf-8"))[0][
        "article_retrieval"
    ]
    assert retrieval["status"] == "failed"
    assert retrieval["fallback_reason"] == "datadome_challenge"
    assert retrieval["syndicated_recovery"]["status"] == "finder_failed"
    assert retrieval["syndicated_recovery"]["error_code"] == "not_configured"


def test_origin_block_recovers_verified_alternate_reporting_with_prefix(tmp_path):
    original_url = nytimes_anthropic_url()
    alternate_url = yahoo_anthropic_url()
    finder = FakeAlternateReportingFinder(
        [
            AlternateReportingCandidate(
                "US judge rules Pentagon blacklisting of Anthropic unlawful",
                alternate_url,
            )
        ]
    )

    def fetch(url, **kwargs):
        if url == original_url:
            raise origin_block_failure("datadome_challenge", method="jina")
        return ArticleFetchResult(
            alternate_reporting_body(),
            method="direct",
            extractor="trafilatura",
            retrieved_url=alternate_url,
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-29",
        algolia_stories=[anthropic_nytimes_story()],
        hot_stories=[],
        article_fetcher=fetch,
        alternate_reporting_finder=finder,
        summarizer=FakeSummarizer(),
    )

    assert finder.calls == ["49473522"]
    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    retrieval = candidate_payload["article_retrieval"]
    assert retrieval["status"] == "success"
    assert retrieval["retrieved_url"] == alternate_url
    assert retrieval["material_origin"] == "alternate_reporting"
    assert retrieval["origin_failure"]["fallback_reason"] == "datadome_challenge"
    assert retrieval["alternate_reporting_recovery"] == {
        "status": "success",
        "provider": "fake-alternate",
        "discovered_candidates": 1,
        "attempted_candidates": 1,
        "rejection_reasons": [],
        "error_code": "",
    }
    assert candidate_payload["summary_basis"] == "fetched_article"
    assert candidate_payload["summary_status"] == "success"

    public_payload = json.loads(result.public_json_path.read_text(encoding="utf-8"))
    public_item = public_payload["sections"]["ai"]["items"][0]
    assert public_payload["schema_version"] == 2
    assert public_item["summary"].startswith(
        "据 Reuters 对同一事件的报道：Summary for"
    )
    assert public_item["source_url"] == original_url
    assert public_item["content_status"] == "ok"


@pytest.mark.parametrize(
    ("fallback_reason", "method"),
    [
        ("challenge_page", "wayback"),
        ("cloudflare_challenge", "wayback"),
        ("datadome_challenge", "wayback"),
        ("vercel_challenge", "wayback"),
    ],
)
def test_alternate_reporting_trigger_uses_shared_origin_block_contract(
    tmp_path, fallback_reason, method
):
    finder = FakeAlternateReportingFinder([])

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-29",
        algolia_stories=[anthropic_nytimes_story()],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: (_ for _ in ()).throw(
            origin_block_failure(fallback_reason, method=method)
        ),
        alternate_reporting_finder=finder,
        summarizer=FakeSummarizer(),
    )

    assert finder.calls == ["49473522"]


@pytest.mark.parametrize(
    "failure",
    [
        ArticleFetchError("not found", error_code="http_404", method="direct"),
        ArticleFetchError(
            "empty",
            error_code="empty_content",
            method="jina",
            fallback_attempted=True,
            fallback_reason="empty_content",
        ),
        ArticleFetchError(
            "unconfirmed block",
            error_code="http_403",
            method="jina",
            fallback_attempted=False,
            fallback_reason="datadome_challenge",
        ),
    ],
)
def test_alternate_reporting_finder_is_not_called_for_other_failures(
    tmp_path, failure
):
    finder = FakeAlternateReportingFinder([])
    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-29",
        algolia_stories=[anthropic_nytimes_story()],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: (_ for _ in ()).throw(failure),
        alternate_reporting_finder=finder,
        summarizer=FakeSummarizer(),
    )
    assert finder.calls == []


def test_alternate_reporting_prefers_yahoo_and_selects_longest_verified_body(
    tmp_path,
):
    original_url = nytimes_anthropic_url()
    shorter_url = "https://finance.yahoo.com/news/anthropic-ruling-short.html"
    tied_long_url = "https://finance.yahoo.com/news/z-anthropic-ruling-long.html"
    longer_url = yahoo_anthropic_url()
    reuters_url = (
        "https://www.reuters.com/legal/government/"
        "us-judge-blocks-pentagons-anthropic-blacklisting-2026-08-28/"
    )
    finder = FakeAlternateReportingFinder(
        [
            AlternateReportingCandidate("Reuters candidate", reuters_url),
            AlternateReportingCandidate("Anthropic blacklisting ruling", shorter_url),
            AlternateReportingCandidate("Anthropic blacklisting ruling", tied_long_url),
            AlternateReportingCandidate("Anthropic blacklisting ruling", longer_url),
        ]
    )
    fetched_urls = []
    summarizer = CapturingSummarizer()

    def fetch(url, **kwargs):
        fetched_urls.append(url)
        if url == original_url:
            raise origin_block_failure("datadome_challenge")
        filler = 250 if url in {tied_long_url, longer_url} else 20
        return ArticleFetchResult(
            alternate_reporting_body(extra_filler=filler),
            method="direct",
            extractor="trafilatura",
            retrieved_url=url,
        )

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-29",
        algolia_stories=[anthropic_nytimes_story()],
        hot_stories=[],
        article_fetcher=fetch,
        alternate_reporting_finder=finder,
        summarizer=summarizer,
    )

    assert fetched_urls == [original_url, shorter_url, tied_long_url, longer_url]
    assert summarizer.fetched_texts == [
        alternate_reporting_body(extra_filler=250)
    ]
    payload = json.loads(
        (tmp_path / "data/2026-08-29-hn-candidates.json").read_text(
            encoding="utf-8"
        )
    )[0]
    assert payload["article_retrieval"]["retrieved_url"] == longer_url


def test_alternate_reporting_conflict_fails_closed(tmp_path):
    original_url = nytimes_anthropic_url()
    first_url = "https://finance.yahoo.com/news/first-event-identity.html"
    second_url = "https://ca.finance.yahoo.com/news/second-event-identity.html"
    finder = FakeAlternateReportingFinder(
        [
            AlternateReportingCandidate(
                "Judge Pentagon blacklisting Anthropic",
                first_url,
            ),
            AlternateReportingCandidate(
                "Trump administration ruling was illegal",
                second_url,
            ),
        ]
    )
    first_body = (
        "Aug 28 (Reuters) - A judge ruled on the Pentagon blacklisting of "
        "Anthropic. "
        + ("Grounded report detail. " * 18)
        + "(Reporting by First Reporter)"
    )
    second_body = (
        "Aug 28 (Reuters) - The Trump administration received a ruling that "
        "the government action was illegal. "
        + ("Grounded report detail. " * 18)
        + "(Reporting by Second Reporter)"
    )
    summarizer = FakeSummarizer()

    def fetch(url, **kwargs):
        if url == original_url:
            raise origin_block_failure("datadome_challenge")
        return ArticleFetchResult(
            first_body if url == first_url else second_body,
            method="direct",
            extractor="trafilatura",
            retrieved_url=url,
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-29",
        algolia_stories=[anthropic_nytimes_story()],
        hot_stories=[],
        article_fetcher=fetch,
        alternate_reporting_finder=finder,
        summarizer=summarizer,
    )

    payload = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    recovery = payload["article_retrieval"]["alternate_reporting_recovery"]
    assert recovery["status"] == "conflict"
    assert recovery["error_code"] == "event_identity_conflict"
    assert payload["summary_status"] == "skipped"
    assert summarizer.titles == []


def test_alternate_reporting_is_disabled_for_classification_and_reuters_origins(
    tmp_path,
):
    alternate_finder = FakeAlternateReportingFinder([])
    block = origin_block_failure("datadome_challenge")

    run_generate(
        output_dir=tmp_path / "classification-briefs",
        data_dir=tmp_path / "classification-data",
        date_label="2026-08-29",
        algolia_stories=[
            Story(
                source="algolia",
                hn_item_id="outside",
                title="Court decision involving a private company",
                source_url=nytimes_anthropic_url(),
                hn_discussion_url="https://news.ycombinator.com/item?id=outside",
                created_at="2026-08-28T02:00:00Z",
                points=500,
                comments=300,
            )
        ],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: (_ for _ in ()).throw(block),
        alternate_reporting_finder=alternate_finder,
        classifier=FakeClassifier(),
        summarizer=FakeSummarizer(),
    )
    assert alternate_finder.calls == []

    reuters_finder = FakeAlternateReportingFinder([])
    run_generate(
        output_dir=tmp_path / "reuters-briefs",
        data_dir=tmp_path / "reuters-data",
        date_label="2026-08-29",
        algolia_stories=[
            story(
                "49473523",
                "Anthropic court ruling",
                points=500,
                comments=300,
                url=(
                    "https://www.reuters.com/legal/government/"
                    "anthropic-ruling-2026-08-28/"
                ),
            )
        ],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: (_ for _ in ()).throw(block),
        syndicated_finder=FakeSyndicatedFinder([]),
        alternate_reporting_finder=reuters_finder,
        summarizer=FakeSummarizer(),
    )
    assert reuters_finder.calls == []


def test_alternate_reporting_rejects_cross_host_redirect_and_keeps_origin_failure(
    tmp_path,
):
    original_url = nytimes_anthropic_url()
    alternate_url = yahoo_anthropic_url()

    def fetch(url, **kwargs):
        if url == original_url:
            raise origin_block_failure("datadome_challenge")
        return ArticleFetchResult(
            alternate_reporting_body(),
            method="direct",
            extractor="trafilatura",
            retrieved_url="https://evil.example/redirected",
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-29",
        algolia_stories=[anthropic_nytimes_story()],
        hot_stories=[],
        article_fetcher=fetch,
        alternate_reporting_finder=FakeAlternateReportingFinder(
            [AlternateReportingCandidate("Anthropic blacklisting ruling", alternate_url)]
        ),
        summarizer=FakeSummarizer(),
    )

    payload = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    retrieval = payload["article_retrieval"]
    assert retrieval["status"] == "failed"
    assert retrieval["fallback_reason"] == "datadome_challenge"
    assert retrieval["origin_failure"] is None
    assert retrieval["alternate_reporting_recovery"]["status"] == "exhausted"
    assert retrieval["alternate_reporting_recovery"]["rejection_reasons"] == [
        "redirected_to_unsupported_url"
    ]
    assert payload["summary_status"] == "skipped"


def test_alternate_reporting_prefix_is_not_used_when_summary_fails(tmp_path):
    original_url = nytimes_anthropic_url()
    alternate_url = yahoo_anthropic_url()

    def fetch(url, **kwargs):
        if url == original_url:
            raise origin_block_failure("datadome_challenge")
        return ArticleFetchResult(
            alternate_reporting_body(),
            method="direct",
            extractor="trafilatura",
            retrieved_url=alternate_url,
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-29",
        algolia_stories=[anthropic_nytimes_story()],
        hot_stories=[],
        article_fetcher=fetch,
        alternate_reporting_finder=FakeAlternateReportingFinder(
            [AlternateReportingCandidate("Anthropic blacklisting ruling", alternate_url)]
        ),
        summarizer=RaisingSummarizer(),
    )

    public_item = json.loads(result.public_json_path.read_text(encoding="utf-8"))[
        "sections"
    ]["ai"]["items"][0]
    assert not public_item["summary"].startswith("据 Reuters")
    assert public_item["content_status"] == "summary_failed"
