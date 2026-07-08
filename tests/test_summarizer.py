from daily_brief.models import Candidate, KeywordMatch, Story
from daily_brief.summarizer import CodexSummarizer, fallback_summary


def candidate(story_text: str = "A demo of an AI coding agent.", fetched_text: str = ""):
    return Candidate(
        story=Story(
            source="test",
            hn_item_id="1",
            title="AI coding agent",
            source_url="https://example.com",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-07-08T00:00:00+08:00",
            points=30,
            comments=5,
            story_text=story_text,
            fetched_text=fetched_text,
        ),
        matched_keywords=[
            KeywordMatch(
                keyword="AI coding",
                weight="high",
                source="title",
                bonus=4.0,
                start=0,
                end=9,
            )
        ],
    )


def test_fallback_summary_uses_title_and_stats():
    text = fallback_summary(candidate())

    assert "AI coding agent" in text
    assert "30 points" in text
    assert "5 comments" in text
    assert "摘要生成失败" in text


def test_codex_summarizer_builds_prompt_and_returns_stdout(monkeypatch):
    calls = {}

    def fake_run(args, input, text, capture_output, timeout, check):
        calls["args"] = args
        calls["input"] = input
        calls["text"] = text
        calls["capture_output"] = capture_output
        calls["timeout"] = timeout
        calls["check"] = check

        class Result:
            stdout = " 中文摘要\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    summary = CodexSummarizer(timeout_seconds=5).summarize(candidate())

    assert summary == "中文摘要"
    assert calls["args"][:3] == ["codex", "exec", "--ephemeral"]
    assert calls["text"] is True
    assert calls["capture_output"] is True
    assert calls["timeout"] == 5
    assert calls["check"] is True
    assert "AI coding agent" in calls["input"]
    assert "https://example.com" in calls["input"]
    assert "https://news.ycombinator.com/item?id=1" in calls["input"]
    assert "30" in calls["input"]
    assert "5" in calls["input"]
    assert "AI coding" in calls["input"]
    assert "A demo of an AI coding agent." in calls["input"]
    assert "中文" in calls["input"]


def test_codex_summarizer_prompt_uses_fetched_text_when_story_text_is_empty(monkeypatch):
    calls = {}

    def fake_run(args, input, text, capture_output, timeout, check):
        calls["input"] = input

        class Result:
            stdout = "摘要"

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    CodexSummarizer().summarize(candidate(story_text="", fetched_text="Fetched article text."))

    assert "Fetched article text." in calls["input"]
