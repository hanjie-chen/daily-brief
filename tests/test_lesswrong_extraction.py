from daily_brief.article_fetcher.extract import (
    _normalize_lesswrong_post_markup,
    extract_html,
)


def test_lesswrong_linkpost_keeps_introduction_article_code_and_metadata():
    markup = """<html><head><title>Alignment evals — LessWrong</title>
    <meta name="description" content="A linkpost introduction."></head><body><main>
      <div class="PostsPage-postContent content">
        <div class="LinkPostMessage-root">This is a linkpost for
          <a href="https://goodhartlabs.com/post">https://goodhartlabs.com/post</a>
        </div>
        <div class="commentOnSelection"><div id="postContent">
          <p>The article explains a careful alignment evaluation.</p>
          <pre><code>python3 arena.py start
python3 arena.py move e2e4</code></pre>
          <p>The result measures whether models alter the board state. It is worth being
            <span class=""><span></span>skeptical that the reported evaluations matter</span>.
          </p>
        </div></div>
      </div>
      <section class="Comments-root"><p>COMMENT_TEXT_SHOULD_NOT_APPEAR</p></section>
    </main></body></html>"""

    text = extract_html(markup)

    assert "title: Alignment evals — LessWrong" in text
    assert "description: A linkpost introduction." in text
    assert "This is a linkpost for https://goodhartlabs.com/post" in text
    assert "The article explains a careful alignment evaluation." in text
    assert "python3 arena.py start\npython3 arena.py move e2e4" in text
    assert "skeptical that the reported evaluations matter" in text
    assert "COMMENT_TEXT_SHOULD_NOT_APPEAR" not in text


def test_lesswrong_normalization_skips_missing_post_body_marker():
    markup = """<html><body><div class="PostsPage-postContent">
      <div class="commentOnSelection"><p>Ordinary page content.</p></div>
    </div></body></html>"""

    assert _normalize_lesswrong_post_markup(markup) == markup


def test_lesswrong_normalization_skips_ambiguous_post_page_markers():
    markup = """<html><body>
      <div class="PostsPage-postContent"><div class="commentOnSelection">
        <div id="postContent"><p>First article.</p></div>
      </div></div>
      <div class="PostsPage-postContent"><div class="commentOnSelection">
        <div id="postContent"><p>Second article.</p></div>
      </div></div>
    </body></html>"""

    assert _normalize_lesswrong_post_markup(markup) == markup
