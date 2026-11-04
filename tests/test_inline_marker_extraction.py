import pytest

from daily_brief.article_fetcher import extract as extraction


@pytest.mark.parametrize("marker", [
    '<d-cite key="reference"></d-cite>',
    '<x-reference data-id="reference"></x-reference>',
    '<span class="citation-marker"></span>',
    '<x-reference hidden></x-reference>',
    '<span>  </span>',
    '<span><x-reference></x-reference></span>',
    '<x-reference></x-reference><d-cite></d-cite>',
])
def test_extract_html_preserves_explanation_after_empty_inline_marker(marker):
    markup = f"""<html><body><article>
      <p>The model repeats its answers{marker}. The authors removed a direction
      from the weights to reduce repeated reasoning.</p>
      <p>The evaluation measured whether the edited model finished its answers.</p>
    </article></body></html>"""

    text = extraction.extract_html(markup)

    space = " " if marker == '<span>  </span>' else ""
    assert " ".join(text.split()) == (
        f"The model repeats its answers{space}. The authors removed a direction "
        "from the weights to reduce repeated reasoning. "
        "The evaluation measured whether the edited model finished its answers."
    )


def test_extract_html_preserves_inline_order_and_paragraph_boundaries():
    markup = """<html><body><article>
      <p><x-reference></x-reference>First result: <b>the model
      <d-cite></d-cite>finished</b> its answer<span></span> correctly.</p>
      <p>A separate measurement checks whether the change generalizes.</p>
    </article></body></html>"""

    text = extraction.extract_html(markup)

    assert text.splitlines() == [
        "First result: the model finished its answer correctly.",
        "A separate measurement checks whether the change generalizes.",
    ]


def test_empty_marker_cleanup_keeps_non_content_filtering():
    markup = """<html><head><title>Local experiment</title></head><body><main>
      <article><p>The experiment examines repeated reasoning<x-ref></x-ref>.
      Its explanation remains part of the article.</p>
      <p>A second paragraph reports the evaluation and its limitations.</p>
      <script>SECRET_SCRIPT</script></article>
      <section class="comments"><p><x-ref></x-ref>SECRET_COMMENT</p></section>
      <nav><p><span></span>SECRET_NAVIGATION</p></nav>
    </main></body></html>"""

    text = extraction.extract_html(markup)

    assert "title: Local experiment" in text
    assert "Its explanation remains part of the article." in text
    assert "SECRET_" not in text


def test_empty_marker_cleanup_preserves_containers_and_nonempty_components():
    markup = """<html><body><article>
      <p>Visible <x-note class="comments">component text</x-note> tail.</p>
      <p><x-note><b>Formatted component</b></x-note> tail.</p>
      <x-widget></x-widget>
      <div hidden><p><x-ref></x-ref>HIDDEN_TEXT</p></div>
    </article></body></html>"""

    normalized = extraction._normalize_empty_inline_markers(markup)

    assert '<x-note class="comments">component text</x-note>' in normalized
    assert '<x-note><b>Formatted component</b></x-note>' in normalized
    assert '<x-widget></x-widget>' in normalized
    assert '<div hidden><p><x-ref></x-ref>HIDDEN_TEXT</p></div>' in normalized
    assert extraction._normalize_empty_inline_markers(normalized) == normalized


@pytest.mark.parametrize("hidden_attribute", [
    'hidden', 'aria-hidden="true"', 'style="display:none"',
    'style="visibility:hidden !important"',
])
def test_empty_marker_cleanup_does_not_expose_hidden_ancestor_text(hidden_attribute):
    markup = f"""<html><body><article>
      <p>The experiment examines repeated reasoning and explains the intervention.
      The authors describe how they changed the model and measured its answers.</p>
      <p>The evaluation reports both useful improvements and remaining limitations.</p>
      <div {hidden_attribute}><p><x-ref></x-ref>HIDDEN_TEXT</p></div>
    </article></body></html>"""

    text = extraction.extract_html(markup)

    assert "explains the intervention" in text
    assert "HIDDEN_TEXT" not in text


def test_empty_marker_cleanup_parser_failure_keeps_original(monkeypatch):
    markup = '<p>Before<x-ref></x-ref> after.</p>'

    def fail_to_parse(*args, **kwargs):
        raise extraction.etree.ParserError("parser limit reached")

    monkeypatch.setattr(extraction.lxml_html, "document_fromstring", fail_to_parse)

    assert extraction._normalize_empty_inline_markers(markup) == markup
