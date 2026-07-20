import pytest

from daily_brief.models import Candidate, Story
from daily_brief.topic_classifier import CodexTopicClassifier


def candidate(item_id: str, title: str, url: str = "https://example.com/article") -> Candidate:
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
        )
    )


def test_classifier_returns_only_ids_from_supplied_batch(monkeypatch):
    calls = []

    def fake_run(args, input, text, capture_output, timeout, check):
        calls.append((args, input, text, capture_output, timeout, check))

        class Result:
            stdout = '["1", "unknown"]\n'

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = CodexTopicClassifier(timeout_seconds=12).classify(
        [candidate("1", "Qwen 3.8"), candidate("2", "SQLite release")]
    )

    assert result == {"1"}
    assert len(calls) == 1
    args, prompt, text, capture_output, timeout, check = calls[0]
    assert args[:3] == ["codex", "exec", "--ephemeral"]
    assert "--skip-git-repo-check" in args
    assert "--sandbox" in args
    assert "read-only" in args
    assert "--cd" in args
    assert args[args.index("--cd") + 1] not in {"", "."}
    assert "Qwen 3.8" in prompt
    assert "SQLite release" in prompt
    assert "example.com" in prompt
    assert "/article" not in prompt
    assert "untrusted" in prompt.lower()
    assert text is True
    assert capture_output is True
    assert timeout == 12
    assert check is True


def test_classifier_does_not_invoke_codex_for_empty_batch(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("codex should not run")

    monkeypatch.setattr("subprocess.run", fail_if_called)

    assert CodexTopicClassifier().classify([]) == set()


@pytest.mark.parametrize("stdout", ["", "not json", "{}", '[1, "2"]'])
def test_classifier_rejects_empty_or_invalid_output(monkeypatch, stdout):
    def fake_run(*args, **kwargs):
        class Result:
            pass

        result = Result()
        result.stdout = stdout
        return result

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="topic classifier"):
        CodexTopicClassifier().classify([candidate("1", "Qwen 3.8")])
