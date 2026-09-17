import json

from daily_brief.models import Candidate, Story
from daily_brief.topic_classifier import (
    TOPIC_CLASSIFIER_ARTICLE_TEXT_MAX_CHARS,
    build_topic_classifier_prompt,
)


def candidate(
    item_id: str,
    title: str,
    url: str = "https://example.com/article",
    story_text: str = "",
    fetched_text: str = "",
) -> Candidate:
    return Candidate(
        story=Story(
            source="test",
            hn_item_id=item_id,
            title=title,
            source_url=url,
            hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
            created_at="2026-07-20T00:00:00Z",
            points=100,
            comments=20,
            story_text=story_text,
            fetched_text=fetched_text,
        )
    )


def test_classifier_prompt_contains_taxonomy_and_only_bounded_model_inputs():
    prompt = build_topic_classifier_prompt(
        [candidate("1", "Qwen 3.8"), candidate("2", "SQLite release")]
    )

    assert "Qwen 3.8" in prompt
    assert "SQLite release" in prompt
    assert "example.com" in prompt
    assert "/article" not in prompt
    assert "untrusted" in prompt.lower()
    assert "core_non_ai" in prompt
    assert "Absence of core-interest" in prompt
    assert "is not positive evidence" in prompt
    assert "Claude-driven reverse" in prompt
    assert "polish prose" in prompt


def test_classifier_prompt_prefers_normalized_bounded_fetched_text_excerpt():
    long_text = "  AI agents\ncan help  " + (
        "x" * TOPIC_CLASSIFIER_ARTICLE_TEXT_MAX_CHARS
    )

    prompt = build_topic_classifier_prompt(
        [
            candidate(
                "1",
                "An ambiguous title",
                story_text="HN submission note",
                fetched_text=long_text,
            )
        ]
    )
    payload = json.loads(prompt.split("Untrusted items:\n", 1)[1])
    excerpt = payload[0]["article_evidence_excerpt"]

    assert excerpt.startswith("AI agents can help ")
    assert "\n" not in excerpt
    assert len(excerpt) == TOPIC_CLASSIFIER_ARTICLE_TEXT_MAX_CHARS


def test_classifier_prompt_uses_story_text_when_no_article_was_fetched():
    prompt = build_topic_classifier_prompt(
        [candidate("1", "Self post", story_text="Grounded HN self-post facts.")]
    )
    payload = json.loads(prompt.split("Untrusted items:\n", 1)[1])

    assert payload[0]["article_evidence_excerpt"] == "Grounded HN self-post facts."


def test_classifier_prompt_exposes_self_posts_and_roundup_routing_rules():
    item = candidate(
        "1",
        "Ask HN: What are you working on?",
        url="https://news.ycombinator.com/item?id=1",
        story_text="What projects or tools are you building?",
    )

    prompt = build_topic_classifier_prompt([item])
    payload = json.loads(prompt.split("Untrusted items:\n", 1)[1])

    assert payload[0]["is_self_post"] is True
    assert "community_roundup" in prompt
    assert "Do not use it for every Ask HN post" in prompt
    assert "standalone analysis" in prompt
    assert "general controversy" in prompt


def test_classifier_prompt_uses_comments_as_roundup_evidence_after_routing():
    item = candidate(
        "1",
        "Ask HN: What are you working on?",
        url="https://news.ycombinator.com/item?id=1",
        story_text="What projects or tools are you building?",
    )
    item.content_kind = "community_roundup"
    item.discussion_text = "I made a filesystem indexer that works offline."

    prompt = build_topic_classifier_prompt([item])
    payload = json.loads(prompt.split("Untrusted items:\n", 1)[1])

    assert payload == [{
        "id": "1",
        "source_question": {
            "title": "Ask HN: What are you working on?",
            "text": "What projects or tools are you building?",
        },
        "hn_comments": "I made a filesystem indexer that works offline.",
    }]
    assert "comments are the primary evidence" in prompt
    assert "question provides context only" in prompt
    assert "Do not infer their topic" in prompt
    assert "community_roundup:" not in prompt
