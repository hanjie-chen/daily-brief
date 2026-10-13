"""Empty page shells must not stop retrieval recovery (no live services)."""

import json
from datetime import UTC, datetime
from html import escape

import pytest
import trafilatura

from daily_brief.article_fetcher import (
    CLASSIFICATION_FETCH_POLICY,
    ArticleFetchError,
    extract_html,
    fetch_article,
    fetch_jina_reader_text,
)
from daily_brief.article_fetcher.content import is_page_shell
from test_article_fetcher import (
    FakeResponse, make_jina_payload, make_wayback_payload, resolver_for,
)


URL = "https://example.com/incident/42"
TITLE = "Example Cloud"
JS_BODY = (
    "Example Cloud\nYou have JavaScript disabled\n"
    "To use the site, you must enable JavaScript in the browser settings and reload the page."
)
NAV_BODY = (
    '[![Image 1: Example Cloud](https://example.com/logo.svg)]'
    '(https://example.com/ "Example Cloud")\n\n'
    "Region\n\nAll Russia Kazakhstan\n\nRegion"
)
FACT = "The storage API is unavailable in the western region."


def html_page(body):
    return (
        f"<html><head><title>{TITLE}</title>"
        '<meta name="description" content="Service availability and incident reports.">'
        "</head><body><main>"
        + "".join(f"<p>{escape(line)}</p>" for line in body.splitlines())
        + "</main></body></html>"
    )


def reader_response(body, title=TITLE):
    payload = json.loads(make_jina_payload(body, url=URL))
    payload["data"]["title"] = title
    return FakeResponse(
        json.dumps(payload).encode(), content_type="application/json",
        final_url=f"https://r.jina.ai/{URL}",
    )


@pytest.mark.parametrize("body", [
    JS_BODY,
    "You need to enable JavaScript to run this app.",
    "Please enable JavaScript. You have JavaScript disabled.",
    "This website requires JavaScript to function properly.",
    NAV_BODY,
    "Home | About us | Contact | Privacy policy",
    "Region All Russia Kazakhstan",
    "[Home](/)\n[Search](/search)\n[Log in](/login)",
    "Language\nEnglish Deutsch Français",
])
def test_reader_rejects_known_shells(body):
    with pytest.raises(ArticleFetchError) as caught:
        fetch_jina_reader_text(
            URL, opener=lambda *args, **kwargs: reader_response(body),
            resolver=resolver_for({}),
        )
    assert caught.value.error_code == "empty_content"
    assert caught.value.method == "jina"


@pytest.mark.parametrize("body", [
    FACT,
    "All systems operational.",
    "v2 is out.",
    JS_BODY + "\n" + FACT,
    NAV_BODY + "\n" + FACT,
    "JavaScript is disabled by default in our sandbox.",
    "The browser displays 'You have JavaScript disabled' when scripts are blocked.",
    'The message "Please enable JavaScript" is misleading.',
    "> Please enable JavaScript.",
    "`You have JavaScript disabled`",
    "```text\nYou have JavaScript disabled\n```",
    "# Enable JavaScript\nThe setting restores interactive charts.",
    "[An interesting project](/project)\n[Another useful project](/other)",
    "Region\nAll systems operational.",
    "Region\nUnknown option",
    "Home\nA short announcement.",
])
def test_reader_preserves_short_facts_and_ambiguous_or_substantive_material(body):
    result = fetch_jina_reader_text(
        URL, opener=lambda *args, **kwargs: reader_response(body),
        resolver=resolver_for({}),
    )
    assert result == body


@pytest.mark.parametrize("body", [JS_BODY, "Home\nAbout us\nContact\nPrivacy policy"])
def test_real_html_extractor_rejects_nonempty_shell_before_metadata(body):
    markup = html_page(body)
    # Regression must exercise a genuinely nonempty extraction, not mock it empty.
    assert trafilatura.extract(markup, favor_precision=True, include_comments=False)
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(markup.encode(), final_url=URL)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}), policy=CLASSIFICATION_FETCH_POLICY)
    assert caught.value.error_code == "empty_content"
    assert requests == [URL]


@pytest.mark.parametrize("body", [FACT, JS_BODY + "\n" + FACT, "Home\nContact\n" + FACT])
def test_real_html_extractor_keeps_short_body_and_publisher_metadata(body):
    result = extract_html(html_page(body))
    assert FACT in result
    assert "title: Example Cloud" in result
    assert "description: Service availability" in result


def test_js_shell_recovers_via_reader():
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == URL:
            return FakeResponse(html_page(JS_BODY).encode(), final_url=URL)
        assert request.full_url == f"https://r.jina.ai/{URL}"
        return reader_response(FACT)

    result = fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert result.text == FACT
    assert result.method == "jina"
    assert result.fallback_reason == "empty_content"
    assert result.attempts == len(requests) == 2


@pytest.mark.parametrize("archive_body", [FACT, JS_BODY, "Home\nAbout us\nContact"])
def test_reader_shell_continues_to_archive_and_archive_uses_same_check(archive_body):
    requests = []
    timestamp = "20261012060000"
    replay_url = f"https://web.archive.org/web/{timestamp}id_/{URL}"

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == URL:
            return FakeResponse(html_page(JS_BODY).encode(), final_url=URL)
        if request.full_url == f"https://r.jina.ai/{URL}":
            return reader_response(NAV_BODY)
        if request.full_url.startswith("https://web.archive.org/cdx/"):
            return FakeResponse(
                make_wayback_payload([timestamp, URL, "text/html", "200", "HASH", "512"]),
                content_type="application/json", final_url=request.full_url,
            )
        assert request.full_url == replay_url
        return FakeResponse(html_page(archive_body).encode(), final_url=replay_url)

    kwargs = dict(
        opener=opener, resolver=resolver_for({}),
        wayback_not_before=datetime(2026, 10, 12, tzinfo=UTC),
        wayback_not_after=datetime(2026, 10, 13, tzinfo=UTC),
    )
    if archive_body == FACT:
        result = fetch_article(URL, **kwargs)
        assert FACT in result.text
        assert result.method == "wayback"
        assert result.material_origin == "archived_copy"
        assert result.attempts == 4
        assert result.fallback_reason == "empty_content"
    else:
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(URL, **kwargs)
        assert caught.value.error_code == "wayback_empty_content"
        assert caught.value.method == "wayback"
        assert caught.value.fallback_attempted
        assert caught.value.fallback_reason == "empty_content"
        assert caught.value.attempts == 4
    assert len(requests) == 4


def test_title_alone_does_not_establish_shell_and_unknown_text_is_preserved():
    assert not is_page_shell("A brief fact", title="A brief fact")
    assert not is_page_shell("Home\n" + "A" * 513)
    assert is_page_shell("You have JavaScript disabled", title="You have JavaScript disabled")


@pytest.mark.parametrize("notice", ["Home", "Please enable JavaScript."])
@pytest.mark.parametrize("fact", ["All systems operational.", "v2 is out."])
def test_reader_preserves_fact_that_is_also_the_page_title(notice, fact):
    body = f"{notice}\n{fact}"
    assert fetch_jina_reader_text(
        URL, opener=lambda *args, **kwargs: reader_response(body, title=fact),
        resolver=resolver_for({}),
    ) == body


@pytest.mark.parametrize("channel", ["html", "reader"])
def test_size_limit_still_precedes_shell_rejection(channel):
    from daily_brief.article_fetcher import ArticleFetchPolicy

    with pytest.raises(ArticleFetchError) as caught:
        if channel == "reader":
            fetch_jina_reader_text(
                URL, opener=lambda *args, **kwargs: reader_response(JS_BODY),
                resolver=resolver_for({}), max_bytes=32,
            )
        else:
            fetch_article(
                URL, opener=lambda *args, **kwargs: FakeResponse(html_page(JS_BODY).encode(), final_url=URL),
                resolver=resolver_for({}), extracted_max_bytes=32,
                policy=ArticleFetchPolicy(jina_enabled=False, wayback_enabled=False),
            )
    assert caught.value.error_code in {"response_too_large", "extracted_content_too_large"}


def test_shells_exhaust_source_channels_then_attempt_search_and_report_no_body():
    from daily_brief.generation.material import prepare_candidate_material
    from daily_brief.models import Candidate, Story
    from daily_brief.time_window import TimeWindow

    requests = []
    searches = []
    timestamp = "20261012060000"
    replay = f"https://web.archive.org/web/{timestamp}id_/{URL}"

    class Finder:
        provider = "test"

        def __init__(self, name):
            self.name = name

        def find(self, candidate):
            searches.append(self.name)
            return []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == URL:
            return FakeResponse(html_page(JS_BODY).encode(), final_url=URL)
        if request.full_url == f"https://r.jina.ai/{URL}":
            return reader_response(NAV_BODY)
        if request.full_url.startswith("https://web.archive.org/cdx/"):
            return FakeResponse(
                make_wayback_payload([timestamp, URL, "text/html", "200", "HASH", "512"]),
                content_type="application/json", final_url=request.full_url,
            )
        assert request.full_url == replay
        return FakeResponse(html_page(JS_BODY).encode(), final_url=replay)

    candidate = Candidate(Story(
        "algolia", "123", "Storage service outage", URL,
        "https://news.ycombinator.com/item?id=123", "2026-10-12T01:00:00Z", 100, 20,
    ))
    window = TimeWindow(
        datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC), "2026-10-13",
    )
    available = prepare_candidate_material(
        candidate,
        lambda url, **kwargs: fetch_article(url, opener=opener, resolver=resolver_for({}), **kwargs),
        None, Finder("alternate"), window, same_article_finder=Finder("same_article"),
    )
    assert not available
    assert searches == ["same_article", "alternate"]
    assert len(requests) == 4
    assert candidate.story.fetched_text == ""
    assert candidate.article_retrieval.status == "failed"
    assert candidate.article_retrieval.fallback_reason == "empty_content"
    assert candidate.article_retrieval.error_code == "wayback_empty_content"
