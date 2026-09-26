import json
import logging

import pytest

from daily_brief import article_fetcher as article_fetcher_module
from daily_brief.article_fetcher import ArticleFetchError, ArticleFetchResult
from daily_brief.generation import material, run_generate
from daily_brief.llm.summarizer import SUMMARY_MODE_RESEARCH_REPORT
from fakes import CapturingSummarizer, FakeClassifier, FakeSummarizer, story


def test_bounded_error_message_is_single_line_and_limited():
    message = material.bounded_error_message(RuntimeError("first\n" + "x" * 600))

    assert message.startswith("first ")
    assert "\n" not in message
    assert len(message) == 500


def test_selected_exploration_article_reuses_classification_fetch(tmp_path):
    fetched_urls = []

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[story("1", "Outside hot story", points=500, comments=20)],
        hot_stories=[],
        classifier=FakeClassifier(),
        article_fetcher=lambda url, **kwargs: fetched_urls.append(url) or "Outside evidence.",
        summarizer=FakeSummarizer(),
    )

    assert fetched_urls == ["https://example.com/1"]
    assert result.public_json_path is not None


def test_default_classification_fetch_uses_bounded_policy(tmp_path, monkeypatch):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return "Grounded outside evidence."

    monkeypatch.setattr(material, "fetch_article", fetch)

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[story("1", "Outside hot story", points=500, comments=20)],
        hot_stories=[],
        classifier=FakeClassifier(),
        summarizer=FakeSummarizer(),
    )

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["timeout_seconds"] == material.CLASSIFICATION_HTTP_TIMEOUT_SECONDS
    assert (
        kwargs["pdf_parse_timeout_seconds"]
        == material.CLASSIFICATION_PDF_PARSE_TIMEOUT_SECONDS
    )
    assert kwargs["policy"] is material.CLASSIFICATION_FETCH_POLICY


def test_injected_classification_fetch_receives_bounded_policy(tmp_path):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return "Grounded outside evidence."

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[story("1", "Outside hot story", points=500, comments=20)],
        hot_stories=[],
        classifier=FakeClassifier(),
        article_fetcher=fetch,
        summarizer=FakeSummarizer(),
    )

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["timeout_seconds"] == material.CLASSIFICATION_HTTP_TIMEOUT_SECONDS
    assert (
        kwargs["pdf_parse_timeout_seconds"]
        == material.CLASSIFICATION_PDF_PARSE_TIMEOUT_SECONDS
    )
    assert kwargs["policy"] is material.CLASSIFICATION_FETCH_POLICY


def test_injected_summary_fetch_receives_full_policy_and_wayback_bounds(tmp_path):
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return "Grounded core article evidence."

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        article_fetcher=fetch,
        summarizer=FakeSummarizer(),
    )

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["policy"] is material.SUMMARY_FETCH_POLICY
    assert kwargs["wayback_not_before"] < kwargs["wayback_not_after"]
    assert "timeout_seconds" not in kwargs


def test_classification_youtube_skip_is_audited_with_zero_attempts(
    tmp_path, monkeypatch
):
    def fetch(url, **kwargs):
        return article_fetcher_module.fetch_article(
            url,
            resolver=lambda host, port, type: [
                (2, type, 6, "", ("93.184.216.34", port))
            ],
            **kwargs,
        )

    monkeypatch.setattr(material, "fetch_article", fetch)
    classifier = FakeClassifier()

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story(
                "1",
                "A recorded lecture",
                points=500,
                comments=20,
                url="https://www.youtube.com/watch?v=68X8yEatepQ",
            )
        ],
        hot_stories=[],
        classifier=classifier,
        summarizer=FakeSummarizer(),
    )

    record = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    assert classifier.seen_ids == []
    assert record["topic_route"] == "topic_unknown"
    assert record["article_retrieval"]["error_code"] == (
        "youtube_skipped_in_classification"
    )
    assert record["article_retrieval"]["attempts"] == 0


def test_adobe_markdown_from_classification_is_reused_for_research_summary(tmp_path):
    body = """# Abstract
This study links enterprise account records to worker roles and financial data. It measures adoption and usage across more than 1,500 organizations and distinguishes descriptive associations from causal effects.
# 1 Introduction
Background and literature that should not enter the summary evidence.
# 2 Methods
Detailed sample construction that should not enter the summary evidence.
# 3 Results
Output tokens increased sevenfold, while an existing cohort increased fourfold. Adoption was concentrated among larger and more R&D-intensive firms, and early-career workers used the product more intensively.
# 4 Conclusion
The analysis covers only Enterprise accounts and does not measure downstream productivity or establish that adoption caused stronger financial outcomes.
# References
A bibliography that should not enter the summary evidence.
"""
    calls = []

    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return ArticleFetchResult(
            text=body,
            method="direct",
            extractor="adobe_pdf_to_markdown",
            retrieved_url=url,
        )

    summarizer = CapturingSummarizer()
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[
            story(
                "1",
                "Enterprise adoption study [pdf]",
                points=500,
                comments=80,
                url="https://example.com/report.pdf",
            )
        ],
        hot_stories=[],
        classifier=FakeClassifier(default_label="ai"),
        article_fetcher=fetch,
        summarizer=summarizer,
    )

    selected = json.loads(result.data_path.read_text(encoding="utf-8"))[0]
    assert len(calls) == 1
    assert calls[0][1]["policy"] is material.CLASSIFICATION_FETCH_POLICY
    assert summarizer.fetched_texts == [body.strip()]
    assert summarizer.summary_modes == [SUMMARY_MODE_RESEARCH_REPORT]
    assert selected["article_retrieval"]["extractor"] == "adobe_pdf_to_markdown"
    assert selected["summary_mode"] == SUMMARY_MODE_RESEARCH_REPORT


def test_external_url_is_fetched_even_when_story_text_contains_only_a_link(tmp_path):
    fetched_urls = []
    body = "Grounded facts from the external article."

    def fetch_article(url, **kwargs):
        fetched_urls.append(url)
        return ArticleFetchResult(
            text=body,
            method="github_readme",
            extractor="plain_text",
            retrieved_url=url,
            material_origin="original",
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[
            story(
                "1",
                "Claude release",
                points=40,
                comments=8,
                story_text=(
                    '<a href="https://web.archive.org/example">'
                    "https://web.archive.org/example</a>"
                ),
                url="https://github.com/example/project",
            )
        ],
        hot_stories=[],
        article_fetcher=fetch_article,
        summarizer=FakeSummarizer(),
    )

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    retrieval = candidate_payload[0]["article_retrieval"]

    assert fetched_urls == ["https://github.com/example/project"]
    assert retrieval["status"] == "success"
    assert retrieval["method"] == "github_readme"
    assert candidate_payload[0]["summary_basis"] == "fetched_article"
    assert candidate_payload[0]["summary_context"] == {
        "strategy": "full_text",
        "source_chars": len(body),
        "selected_chars": len(body),
        "sections": [],
    }


def test_empty_content_jina_success_is_summarized_and_persisted(tmp_path):
    summarizer = FakeSummarizer()
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: ArticleFetchResult(
            text="Grounded article facts.",
            method="jina",
            fallback_reason="empty_content",
            extractor="jina",
        ),
        summarizer=summarizer,
    )

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    retrieval = candidate_payload[0]["article_retrieval"]

    assert retrieval["status"] == "success"
    assert retrieval["method"] == "jina"
    assert retrieval["extractor"] == "jina"
    assert retrieval["fallback_attempted"] is True
    assert retrieval["fallback_reason"] == "empty_content"
    assert candidate_payload[0]["summary_basis"] == "fetched_article"
    assert candidate_payload[0]["summary_status"] == "success"
    assert summarizer.titles == ["Claude release"]


def test_wayback_article_is_summarized_with_archived_copy_provenance(tmp_path):
    summarizer = FakeSummarizer()
    replay_url = (
        "https://web.archive.org/web/20260822062417id_/https://www.felonybench.com/"
    )
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-23",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: ArticleFetchResult(
            text="Grounded archived article facts.",
            method="wayback",
            extractor="trafilatura",
            fallback_reason="vercel_challenge",
            attempts=4,
            retrieved_url=replay_url,
            material_origin="archived_copy",
        ),
        summarizer=summarizer,
    )

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    retrieval = candidate_payload[0]["article_retrieval"]

    assert retrieval["status"] == "success"
    assert retrieval["method"] == "wayback"
    assert retrieval["extractor"] == "trafilatura"
    assert retrieval["fallback_attempted"] is True
    assert retrieval["fallback_reason"] == "vercel_challenge"
    assert retrieval["attempts"] == 4
    assert retrieval["retrieved_url"] == replay_url
    assert retrieval["material_origin"] == "archived_copy"
    assert candidate_payload[0]["summary_basis"] == "fetched_article"
    assert candidate_payload[0]["summary_status"] == "success"
    assert summarizer.titles == ["Claude release"]


def test_youtube_caption_is_used_as_the_summary_basis(tmp_path):
    summarizer = CapturingSummarizer()
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-08-14",
        algolia_stories=[
            story(
                "1",
                "The AI boom isn't real",
                points=40,
                comments=8,
                url="https://www.youtube.com/watch?v=68X8yEatepQ",
            )
        ],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: ArticleFetchResult(
            text=(
                "The interview argues that AI infrastructure revenue is concentrated "
                "among a small number of customers."
            ),
            method="youtube_caption",
            extractor="yt_dlp",
        ),
        summarizer=summarizer,
    )

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    selected = candidate_payload[0]
    assert summarizer.fetched_texts == [
        "The interview argues that AI infrastructure revenue is concentrated "
        "among a small number of customers."
    ]
    assert selected["article_retrieval"]["method"] == "youtube_caption"
    assert selected["article_retrieval"]["extractor"] == "yt_dlp"
    assert selected["summary_basis"] == "youtube_caption"
    assert selected["summary_status"] == "success"


def test_github_readme_retrieval_provenance_is_persisted(tmp_path):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        article_fetcher=lambda url, **kwargs: ArticleFetchResult(
            text="Grounded repository README facts.",
            method="github_readme",
            extractor="plain_text",
        ),
        summarizer=FakeSummarizer(),
    )

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    retrieval = candidate_payload[0]["article_retrieval"]

    assert retrieval["status"] == "success"
    assert retrieval["method"] == "github_readme"
    assert retrieval["extractor"] == "plain_text"
    assert retrieval["fallback_attempted"] is False
    assert retrieval["fallback_reason"] == ""


def test_github_pdf_retrieval_provenance_and_logging_are_persisted(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="daily_brief"):
        result = run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-20",
            algolia_stories=[story("1", "Claude release", points=40, comments=8)],
            hot_stories=[],
            article_fetcher=lambda url, **kwargs: ArticleFetchResult(
                text="Grounded PDF facts.",
                method="github_raw",
                extractor="pypdf",
            ),
            summarizer=FakeSummarizer(),
        )

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    retrieval = candidate_payload[0]["article_retrieval"]

    assert retrieval["status"] == "success"
    assert retrieval["method"] == "github_raw"
    assert retrieval["extractor"] == "pypdf"
    assert (
        "item_id=1 status=success method=github_raw extractor=pypdf "
        "fallback_reason=none"
    ) in caplog.text


def test_article_failure_does_not_prevent_brief_generation(tmp_path, caplog):
    def raise_fetch_error(url, **kwargs):
        raise ArticleFetchError(
            "HTTP Error 403: Forbidden",
            error_code="http_403",
            method="direct",
            extractor="trafilatura",
        )

    summarizer = FakeSummarizer()

    with caplog.at_level(logging.ERROR, logger="daily_brief"):
        result = run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-20",
            algolia_stories=[story("1", "Claude release", points=40, comments=8)],
            hot_stories=[],
            article_fetcher=raise_fetch_error,
            summarizer=summarizer,
            capture_model_inputs=True,
        )

    assert result.brief_path.exists()
    markdown = result.brief_path.read_text(encoding="utf-8")
    assert "原文抓取失败，未生成可靠摘要" in markdown
    assert "- Content: Error — 原文抓取失败（http_403）。" in markdown
    assert summarizer.titles == []
    assert (
        "component=article_fetch item_id=1 status=failed method=direct "
        "extractor=trafilatura error=ArticleFetchError code=http_403"
    ) in caplog.text

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    failed = next(item for item in candidate_payload if item["hn_item_id"] == "1")
    assert failed["article_retrieval"] == {
        "status": "failed",
        "method": "direct",
        "extractor": "trafilatura",
        "attempts": 1,
        "fallback_attempted": False,
        "fallback_reason": "",
        "error_type": "ArticleFetchError",
        "error_code": "http_403",
        "error_message": "HTTP Error 403: Forbidden",
        "retrieved_url": "",
        "material_origin": "",
        "origin_failure": None,
        "same_article_recovery": {
            "status": "not_attempted", "provider": "", "query": "",
            "discovered_candidates": 0, "attempted_candidates": 0,
            "candidates": [], "error_code": "",
        },
        "syndicated_recovery": {
            "status": "not_attempted",
            "provider": "",
            "discovered_candidates": 0,
            "attempted_candidates": 0,
            "rejection_reasons": [],
            "error_code": "",
        },
        "alternate_reporting_recovery": {
            "status": "not_attempted",
            "provider": "",
            "discovered_candidates": 0,
            "attempted_candidates": 0,
            "rejection_reasons": [],
            "error_code": "",
        },
    }
    assert failed["summary_basis"] == "none"
    assert failed["summary_status"] == "skipped"
    assert failed["summary_generation"]["status"] == "skipped"
    assert failed["summary_generation"]["attempts"] == 0

    public_payload = json.loads(result.public_json_path.read_text(encoding="utf-8"))
    public_item = public_payload["sections"]["ai"]["items"][0]
    assert public_item["content_status"] == "fetch_failed"
    assert "HTTP Error 403" not in result.public_json_path.read_text(encoding="utf-8")

    model_input = json.loads(result.model_input_path.read_text(encoding="utf-8"))
    assert model_input["summary_candidates"] == []


@pytest.mark.parametrize("fallback_reason", ["datadome_challenge", "vercel_challenge"])
def test_origin_block_failure_uses_specific_reader_message(
    tmp_path,
    fallback_reason,
):
    def raise_fetch_error(url, **kwargs):
        raise ArticleFetchError(
            "article retrieval failed after origin challenge",
            error_code="http_403",
            method="jina",
            extractor="jina",
            fallback_attempted=True,
            fallback_reason=fallback_reason,
        )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        article_fetcher=raise_fetch_error,
        summarizer=FakeSummarizer(),
    )

    markdown = result.brief_path.read_text(encoding="utf-8")
    assert "来源网站阻止自动抓取，未生成可靠摘要" in markdown
    assert "- Content: Error — 来源网站阻止自动抓取。" in markdown

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    retrieval = candidate_payload[0]["article_retrieval"]
    assert retrieval["fallback_attempted"] is True
    assert retrieval["fallback_reason"] == fallback_reason
    assert retrieval["error_code"] == "http_403"


def test_empty_content_jina_failure_skips_summary_and_persists_provenance(
    tmp_path,
):
    def raise_fetch_error(url, **kwargs):
        raise ArticleFetchError(
            "article retrieval failed: direct=trafilatura empty_content; "
            "jina=Jina Reader returned malformed JSON",
            error_code="jina_malformed_json",
            method="jina",
            extractor="jina",
            fallback_attempted=True,
            fallback_reason="empty_content",
        )

    summarizer = FakeSummarizer()
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        article_fetcher=raise_fetch_error,
        summarizer=summarizer,
    )

    candidate_payload = json.loads(result.data_path.read_text(encoding="utf-8"))
    failed = candidate_payload[0]
    retrieval = failed["article_retrieval"]
    assert retrieval["status"] == "failed"
    assert retrieval["method"] == "jina"
    assert retrieval["extractor"] == "jina"
    assert retrieval["fallback_attempted"] is True
    assert retrieval["fallback_reason"] == "empty_content"
    assert retrieval["error_code"] == "jina_malformed_json"
    assert "direct=trafilatura empty_content" in retrieval["error_message"]
    assert "jina=Jina Reader returned malformed JSON" in retrieval["error_message"]
    assert failed["summary_basis"] == "none"
    assert failed["summary_status"] == "skipped"
    assert summarizer.titles == []
    assert "原文抓取失败，未生成可靠摘要" in result.brief_path.read_text(
        encoding="utf-8"
    )
