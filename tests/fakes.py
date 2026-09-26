"""Fakes and story builders shared by the CLI and generation tests."""

from daily_brief.article_fetcher import ArticleFetchError
from daily_brief.llm.gemini_backend import GeminiAPIError
from daily_brief.models import Story
from daily_brief.recovery import SyndicatedFinderError


class FakeSummarizer:
    last_summary_provider_status = "completed"
    last_summary_usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "thought_tokens": 60,
        "total_tokens": 180,
    }

    def __init__(self):
        self.titles = []

    def summarize(self, candidate):
        self.titles.append(candidate.story.title)
        return f"Summary for {candidate.story.title}"


class RaisingSummarizer:
    name = "test-provider"
    summarizer_model = "test-summary-model"
    last_summary_attempts = 4

    def summarize(self, candidate):
        raise GeminiAPIError(
            "quota reached",
            error_code="quota_exceeded",
            http_status=429,
        )


class MixedScriptSummarizer:
    def summarize(self, candidate):
        return "  Anthropic发布Claude 5模型。\n"


class CapturingSummarizer:
    def __init__(self):
        self.fetched_texts = []
        self.summary_modes = []

    def summarize(self, candidate):
        self.fetched_texts.append(candidate.story.fetched_text)
        self.summary_modes.append(candidate.summary_mode)
        return "Captured summary"


class FakeClassifier:
    def __init__(self, decisions=None, default_label="outside"):
        self.decisions = dict(decisions or {})
        self.default_label = default_label
        self.seen_ids = []

    def classify(self, candidates):
        item_ids = [candidate.story.hn_item_id for candidate in candidates]
        self.seen_ids.extend(item_ids)
        return {
            item_id: self.decisions.get(item_id, self.default_label)
            for item_id in item_ids
        }


class FakeModelBackend(FakeSummarizer, FakeClassifier):
    name = "fake"

    def __init__(self):
        FakeSummarizer.__init__(self)
        FakeClassifier.__init__(self)


class FakeGeminiBackendFactory:
    @classmethod
    def from_environment(cls, **kwargs):
        return FakeModelBackend()


class RaisingClassifier:
    def classify(self, candidates):
        raise RuntimeError("classifier unavailable")


class FakeSyndicatedFinder:
    provider = "fake"

    def __init__(self, results):
        self.results = results
        self.calls = []

    def find(self, candidate):
        self.calls.append(candidate.story.hn_item_id)
        return self.results


class RaisingSyndicatedFinder:
    provider = "fake"

    def find(self, candidate):
        raise SyndicatedFinderError(
            "provider unavailable",
            error_code="provider_request_failed",
        )


class FakeAlternateReportingFinder:
    provider = "fake-alternate"

    def __init__(self, results):
        self.results = results
        self.calls = []

    def find(self, candidate):
        self.calls.append(candidate.story.hn_item_id)
        return self.results


def nytimes_anthropic_url():
    return (
        "https://www.nytimes.com/2026/08/27/technology/"
        "anthropic-government-blacklisting-ruling.html"
    )


def yahoo_anthropic_url():
    return (
        "https://ca.finance.yahoo.com/news/"
        "us-judge-rules-pentagon-blacklisting-012047911.html"
    )


def anthropic_nytimes_story():
    return Story(
        source="algolia",
        hn_item_id="49473522",
        title=(
            "Judge rules Trump administration’s blacklisting of Anthropic "
            "was illegal"
        ),
        source_url=nytimes_anthropic_url(),
        hn_discussion_url="https://news.ycombinator.com/item?id=49473522",
        created_at="2026-08-28T02:00:00Z",
        points=500,
        comments=300,
    )


def origin_block_failure(fallback_reason, *, method="jina"):
    return ArticleFetchError(
        "origin blocked; retrieval fallbacks failed",
        error_code="http_403",
        method=method,
        extractor=method,
        fallback_attempted=True,
        fallback_reason=fallback_reason,
        attempts=3,
    )


def alternate_reporting_body(*, extra_filler=0):
    beginning = (
        "Aug 28 (Reuters) - A U.S. judge ruled that the Pentagon's blacklisting "
        "of Anthropic was unlawful. The court found the government action "
        "violated the First Amendment as illegal retaliation and denied the "
        "company required Fifth Amendment process. "
    )
    details = "Additional grounded reporting detail. " * 8
    footer = "(Reporting by Christian Martinez and Jasper Ward)"
    return beginning + details + ("x" * extra_filler) + footer


def reuters_story_url():
    return (
        "https://www.reuters.com/business/"
        "nvidia-scales-back-250-billion-openai-data-center-guarantee-"
        "wsj-reports-2026-08-14/"
    )


def yahoo_story_url():
    return (
        "https://finance.yahoo.com/technology/ai/articles/"
        "nvidia-scales-back-250-billion-234356524.html"
    )


def datadome_jina_failure(*, attempts=2):
    return ArticleFetchError(
        "Reuters blocked; Jina failed",
        error_code="http_403",
        method="jina",
        extractor="jina",
        fallback_attempted=True,
        fallback_reason="datadome_challenge",
        attempts=attempts,
    )


def verified_reuters_copy_body():
    facts = (
        "Aug 14 (Reuters) - Nvidia scaled back the amount of financing it may "
        "guarantee for OpenAI's Ohio data center from a previously discussed "
        "$250 billion to less than $120 billion. Investors were concerned about "
        "Nvidia's exposure, while OpenAI discussed leases for the complete 10GW "
        "project. "
    )
    return facts + ("Additional grounded reporting detail. " * 20)


def story(
    item_id,
    title,
    *,
    source="algolia",
    points=30,
    comments=5,
    story_text="",
    url=None,
):
    return Story(
        source=source,
        hn_item_id=str(item_id),
        title=title,
        source_url=url or f"https://example.com/{item_id}",
        hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
        created_at="2026-07-08T00:00:00+08:00",
        points=points,
        comments=comments,
        story_text=story_text,
    )
