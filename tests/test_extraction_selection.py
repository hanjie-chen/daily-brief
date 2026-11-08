"""Disjoint article candidates must not silently select unrelated recommendations."""

from html import escape
from pathlib import Path

from lxml import html
import pytest

from daily_brief.article_fetcher import (
    ArticleFetchPolicy,
    ArticleFetchError,
    extract_html,
    fetch_article,
)
from daily_brief.article_fetcher import extract as extraction
from test_article_fetcher import FakeResponse, make_jina_payload, resolver_for


TITLE = "Database journal recovery"
PRIMARY = "Distant planets orbit bright stars while telescopes collect images for astronomers."
ALTERNATIVE = "Database journal recovery restores committed transactions after a storage device fails."
MARKUP = f"<html><head><title>{TITLE}</title></head><body><p>Page content.</p></body></html>"


def candidates(monkeypatch, primary=PRIMARY, alternative=ALTERNATIVE, cleaned=ALTERNATIVE):
    """Control extraction candidates while exercising the public composition path."""
    calls = []

    def extract(markup, **kwargs):
        calls.append((markup, kwargs))
        return primary if len(calls) == 1 else cleaned

    monkeypatch.setattr(extraction.trafilatura, "extract", extract)
    monkeypatch.setattr(
        extraction, "try_readability",
        lambda document: html.fromstring(f"<div><p>{escape(alternative)}</p></div>"),
    )
    return calls


def test_disjoint_candidate_with_better_title_coverage_is_cleaned_once(monkeypatch):
    cleaned = "Database journal recovery completed without losing committed transactions."
    calls = candidates(monkeypatch, cleaned=cleaned)

    text = extract_html(MARKUP)

    assert text.endswith("Extracted body:\n" + cleaned)
    assert "title: " + TITLE in text
    assert PRIMARY not in text
    assert ALTERNATIVE not in text
    assert len(calls) == 2
    assert ALTERNATIVE in calls[1][0]
    assert calls[1][1] == {
        "include_comments": False, "favor_precision": True, "include_tables": True,
    }


@pytest.mark.parametrize("primary,alternative", [
    (PRIMARY, "Distant galaxies emit light while observatories measure seasonal temperature changes."),
    ("Database engines write durable records while workers coordinate concurrent updates.",
     "Database servers handle queries using indexes and carefully tuned memory pools."),
    (ALTERNATIVE, PRIMARY),
    # More title words do not justify replacing a shorter excerpt of the same body.
    (PRIMARY, PRIMARY + " " + ALTERNATIVE),
    (PRIMARY, ""),
    (PRIMARY, "Database journal"),
    ("All systems operational.", ALTERNATIVE),
])
def test_absent_short_overlapping_or_no_better_candidate_keeps_primary(monkeypatch, primary, alternative):
    calls = candidates(monkeypatch, primary, alternative, cleaned="SHOULD_NOT_BE_USED")

    text = extract_html(MARKUP)

    assert text.endswith("Extracted body:\n" + primary)
    assert len(calls) == 1


@pytest.mark.parametrize("tail_length,replace", [(9, False), (10, True)])
def test_four_word_overlap_boundary_uses_smaller_set(monkeypatch, tail_length, replace):
    # Exactly one shared four-word phrase: 1/10 is retained, 1/11 is replaced.
    shared = "commonalpha commonbeta commongamma commondelta "
    primary = shared + " ".join(f"planet{i}" for i in range(tail_length))
    alternative = shared + "journal " + " ".join(f"record{i}" for i in range(tail_length - 1))
    calls = candidates(monkeypatch, primary, alternative, cleaned=ALTERNATIVE)

    text = extract_html(MARKUP)

    assert text.endswith("Extracted body:\n" + (ALTERNATIVE if replace else primary))
    assert len(calls) == (2 if replace else 1)


@pytest.mark.parametrize("head", ["", "<title></title>", "<title>AI — 2</title>"])
def test_missing_meaningful_title_keeps_original_without_comparison(monkeypatch, head):
    calls = candidates(monkeypatch)

    def unexpected(document):
        pytest.fail("No title evidence: do not ask Readability to choose the article")

    monkeypatch.setattr(extraction, "try_readability", unexpected)
    text = extract_html(f"<html><head>{head}</head><body><p>Content.</p></body></html>")

    assert text.endswith(PRIMARY)
    assert len(calls) == 1


def test_title_is_not_added_to_body_before_comparison(monkeypatch):
    calls = candidates(monkeypatch)
    text = extract_html(MARKUP.replace(
        "</head>", f'<meta name="description" content="{ALTERNATIVE}"></head>',
    ))
    assert text.endswith("Extracted body:\n" + ALTERNATIVE)
    assert "description: " + ALTERNATIVE in text
    assert len(calls) == 2


def test_same_article_in_adjacent_blocks_is_not_mistaken_for_disjoint_text(monkeypatch):
    primary = "Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima."
    calls = candidates(monkeypatch, primary)
    alternative = html.fromstring(
        "<div><p>Alpha bravo charlie delta</p><p>echo foxtrot golf hotel</p>"
        "<p>india juliet kilo lima.</p><p>Database journal recovery details.</p></div>"
    )
    before = html.tostring(alternative)
    monkeypatch.setattr(extraction, "try_readability", lambda document: alternative)

    assert extract_html(MARKUP).endswith(primary)
    assert html.tostring(alternative) == before
    assert len(calls) == 1


def test_comparison_keeps_inline_words_together(monkeypatch):
    primary = "Database journal recovery restores committed transactions after storage failure."
    # An extra title word creates the opportunity to switch if inline markup
    # falsely destroys otherwise shared four-word phrases.
    calls = candidates(monkeypatch, primary)
    alternative = html.fromstring(
        "<div><p>Data<b>base</b> jour<i>nal</i> reco<em>very</em> restores "
        "committed transactions after storage failure.</p><p>Verification follows.</p></div>"
    )
    monkeypatch.setattr(extraction, "try_readability", lambda document: alternative)

    assert extract_html(MARKUP.replace(TITLE, TITLE + " verification")).endswith(primary)
    assert len(calls) == 1


def test_register_layout_recovers_shorter_article_instead_of_recommendations():
    # Authored prose in the misleading layout: the correct article is shorter
    # than the unrelated page-content lists, so the library keeps those lists.
    markup = (Path(__file__).parent / "fixtures/article_extraction/disjoint-recommendations.html").read_text()
    old_body = extraction.trafilatura.extract(
        markup, include_comments=False, favor_precision=True, include_tables=True,
    )
    assert "harbor" in old_body
    assert "lunar observatory" not in old_body
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(markup.encode())

    result = fetch_article(
        "https://example.com/article", opener=opener, resolver=resolver_for({}),
    )
    assert "lunar observatory completed its mirror calibration" in result.text
    assert "harbor" not in result.text
    assert "title: Lunar observatory mirror calibration succeeds" in result.text
    assert result.method == "direct"
    assert result.extractor == "trafilatura"
    assert requests == ["https://example.com/article"]


def test_rejected_linodelike_controls_do_not_fall_back_to_primary_or_metadata(monkeypatch):
    # The secondary text can mention the page title yet consist only of controls.
    # An empty cleanup is a retrieval failure, never permission to use raw text.
    primary = "We deployed network rules that drop malformed packets on public interfaces."
    calls = candidates(
        monkeypatch, primary, "Database journal recovery alerts: select country and telephone area code.",
        cleaned="",
    )
    assert extract_html(MARKUP) == ""
    assert len(calls) == 2


def test_unreadable_optional_candidate_preserves_primary(monkeypatch):
    candidates(monkeypatch)
    monkeypatch.setattr(extraction, "try_readability", lambda document: html.HtmlElement())
    assert extract_html(MARKUP).endswith(PRIMARY)


@pytest.mark.parametrize("recovery_enabled", [True, False])
def test_empty_cleanup_obeys_explicit_recovery_policy(monkeypatch, recovery_enabled):
    candidates(monkeypatch, cleaned="")
    requests = []
    url = "https://example.com/article"

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == url:
            return FakeResponse(MARKUP.encode(), final_url=url)
        assert request.full_url == "https://r.jina.ai/" + url
        return FakeResponse(
            make_jina_payload(ALTERNATIVE), content_type="application/json",
            final_url=request.full_url,
        )

    kwargs = {"opener": opener, "resolver": resolver_for({})}
    if not recovery_enabled:
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(
                url, policy=ArticleFetchPolicy(jina_enabled=False, wayback_enabled=False), **kwargs,
            )
        assert caught.value.error_code == "empty_content"
        assert requests == [url]
    else:
        result = fetch_article(url, **kwargs)
        assert result.text == ALTERNATIVE
        assert result.method == "jina"
        assert result.fallback_reason == "empty_content"
        assert len(requests) == 2


def test_selected_cleaned_body_still_obeys_size_limit(monkeypatch):
    candidates(monkeypatch, cleaned=ALTERNATIVE * 20)
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(
            "https://example.com/article",
            opener=lambda *args, **kwargs: FakeResponse(MARKUP.encode()),
            resolver=resolver_for({}), extracted_max_bytes=300,
        )
    assert caught.value.error_code == "extracted_content_too_large"
