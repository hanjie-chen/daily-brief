import json

from daily_brief.models import Candidate, Story
from daily_brief.topic_classifier import (
    TOPIC_CLASSIFIER_STORY_TEXT_MAX_CHARS,
    build_topic_classifier_prompt,
)


def candidate(
    item_id: str,
    title: str,
    url: str = "https://example.com/article",
    story_text: str = "",
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
        )
    )


def test_classifier_prompt_contains_only_bounded_model_inputs():
    prompt = build_topic_classifier_prompt(
        [candidate("1", "Qwen 3.8"), candidate("2", "SQLite release")]
    )

    assert "Qwen 3.8" in prompt
    assert "SQLite release" in prompt
    assert "example.com" in prompt
    assert "/article" not in prompt
    assert "untrusted" in prompt.lower()


def test_classifier_prompt_includes_normalized_bounded_story_text_excerpt():
    long_text = "  AI agents\ncan help  " + (
        "x" * TOPIC_CLASSIFIER_STORY_TEXT_MAX_CHARS
    )

    prompt = build_topic_classifier_prompt(
        [candidate("1", "An ambiguous title", story_text=long_text)]
    )
    payload = json.loads(prompt.split("Untrusted items:\n", 1)[1])
    excerpt = payload[0]["story_text_excerpt"]

    assert excerpt.startswith("AI agents can help ")
    assert "\n" not in excerpt
    assert len(excerpt) == TOPIC_CLASSIFIER_STORY_TEXT_MAX_CHARS
