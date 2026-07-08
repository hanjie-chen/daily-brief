from __future__ import annotations

import subprocess

from .models import Candidate


class CodexSummarizer:
    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = timeout_seconds

    def summarize(self, candidate: Candidate) -> str:
        result = subprocess.run(
            ["codex", "exec", "--ephemeral", "Write a concise Chinese summary for this Hacker News item."],
            input=_build_prompt(candidate),
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=True,
        )
        return result.stdout.strip()


def fallback_summary(candidate: Candidate) -> str:
    return (
        f"{candidate.story.title}。"
        f"当前 HN 热度为 {candidate.story.points} points / {candidate.story.comments} comments；"
        "摘要生成失败时保留此基础信息。"
    )


def _build_prompt(candidate: Candidate) -> str:
    keywords = ", ".join(match.keyword for match in candidate.matched_keywords) or "none"
    body = candidate.story.story_text or candidate.story.fetched_text or "(not available)"
    return f"""请用中文写 1-2 句话摘要，说明这条 Hacker News 内容为什么值得看。

Title: {candidate.story.title}
Source URL: {candidate.story.source_url}
HN Discussion: {candidate.story.hn_discussion_url}
Points: {candidate.story.points}
Comments: {candidate.story.comments}
Matched keywords: {keywords}
Story text:
{body}
"""
