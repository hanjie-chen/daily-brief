from daily_brief.article_fetcher.source_evidence import (
    SourceRelation,
    extract_html_source_evidence,
    extract_markdown_source_evidence,
)


def test_goodhart_header_exposes_crosspost_identity_evidence():
    markup = '''<html><head><link rel="canonical" href="/blog/example"></head><body>
    <article><header><h1>Astra and Fable still hack on simple variants of alignment evals from 2025</h1>
    <p>7 September 2026 <!-- -->· Dean Valentine · Cross-posted from
    <a href="https://www.lesswrong.com/posts/munJKF7iWMsWJLAH2">LessWrong</a></p>
    </header><div><p>Synthetic article body.</p></div></article></body></html>'''

    evidence = extract_html_source_evidence(
        markup, "https://goodhartlabs.com/blog/frontier-models-still-hack-alignment-evals"
    )

    assert evidence.title == (
        "Astra and Fable still hack on simple variants of alignment evals from 2025"
    )
    assert evidence.author == "Dean Valentine"
    assert SourceRelation(
        url="https://www.lesswrong.com/posts/munJKF7iWMsWJLAH2",
        context=(
            "7 September 2026 · Dean Valentine · Cross-posted from LessWrong"
        ),
        kind="crosspost",
    ) in evidence.relations
    assert any(relation.kind == "canonical" for relation in evidence.relations)


def test_evidence_rejects_relation_text_in_comments_navigation_and_article_quotes():
    markup = """
    <html><head><title>Actual title</title></head><body>
      <nav>Cross-posted from <a href="https://bad.example/nav">Bad</a></nav>
      <article><h1>Actual title</h1><div class="article-body">
        <blockquote>Cross-posted from <a href="https://bad.example/quote">Bad</a></blockquote>
        <p>Article content.</p>
      </div></article>
      <section class="comments">Originally published at <a href="https://bad.example/comment">Bad</a></section>
    </body></html>
    """

    evidence = extract_html_source_evidence(markup, "https://publisher.example/post")

    assert evidence.title == "Actual title"
    assert evidence.relations == ()


def test_markdown_evidence_requires_an_explicit_nearby_relation_link():
    evidence = extract_markdown_source_evidence(
        "# Reposted title\n\nCross-posted from [Original site](https://origin.example/post).",
        "https://publisher.example/post",
        author="A. Writer",
    )

    assert evidence.title == "Reposted title"
    assert evidence.author == "A. Writer"
    assert evidence.relations == (
        SourceRelation(
            url="https://origin.example/post",
            context="Cross-posted from Original site.",
            kind="crosspost",
        ),
    )


def test_relation_does_not_attach_to_an_unrelated_header_link():
    evidence = extract_html_source_evidence('''<article><header><h1>Title</h1>
    <p>Cross-posted from <a href="https://original.test/a">Original</a>;
    more on <a href="https://unrelated.test/b">Other</a></p>
    </header></article>''', 'https://publisher.test/a')
    assert [r.url for r in evidence.relations] == ['https://original.test/a']


def test_youtube_narration_description_can_use_plain_url():
    evidence = extract_markdown_source_evidence(
        '', 'https://youtube.com/watch?v=abcdefghijk', title='Title',
        description='Narration of https://origin.test/article\nMy channel description.',
    )
    assert evidence.relations[0].kind == 'narration'
    assert evidence.relations[0].url == 'https://origin.test/article'


def test_markdown_quotes_and_comment_sections_are_not_identity_evidence():
    evidence = extract_markdown_source_evidence(
        '# Title\n> Cross-posted from [X](https://origin.test/a)\n'
        '## Comments\nCross-posted from [Y](https://origin.test/b)',
        'https://publisher.test/a',
    )
    assert not evidence.relations
