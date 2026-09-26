import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from daily_brief.article_fetcher import ArticleFetchResult
from daily_brief.models import Candidate, Story
from daily_brief.recovery.same_article import (
    HN_DOMAIN,
    MAX_SAME_ARTICLE_CANDIDATES,
    SameArticleCandidate,
    SameArticleFinderError,
    TavilySameArticleFinder,
    normalize_candidate_url,
    same_source_url,
    validate_same_article,
)
from daily_brief.recovery.tavily import TAVILY_MAX_RESPONSE_BYTES

LESSWRONG_URL = "https://www.lesswrong.com/posts/F7WmSWLJAHZ/"
GOODHART_URL = "https://goodhartlabs.com/blog/frontier-models-still-hack-alignment-evals"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body.read(size)


def source() -> Candidate:
    return Candidate(story=Story(
        source="algolia", hn_item_id="1",
        title="Astra and Fable still hack on simple variants of alignment evals from 2025",
        source_url=LESSWRONG_URL,
        hn_discussion_url="https://news.ycombinator.com/item?id=1",
        created_at="2026-09-15T00:00:00Z", points=1, comments=1,
    ))


def result(*, title=None, relations=(), text=None, method="direct"):
    body = text or "\n\n".join([
        "The experiment gives each model a chess task and exposes an engine socket. " * 5,
        "Researchers test whether the instruction against cheating transfers to a nearby variant. " * 5,
        "The results show the new models still use the prohibited interface in this evaluation. " * 5,
    ])
    evidence = SimpleNamespace(
        title=title or source().story.title + " | Goodhart Labs",
        author="Dean", relations=relations,
    )
    return ArticleFetchResult(text=body, method=method, source_evidence=evidence)


def test_finder_uses_only_title_and_bounded_hn_excluded_search():
    requests = []
    def opener(request, timeout):
        requests.append((request, timeout))
        return FakeResponse(json.dumps({"results": [{"title": "Goodhart", "url": GOODHART_URL}]}).encode())
    found = TavilySameArticleFinder(api_key="secret", opener=opener).find(source())
    assert found == [SameArticleCandidate("Goodhart", GOODHART_URL)]
    request, timeout = requests[0]
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 10
    assert json.loads(request.data) == {
        "query": source().story.title, "search_depth": "basic", "topic": "general",
        "max_results": MAX_SAME_ARTICLE_CANDIDATES, "include_answer": False,
        "include_raw_content": False, "include_images": False,
        "exclude_domains": [HN_DOMAIN], "auto_parameters": False, "exact_match": False,
    }


def test_finder_fails_closed_and_bounds_results():
    with pytest.raises(SameArticleFinderError, match="not configured") as caught:
        TavilySameArticleFinder(api_key="").find(source())
    assert caught.value.error_code == "not_configured"
    finder = TavilySameArticleFinder(api_key="secret", opener=lambda *args, **kwargs: FakeResponse(b"x" * (TAVILY_MAX_RESPONSE_BYTES + 1)))
    with pytest.raises(SameArticleFinderError) as caught:
        finder.find(source())
    assert caught.value.error_code == "response_too_large"
    items = [{"title": str(i), "url": f"https://example{i}.com/a"} for i in range(12)]
    bounded = TavilySameArticleFinder(api_key="secret", opener=lambda *args, **kwargs: FakeResponse(json.dumps({"results": items}).encode()))
    assert len(bounded.find(source())) == MAX_SAME_ARTICLE_CANDIDATES


@pytest.mark.parametrize("url", ["file:///tmp/a", "https://x:y@example.com/a", "http://127.0.0.1/a", "https://[::1]/a"])
def test_url_normalizer_rejects_unsafe_inputs(url):
    assert normalize_candidate_url(url) is None


def test_url_normalizer_and_lesswrong_identity_are_conservative():
    assert normalize_candidate_url("HTTPS://Example.COM:443/a#part") == "https://example.com/a"
    assert same_source_url(LESSWRONG_URL, "https://lesswrong.com/posts/F7WmSWLJAHZ/another-slug")
    assert not same_source_url(LESSWRONG_URL, "https://lesswrong.com/posts/different/")
    assert not same_source_url("https://example.com/one", "https://example.com/one/more")


def test_validator_accepts_real_shaped_crosspost_only_with_fetched_evidence():
    relation = SimpleNamespace(kind="crosspost", url=LESSWRONG_URL, context="Cross-posted from LessWrong")
    checked = validate_same_article(source(), SameArticleCandidate("untrusted title", GOODHART_URL), result(relations=(relation,)))
    assert checked.accepted is True
    assert checked.reason == "verified"
    assert checked.evidence == (f"crosspost:{LESSWRONG_URL}:Cross-posted from LessWrong",)


@pytest.mark.parametrize("relations, text, reason", [
    ((), None, "missing_explicit_source_relation"),
    ((SimpleNamespace(kind="canonical", url=LESSWRONG_URL, context=""),), None, "missing_explicit_source_relation"),
    ((SimpleNamespace(kind="crosspost", url="https://lesswrong.com/posts/wrong/", context=""),), None, "missing_explicit_source_relation"),
    ((SimpleNamespace(kind="crosspost", url=LESSWRONG_URL, context=""),), "Read the full article. " * 100, "teaser_content"),
    ((SimpleNamespace(kind="crosspost", url=LESSWRONG_URL, context=""),), "Useful short text", "body_too_short"),
])
def test_validator_rejects_bare_links_teasers_and_wrong_post(relations, text, reason):
    checked = validate_same_article(source(), SameArticleCandidate("x", GOODHART_URL), result(relations=relations, text=text))
    assert checked.accepted is False
    assert checked.reason == reason


def test_validator_rejects_eink_style_summary_and_wrong_title():
    relation = SimpleNamespace(kind="crosspost", url=LESSWRONG_URL, context="Cross-posted")
    summary = "This post summarizes the article and links back to the original. " * 40
    assert validate_same_article(source(), SameArticleCandidate("x", GOODHART_URL), result(relations=(relation,), text=summary)).reason == "summary_or_aggregator"
    assert validate_same_article(source(), SameArticleCandidate("x", GOODHART_URL), result(title="A different post", relations=(relation,))).reason == "page_title_mismatch"


def test_validator_accepts_youtube_only_when_narration_is_explicit():
    narration = SimpleNamespace(kind="narration", url=LESSWRONG_URL, context="Narration of the LessWrong post")
    checked = validate_same_article(source(), SameArticleCandidate("x", "https://youtube.com/watch?v=a"), result(relations=(narration,), method="youtube_caption"))
    assert checked.accepted is True
    assert checked.evidence[0].startswith("narration:")
