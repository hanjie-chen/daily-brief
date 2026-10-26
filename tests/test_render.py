import json
from dataclasses import replace

import pytest

from daily_brief.models import ArticleRetrieval, Candidate, KeywordMatch, Story
from daily_brief.output import (
    render_candidates_json,
    render_public_brief_json,
)


def candidate(section="ai", selected=True, title="AI coding agent"):
    item = Candidate(
        story=Story(
            source="algolia",
            hn_item_id="1",
            title=title,
            source_url="https://example.com",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-07-08T00:00:00+08:00",
            points=30,
            comments=5,
        ),
        matched_keywords=[KeywordMatch("AI coding", "high", "text", 4.0, 0, 9)],
        score=9.2,
        selected=selected,
        section=section,
        summary="这是一个 AI coding agent 项目。",
        why="keywords: AI coding",
    )
    return item


def test_render_candidates_json_uses_snake_case_fields():
    item = candidate(selected=False, title="中文 AI coding agent")
    item.score = 9.23456
    item.rejection_reason = "not_selected"

    rendered = render_candidates_json([item])
    data = json.loads(rendered)

    assert rendered.endswith("\n")
    assert "中文 AI coding agent" in rendered
    assert set(data[0]) == {
        "source",
        "hn_item_id",
        "title",
        "source_url",
        "hn_discussion_url",
        "created_at",
        "summary_reference_date",
        "webpage_published_at",
        "points",
        "comments",
        "matched_keywords",
        "topic_route",
        "content_kind",
        "content_reason",
        "summary_mode",
        "summary_input_mode",
        "summary_sources_used",
        "summary_context",
        "score",
        "selected",
        "section",
        "rejection_reason",
        "content_deduplication",
        "article_retrieval",
        "discussion_retrieval",
        "hn_post_retrieval",
        "summary_basis",
        "summary_status",
        "summary_generation",
        "source_material",
    }
    assert data[0]["source"] == "algolia"
    assert data[0]["hn_item_id"] == "1"
    assert data[0]["title"] == "中文 AI coding agent"
    assert data[0]["source_url"] == "https://example.com"
    assert data[0]["hn_discussion_url"] == "https://news.ycombinator.com/item?id=1"
    assert data[0]["created_at"] == "2026-07-08T00:00:00+08:00"
    assert data[0]["points"] == 30
    assert data[0]["comments"] == 5
    assert data[0]["matched_keywords"] == ["AI coding"]
    assert data[0]["topic_route"] == "not_evaluated"
    assert data[0]["summary_mode"] == "not_routed"
    assert data[0]["summary_context"] == {
        "strategy": "not_prepared",
        "source_chars": 0,
        "selected_chars": 0,
        "sections": [],
    }
    assert data[0]["score"] == 9.2346
    assert data[0]["selected"] is False
    assert data[0]["section"] == "ai"
    assert data[0]["rejection_reason"] == "not_selected"
    assert data[0]["article_retrieval"] == {
        "status": "not_attempted",
        "method": "",
        "extractor": "",
        "attempts": 0,
        "fallback_attempted": False,
        "fallback_reason": "",
        "error_type": "",
        "error_code": "",
        "error_message": "",
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
    assert data[0]["discussion_retrieval"] == {
        "status": "not_attempted",
        "comments": 0,
        "chars": 0,
        "requested_items": 0,
        "failed_items": 0,
        "top_level_comments": 0,
        "reply_comments": 0,
        "stop_reason": "",
        "error_type": "",
        "error_code": "",
        "error_message": "",
    }
    assert data[0]["summary_basis"] == "not_generated"
    assert data[0]["summary_status"] == "not_generated"
    assert data[0]["summary_generation"] == {
        "status": "not_attempted",
        "provider": "",
        "model": "",
        "attempts": 0,
        "provider_status": "",
        "reasoning_effort": None,
        "input_tokens": None,
        "output_tokens": None,
        "thought_tokens": None,
        "total_tokens": None,
        "error_type": "",
        "error_code": "",
        "http_status": None,
        "error_message": "",
    }


def test_render_candidates_json_records_research_context_without_exposing_text():
    item = candidate()
    item.summary_mode = "research_report"
    item.summary_context_strategy = "research_sections"
    item.summary_context_source_chars = 110_309
    item.summary_context_selected_chars = 31_071
    item.summary_context_sections = ["abstract", "results_through_conclusion"]

    data = json.loads(render_candidates_json([item]))[0]

    assert data["summary_context"] == {
        "strategy": "research_sections",
        "source_chars": 110_309,
        "selected_chars": 31_071,
        "sections": ["abstract", "results_through_conclusion"],
    }
    assert "text" not in data["summary_context"]


def test_render_public_brief_json_contains_stable_schema_and_selected_items():
    ai_item = candidate(title="中文 <AI> agent")
    hot_item = candidate(section="non_ai_hot", title="SQLite release")

    rendered = render_public_brief_json(
        "2026-07-08",
        "2026-07-08T08:04:00+08:00",
        [ai_item],
        [hot_item],
        ai_note="栏目可能不完整。",
    )
    payload = json.loads(rendered)

    assert payload == {
        "schema_version": 2,
        "date": "2026-07-08",
        "generated_at": "2026-07-08T08:04:00+08:00",
        "timezone": "Asia/Singapore",
        "sections": {
            "ai": {
                "note": "栏目可能不完整。",
                "items": [
                    {
                        "hn_item_id": "1",
                        "title": "中文 <AI> agent",
                        "summary": "这是一个 AI coding agent 项目。",
                        "content_status": "ok",
                        "why": "keywords: AI coding",
                        "source_url": "https://example.com",
                        "discussion_url": "https://news.ycombinator.com/item?id=1",
                        "points": 30,
                        "comments": 5,
                        "generation_info": {
                            "materials": {
                                "webpage": {"status": "not_attempted", "method": "unknown", "origin": "unknown", "reason": "none"},
                                "hn_post": {"status": "unknown", "reason": "unknown"},
                                "hn_comments": {"status": "not_attempted", "reason": "none"},
                            },
                            "summary_sources": [],
                            "generation": {"status": "not_attempted", "model": None, "reason": "none"},
                        },
                    }
                ],
            },
            "non_ai_hot": {
                "note": "",
                "items": [
                    {
                        "hn_item_id": "1",
                        "title": "SQLite release",
                        "summary": "这是一个 AI coding agent 项目。",
                        "content_status": "ok",
                        "why": "keywords: AI coding",
                        "source_url": "https://example.com",
                        "discussion_url": "https://news.ycombinator.com/item?id=1",
                        "points": 30,
                        "comments": 5,
                        "generation_info": {
                            "materials": {
                                "webpage": {"status": "not_attempted", "method": "unknown", "origin": "unknown", "reason": "none"},
                                "hn_post": {"status": "unknown", "reason": "unknown"},
                                "hn_comments": {"status": "not_attempted", "reason": "none"},
                            },
                            "summary_sources": [],
                            "generation": {"status": "not_attempted", "model": None, "reason": "none"},
                        },
                    }
                ],
            },
        },
    }
    assert rendered.endswith("\n")


def test_render_public_brief_json_normalizes_untrusted_multiline_text():
    item = candidate(title="Title\nInjected")
    item.summary = "First\nSecond"
    item.why = "because\tkeywords"

    payload = json.loads(
        render_public_brief_json(
            "2026-07-08",
            "2026-07-08T08:04:00+08:00",
            [item],
            [],
        )
    )
    public_item = payload["sections"]["ai"]["items"][0]

    assert public_item["title"] == "Title Injected"
    assert public_item["summary"] == "First Second"
    assert public_item["why"] == "because keywords"


def test_render_marks_article_fetch_failure_without_exposing_raw_error():
    item = candidate()
    item.summary = "原文抓取失败，未生成可靠摘要；请查看原文或讨论。"
    item.article_retrieval = ArticleRetrieval(
        status="failed",
        method="direct",
        error_type="ArticleFetchError",
        error_code="http_403",
        error_message="secret upstream detail",
    )
    item.summary_basis = "none"
    item.summary_status = "skipped"

    public_payload = json.loads(
        render_public_brief_json(
            "2026-07-08",
            "2026-07-08T08:04:00+08:00",
            [item],
            [],
        )
    )

    assert public_payload["sections"]["ai"]["items"][0]["content_status"] == (
        "fetch_failed"
    )
    assert "secret upstream detail" not in json.dumps(
        public_payload, ensure_ascii=False
    )


def test_render_labels_origin_block_without_exposing_terminal_fallback_error():
    item = candidate()
    item.summary = "来源网站阻止自动抓取，未生成可靠摘要；请查看原文或讨论。"
    item.article_retrieval = ArticleRetrieval(
        status="failed",
        method="jina",
        extractor="jina",
        fallback_attempted=True,
        fallback_reason="datadome_challenge",
        error_type="ArticleFetchError",
        error_code="http_403",
        error_message="secret Jina detail",
    )

    published = public_item(item)
    assert published["summary"] == item.summary
    assert published["content_status"] == "fetch_failed"
    assert "http_403" not in json.dumps(published)
    assert "secret Jina detail" not in json.dumps(published)


def test_render_labels_hn_discussion_fallback_without_hiding_fetch_failure():
    item = candidate()
    item.article_retrieval = ArticleRetrieval(
        status="failed",
        method="jina",
        fallback_reason="cloudflare_challenge",
    )
    item.summary = "根据 Hacker News 讨论（不代表原文观点）：主要存在两种看法。"
    item.summary_basis = "hn_comments"
    item.summary_status = "success"

    public_payload = json.loads(
        render_public_brief_json(
            "2026-07-08",
            "2026-07-08T08:04:00+08:00",
            [item],
            [],
        )
    )

    assert public_payload["sections"]["ai"]["items"][0]["content_status"] == (
        "fetch_failed"
    )


def test_public_content_status_distinguishes_title_only_and_summary_failure():
    title_only = candidate(title="Title only")
    title_only.summary_basis = "title_only"
    title_only.summary_status = "success"
    summary_failed = candidate(title="Summary failed")
    summary_failed.summary_basis = "fetched_article"
    summary_failed.summary_status = "failed"

    payload = json.loads(
        render_public_brief_json(
            "2026-07-08",
            "2026-07-08T08:04:00+08:00",
            [title_only, summary_failed],
            [],
        )
    )

    assert [item["content_status"] for item in payload["sections"]["ai"]["items"]] == [
        "title_only",
        "summary_failed",
    ]


def test_render_marks_summary_failure_as_distinct_from_fetch_failure():
    item = candidate(title="Summary failed")
    item.summary = "原文已抓取，但摘要生成失败；请查看原文或讨论。"
    item.summary_basis = "fetched_article"
    item.summary_status = "failed"
    item.summary_generation.status = "failed"
    item.summary_generation.error_code = "quota_exceeded"
    item.summary_generation.error_message = "secret provider detail"

    public_payload = json.loads(
        render_public_brief_json(
            "2026-07-08",
            "2026-07-08T08:04:00+08:00",
            [item],
            [],
        )
    )

    public_item = public_payload["sections"]["ai"]["items"][0]
    assert public_item["content_status"] == "summary_failed"
    assert "secret provider detail" not in json.dumps(
        public_payload, ensure_ascii=False
    )


def test_roundup_list_survives_public_json_rendering():
    from daily_brief.output import validate_public_brief

    item = candidate(title="Ask HN: What are you working on?")
    item.content_kind = "community_roundup"
    item.summary_basis = "hn_comments"
    item.summary_status = "success"
    item.summary = (
        "根据 Hacker News 部分评论：这是一个分享近期项目的帖子。\n\n"
        "- 法律工具：追踪法条修改，帮助查看不同版本。\n"
        "- 木工工具：根据尺寸生成制作方案，并比较板材成本。"
    )
    payload = json.loads(render_public_brief_json(
        "2026-09-16", "2026-09-16T08:00:00+08:00", [item], []
    ))
    validate_public_brief(payload)
    assert payload["sections"]["ai"]["items"][0]["summary"] == item.summary


def public_item(item):
    from daily_brief.output import validate_public_brief

    payload = json.loads(render_public_brief_json(
        "2026-09-27", "2026-09-27T08:00:00+08:00", [item], []
    ))
    validate_public_brief(payload)
    return payload["sections"]["ai"]["items"][0]


@pytest.mark.parametrize(
    ("method", "origin", "reason"),
    [
        ("direct", "original", ""),
        ("jina", "original", "empty_content"),
        ("wayback", "archived_copy", "cloudflare_challenge"),
        ("github_readme", "original", ""),
        ("github_raw", "original", ""),
    ],
)
def test_public_generation_info_separates_material_from_transport(method, origin, reason):
    item = candidate()
    item.summary_status = "success"
    item.article_retrieval = ArticleRetrieval(
        status="success", method=method, material_origin=origin,
        fallback_reason=reason, error_message="private diagnostic",
        retrieved_url="https://example.com/private-recovery-url",
    )
    published = public_item(item)
    assert "provenance" not in published
    assert published["generation_info"]["materials"]["webpage"] == {
        "status": "success", "method": method, "origin": origin, "reason": reason or "none",
    }
    assert "private" not in json.dumps(published)
    audit = json.loads(render_candidates_json([item]))[0]["article_retrieval"]
    assert audit["method"] == method
    assert audit["material_origin"] == origin
    assert audit["fallback_reason"] == reason
    assert audit["retrieved_url"] == "https://example.com/private-recovery-url"
    assert audit["error_message"] == "private diagnostic"


@pytest.mark.parametrize("origin", ["same_article", "syndicated_copy", "alternate_reporting"])
def test_public_recovered_material_keeps_origin_failure_reason(origin):
    from daily_brief.models import RetrievalFailure

    item = candidate()
    item.summary_status = "success"
    item.article_retrieval = ArticleRetrieval(
        status="success", method="direct", material_origin=origin,
        origin_failure=RetrievalFailure(
            method="wayback", fallback_reason="datadome_challenge",
            error_message="private diagnostic",
        ),
    )
    material = public_item(item)["generation_info"]["materials"]["webpage"]
    assert material["origin"] == origin
    assert material["method"] == "direct"
    assert material["reason"] == "datadome_challenge"
    audit = json.loads(render_candidates_json([item]))[0]["article_retrieval"]
    assert audit["origin_failure"]["fallback_reason"] == "datadome_challenge"
    assert audit["origin_failure"]["error_message"] == "private diagnostic"


def test_public_discussion_success_does_not_hide_original_fetch_failure():
    item = candidate()
    item.summary_status = "success"
    item.summary_sources_used = ["hn_comments"]
    item.summary = "评论者介绍了两个实践。"
    item.article_retrieval = ArticleRetrieval(
        status="failed", method="jina", fallback_reason="cloudflare_challenge",
    )
    published = public_item(item)
    assert published["content_status"] == "fetch_failed"
    assert published["generation_info"]["summary_sources"] == ["hn_comments"]
    assert published["generation_info"]["materials"]["webpage"]["status"] == "failed"
    assert published["generation_info"]["materials"]["webpage"]["method"] == "jina"
    assert published["summary"] == item.summary


def test_unknown_internal_values_never_become_public_diagnostics():
    item = candidate()
    item.article_retrieval = ArticleRetrieval(
        method="private transport", status="private status",
        material_origin="private origin", fallback_reason="private failure",
    )
    assert set(public_item(item)["generation_info"]["materials"]["webpage"].values()) == {"unknown"}


@pytest.mark.parametrize("sources", [
    ["web_body"], ["hn_post"], ["hn_comments"], ["web_body", "hn_comments"],
    ["web_metadata", "hn_post", "hn_comments"], ["web_metadata"], ["web_body", "hn_post"], [],
])
def test_public_generation_info_uses_declared_sources_not_available_inputs(sources):
    item = candidate()
    item.summary_input_mode = "materials"
    item.summary_status = "success"
    item.summary_sources_used = sources
    item.story = replace(item.story, fetched_text="Available body", story_text="Available post")
    item.discussion_text = "Available but possibly ignored comments"
    assert public_item(item)["generation_info"]["summary_sources"] == (sources or None)
    audit = json.loads(render_candidates_json([item]))[0]
    assert audit["summary_sources_used"] == sources
