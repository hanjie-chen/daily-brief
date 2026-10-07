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


def test_publication_metadata_is_distinct_from_modified_date():
    evidence = extract_html_source_evidence('''<html><head>
      <meta property="article:modified_time" content="2026-10-07T12:00:00Z">
      <meta property="article:published_time" content="2026-10-06T09:00:00Z">
    </head><body><article><h1>News</h1></article></body></html>''',
    'https://publisher.test/news')
    assert evidence.published_at == '2026-10-06T09:00:00Z'


def test_publication_metadata_accepts_explicit_name_and_itemprop():
    for attribute, name in [('name', 'pubdate'), ('itemprop', 'datePublished')]:
        evidence = extract_html_source_evidence(
            f'<html><head><meta {attribute}="{name}" content="2026-10-06"></head></html>',
            'https://publisher.test/news',
        )
        assert evidence.published_at == '2026-10-06'


def test_publication_date_accepts_article_jsonld_graph():
    evidence = extract_html_source_evidence('''<html><head>
    <script type="application/ld+json">{"@graph":[
      {"@type":"WebSite", "datePublished":"2000-01-01"},
      {"@type":["Article", "NewsArticle"], "datePublished":"2026-10-06",
       "dateModified":"2026-10-07"}]}</script></head></html>''',
    'https://publisher.test/news')
    assert evidence.published_at == '2026-10-06'


def test_publication_date_accepts_header_time_and_explicit_article_time():
    for markup in [
        '<article><header><p>Published <time datetime="2026-10-06">October 6</time></p></header></article>',
        '<article><time itemprop="datePublished" datetime="2026-10-06">October 6</time>'
        '<time itemprop="dateModified" datetime="2026-10-07">Updated October 7</time></article>',
    ]:
        assert extract_html_source_evidence(markup, 'https://publisher.test/news').published_at == '2026-10-06'


def test_publication_date_does_not_use_unrelated_or_updated_dates():
    markup = '''<html><head><meta name="dateModified" content="2026-10-07">
    <script type="application/ld+json">{"@type":"WebSite","datePublished":"2000-01-01"}</script>
    </head><body><nav><time datetime="2026-10-07">Today</time></nav>
    <article><header><p>Updated <time datetime="2026-10-07">October 7</time></p></header>
    <p>Historical event: <time datetime="1999-01-01">January 1</time></p>
    <div class="related"><time itemprop="datePublished" datetime="2000-01-01">Related</time></div>
    </article></body></html>'''
    assert extract_html_source_evidence(markup, 'https://publisher.test/news').published_at == ''


def test_publication_date_ignores_nested_recommendations_and_malformed_jsonld():
    markup = '''<html><head>
    <script type="application/ld+json">{invalid JSON}</script>
    <script type="application/ld+json">{"@type":"NewsArticle","dateModified":"2026-10-07",
    "related":{"@type":"NewsArticle","datePublished":"2000-01-01"}}</script>
    </head></html>'''
    assert extract_html_source_evidence(markup, 'https://publisher.test/news').published_at == ''


def test_publication_date_is_bounded_and_markdown_requires_explicit_metadata():
    evidence = extract_html_source_evidence(
        '<html><head><meta name="pubdate" content="' + 'x' * 65 + '"></head></html>',
        'https://publisher.test/news',
    )
    assert evidence.published_at == ''
    assert extract_markdown_source_evidence(
        '# Article\nPublished: 2026-10-06', 'https://publisher.test/news',
    ).published_at == ''
    assert extract_markdown_source_evidence(
        '# Article', 'https://publisher.test/news', published_at=' 2026-10-06 ',
    ).published_at == '2026-10-06'
