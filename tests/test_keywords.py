import pytest

from daily_brief.keywords import match_keywords


def names(matches):
    return [match.keyword for match in matches]


def test_keyword_matching_uses_boundaries():
    text = "Medieval-style fortifications and storage systems"

    matches = match_keywords(title=text, story_text="", url="")

    assert "eval" not in names(matches)
    assert "RAG" not in names(matches)


def test_abbreviations_require_uppercase_standalone_tokens():
    assert "AI" in names(match_keywords(title="How AI is reshaping education", story_text="", url=""))
    assert "AI" not in names(match_keywords(title="How ai is reshaping education", story_text="", url=""))
    assert "RAG" not in names(match_keywords(title="average storage wins", story_text="", url=""))


def test_abbreviations_accept_plural_suffixes():
    assert "LLM" in names(match_keywords(title="Protecting our commons from LLMs", story_text="", url=""))
    assert "GPU" in names(match_keywords(title="Two GPUs for local inference", story_text="", url=""))


@pytest.mark.parametrize("title", ["GPT5 release", "GPT5.6 release", "GPT-6 release", "GPTs in production"])
def test_gpt_accepts_version_and_plural_suffixes(title):
    assert "GPT" in names(match_keywords(title=title, story_text="", url=""))


@pytest.mark.parametrize("title", ["AIRBNB update", "GPTsomething release", "GPT5.6preview release"])
def test_variant_suffixes_do_not_relax_letter_boundaries(title):
    assert not {"AI", "GPT"} & set(names(match_keywords(title=title, story_text="", url="")))


def test_longest_match_wins_avoids_repeated_short_token_scores():
    matches = match_keywords(title="AI coding agent built with Claude", story_text="", url="")

    assert "AI coding" in names(matches)
    assert "coding agent" in names(matches)
    assert "Claude" in names(matches)
    assert "AI" not in names(matches)
    assert "agent" not in names(matches)


def test_url_tokens_are_weak_only():
    matches = match_keywords(title="A normal release note", story_text="", url="https://example.com/developer/tools")

    assert [(match.keyword, match.weight) for match in matches] == [("developer tools", "weak")]


def test_current_model_and_product_names_are_non_weak_ai_signals():
    titles = [
        "GPT-5.6 release",
        "OpenAI Codex Micro",
        "Qwen 3.8",
        "The Kimi K3 Moment",
        "Grok Build is open source",
        "DeepSeek releases a model",
        "Claude Fable 5",
        "Moonshot launches a service",
        "An open-weights model",
    ]

    for title in titles:
        matches = match_keywords(title=title, story_text="", url="")

        assert any(match.weight != "weak" for match in matches), title


def test_ambiguous_product_names_are_case_sensitive():
    matches = match_keywords(
        title="A moonshot fable helps readers grok a difficult idea",
        story_text="",
        url="",
    )

    assert not {"Moonshot", "Fable", "Grok"} & set(names(matches))
