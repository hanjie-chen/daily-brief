import json

import pytest

from daily_brief.llm.gemini_backend import GeminiBackend
from daily_brief.llm.gemini_output import (
    GeminiResponseError,
    validate_material_summary,
)
from daily_brief.llm.summarizer import InsufficientSummaryMaterial, MAX_HN_COMMENTS_CHARS
from daily_brief.llm.summarizer import (
    SUMMARY_MODE_GENERIC,
    build_summary_context,
    build_summary_prompt,
    route_summary_mode,
)
from daily_brief.models import Candidate, Story


def candidate(*, fetched_text="", story_text="", comments=""):
    item = Candidate(
        story=Story(
            source="test",
            hn_item_id="1",
            title="Show HN: A video explainer site",
            source_url="https://example.com",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-09-29T00:00:00Z",
            points=1,
            comments=1,
            fetched_text=fetched_text,
            story_text=story_text,
        )
    )
    item.summary_input_mode = "materials"
    item.summary_basis = "hn_comments"
    item.discussion_text = comments
    return item


def sufficient(**fields):
    return {
        "status": "sufficient",
        "metadata_summary": "",
        "article_summary": "",
        "post_summary": "",
        "comments_summary": "",
        "reason": "",
        **fields,
    }


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode()

    def read(self, amount=-1):
        return self.payload if amount < 0 else self.payload[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecordingOpener:
    def __init__(self, output):
        self.output = output
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return FakeResponse({
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{
                "type": "text", "text": json.dumps(self.output, ensure_ascii=False),
            }]}],
        })


def request_schema(opener):
    payload = json.loads(opener.requests[0].data)
    return payload["response_format"]["schema"]


def test_material_context_separates_metadata_body_post_and_comments():
    item = candidate(
        fetched_text=(
            "Page metadata (publisher-provided context, not article body):\n"
            "A home page with short video explainers.\n\nExtracted body:\n"
            "The site generates a video after a visitor opens a story."
        ),
        story_text="It uses an LLM and costs about four cents per video.",
        comments="A commenter says the generated clips are useful as a preview.",
    )

    context = build_summary_context(item)
    prompt = build_summary_prompt(item)

    assert context.strategy == "materials"
    assert {"web_metadata", "web_body", "hn_post", "hn_comments"} <= set(context.sections)
    assert "A home page with short video explainers." in context.text
    assert "The site generates a video" in context.text
    assert "costs about four cents" in context.text
    assert "generated clips are useful" in context.text
    assert "metadata_summary" in prompt
    assert "comments_summary" in prompt
    assert route_summary_mode(item) == SUMMARY_MODE_GENERIC


def test_material_validator_adds_code_owned_attribution_and_records_actual_sources():
    item = candidate(
        fetched_text="Page metadata (publisher-provided context, not article body):\nA video site.\n\nExtracted body:\nBody detail.",
        story_text="Post detail.",
        comments="Comment detail.",
    )

    summary = validate_material_summary(
        sufficient(
            metadata_summary="它为每个故事提供短视频解释。",
            post_summary="发帖者称视频按需生成。",
            comments_summary="有人认为它适合快速预览。",
        ),
        item,
    )

    assert summary == (
        "网站介绍称：它为每个故事提供短视频解释。 "
        "根据 HN 发帖者介绍：发帖者称视频按需生成。 "
        "根据 Hacker News 部分评论：有人认为它适合快速预览。"
    )
    assert item.summary_sources_used == ["web_metadata", "hn_post", "hn_comments"]


def test_material_validator_rejects_a_nonempty_unavailable_source_without_audit_update():
    item = candidate(fetched_text="Page body only.")
    item.summary_sources_used = ["stale"]

    with pytest.raises(GeminiResponseError, match="unavailable source hn_comments"):
        validate_material_summary(
            sufficient(comments_summary="有人提出限制。"), item
        )

    assert item.summary_sources_used == ["stale"]


def test_material_backend_uses_four_field_schema_and_records_sources_after_success():
    opener = RecordingOpener(sufficient(
        article_summary="页面说明视频会在首次打开故事时生成。",
        post_summary="发帖者称每条视频成本约四美分。",
    ))
    backend = GeminiBackend(api_key="secret", opener=opener)
    item = candidate(fetched_text="Page body.", story_text="Post detail.")

    assert backend.summarize(item) == (
        "根据网页内容：页面说明视频会在首次打开故事时生成。 "
        "根据 HN 发帖者介绍：发帖者称每条视频成本约四美分。"
    )
    assert request_schema(opener)["required"] == [
        "status", "metadata_summary", "article_summary", "post_summary",
        "comments_summary", "reason",
    ]
    assert item.summary_sources_used == ["web_body", "hn_post"]


@pytest.mark.parametrize("output", [
    {"status": "bad", "metadata_summary": "", "article_summary": "", "post_summary": "", "comments_summary": "", "reason": ""},
    sufficient(article_summary="x" * 1_001),
    sufficient(article_summary=3),
])
def test_material_backend_rejects_invalid_status_type_or_oversized_output(output):
    backend = GeminiBackend(api_key="secret", opener=RecordingOpener(output))
    item = candidate(fetched_text="Page body.")

    with pytest.raises(GeminiResponseError):
        backend.summarize(item)
    assert item.summary_sources_used == []


def test_material_backend_reports_insufficient_and_omits_absent_comments():
    backend = GeminiBackend(api_key="secret", opener=RecordingOpener({
        "status": "insufficient", "metadata_summary": "", "article_summary": "",
        "post_summary": "", "comments_summary": "", "reason": "No useful facts.",
    }))
    item = candidate(fetched_text="", story_text="", comments="")

    with pytest.raises(InsufficientSummaryMaterial):
        backend.summarize(item)
    assert item.summary_sources_used == []


def test_material_context_preserves_ordered_comment_sample_and_bounds_post_and_research_body():
    research = (
        "Abstract\n" + "Abstract fact. " * 30 + "\nIntroduction\n" + "Setup. " * 40
        + "\nResults\n" + "Result fact. " * 1200 + "\nConclusion\n" + "Limit. " * 80
        + "\nReferences\n" + "Citation. " * 100
    )
    post = "Post fact. " * 1_000
    comments = "FIRST_REPLY\n" + "comment. " * 4_000 + "LAST_REPLY"
    item = candidate(fetched_text=research, story_text=post, comments=comments)

    context = build_summary_context(item)

    assert "Abstract fact." in context.text
    assert "Limit." in context.text
    assert len(context.text.split("Untrusted HN post text", 1)[1].split("Untrusted HN comments", 1)[0]) <= 6_100
    comment_block = context.text.split("Untrusted HN comments (bounded sample):\n", 1)[1]
    assert comment_block.startswith("FIRST_REPLY")
    assert len(comment_block) <= MAX_HN_COMMENTS_CHARS
    assert "[HN comment sample truncated]" in comment_block
