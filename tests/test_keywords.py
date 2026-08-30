from pathlib import Path

import pytest

from daily_brief.config import CORE_TOPIC_HIGH_WEIGHT_KEYWORDS
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


@pytest.mark.parametrize(
    ("keyword", "title"),
    [
        ("GPT", "GPT5 release"),
        ("GPT", "GPT5.6 release"),
        ("GPT", "GPT-6 release"),
        ("GPT", "GPTs in production"),
        ("Qwen", "Qwen2.5 release"),
        ("Qwen", "Qwen3.8 Max is out"),
        ("Grok", "Grok3 is available"),
    ],
)
def test_versioned_keywords_accept_version_and_plural_suffixes(keyword, title):
    assert keyword in names(match_keywords(title=title, story_text="", url=""))


@pytest.mark.parametrize(
    "title",
    [
        "AIRBNB update",
        "GPTsomething release",
        "GPT5.6preview release",
        "Qwensomething release",
        "Grokking algorithms",
    ],
)
def test_variant_suffixes_do_not_relax_letter_boundaries(title):
    assert not {"AI", "GPT", "Qwen", "Grok"} & set(
        names(match_keywords(title=title, story_text="", url=""))
    )


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


@pytest.mark.parametrize(
    "title",
    [
        "Rust never sleeps: preventing rust on garden tools",
        "Here we go again with another framework",
        "C is fine, actually",
        "A kernel of truth in the popcorn industry",
        "Shell announces new offshore project",
        "Taylor Swift ticket pricing analysis",
        "A python ate my chickens",
        "Docker workers begin strike at port",
        "The compiler of a folk-song anthology wins an award",
        "Elixir sales rise at the wellness expo",
        "Cassandra predicts another difficult year",
        "Terraform Mars in this new board game",
        "A court interpreter describes the trial",
        "The city expands garbage collection",
        "A Taylor Swift package includes four vinyl records",
        "GCC leaders meet to discuss regional trade",
        "Clang went the bells throughout the night",
        "Learn to zig when your opponent expects a zag",
        "The neighborhood Bash returns this weekend",
    ],
)
def test_ambiguous_core_topic_terms_do_not_match(title):
    matches = match_keywords(title=title, story_text="", url="")

    assert not any(match.weight == "high" for match in matches)


@pytest.mark.parametrize("keyword", CORE_TOPIC_HIGH_WEIGHT_KEYWORDS)
def test_approved_core_topic_keywords_are_high_weight(keyword):
    matches = match_keywords(
        title=f"Technical notes on {keyword}",
        story_text="",
        url="",
    )

    assert any(
        match.keyword == keyword and match.weight == "high"
        for match in matches
    )


def test_runtime_core_topic_keywords_match_approved_manifest():
    manifest_path = (
        Path(__file__).parents[1] / "docs" / "keyword-entry-manifest.md"
    )
    rows = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        rows.append(
            {
                "keyword": cells[0].removeprefix("`").removesuffix("`"),
                "tier": cells[1],
                "hits": int(cells[2]),
                "decision": cells[3],
                "reason": cells[4],
            }
        )

    assert [row["keyword"] for row in rows] == CORE_TOPIC_HIGH_WEIGHT_KEYWORDS
    assert len(rows) == 99
    assert all(row["tier"] in {"A", "B"} for row in rows)
    assert all(row["decision"] == "批准" for row in rows)
    assert all(row["reason"] for row in rows)

    zero_hit_plural_keywords = {
        "GCC compilers",
        "Clang compilers",
        "Linux kernels",
        "bytecode interpreters",
        "type systems",
        "borrow checkers",
        "goroutines",
        "kernel panics",
        "kernel modules",
        "POSIX shells",
        "Bash scripts",
        "JDKs",
        "compiler optimizations",
    }
    uniform_reason = "单数已过关，复数误命中风险不高于单数。"
    assert {
        row["keyword"]
        for row in rows
        if row["hits"] == 0 and row["reason"] == uniform_reason
    } == zero_hit_plural_keywords
