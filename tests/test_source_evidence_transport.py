import gzip
import json
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest

from daily_brief.article_fetcher import fetch_article
from daily_brief.article_fetcher.wayback import _WaybackCapture, _fetch_wayback_capture


SOURCE = 'https://origin.example/article'
COPY = 'https://copy.example/article'
MARKUP = f'''<html><body><article><header><h1>Article title</h1>
<p>Cross-posted from <a href="{SOURCE}">Origin</a></p></header>
<p>A careful test measures how well agents follow instructions during a chess game.</p>
<p>The experiment exposes an engine socket and observes which agents use it.</p>
<p>The authors describe limitations and explain how to reproduce their evaluation.</p>
</article></body></html>'''.encode()


class Response(BytesIO):
    def __init__(self, data, url, content_type='text/html', encoding=None):
        super().__init__(data)
        self.url = url
        self.headers = Message()
        self.headers['Content-Type'] = content_type + '; charset=utf-8'
        if encoding:
            self.headers['Content-Encoding'] = encoding

    def geturl(self):
        return self.url


def resolver(host, port, type):
    return [(2, type, 6, '', ('93.184.216.34', port))]


@pytest.mark.parametrize('transport', ['direct', 'jina'])
def test_fetch_retains_actual_source_evidence_through_transport(transport):
    calls = []
    def opener(request, **kwargs):
        calls.append(request.full_url)
        if request.full_url == COPY:
            if transport == 'direct':
                return Response(MARKUP, COPY)
            headers = Message()
            headers['x-vercel-mitigated'] = 'challenge'
            raise HTTPError(COPY, 429, 'Blocked', headers, None)
        assert request.full_url == 'https://r.jina.ai/' + COPY
        return Response(json.dumps({'code': 200, 'status': 20000, 'data': {
            'httpStatus': 200, 'url': COPY, 'title': 'Article title',
            'content': f'Cross-posted from [Origin]({SOURCE})\n\nArticle body.',
        }}).encode(), request.full_url, 'application/json')
    result = fetch_article(COPY, opener=opener, resolver=resolver)
    assert result.method == transport
    assert result.source_evidence.title == 'Article title'
    assert result.source_evidence.relations[0].url == SOURCE
    assert len(calls) == (1 if transport == 'direct' else 2)


def test_gzip_wayback_preserves_relative_source_relation_against_original_page():
    timestamp = '20260914085721'
    replay = f'https://web.archive.org/web/{timestamp}id_/{COPY}'
    result = _fetch_wayback_capture(
        _WaybackCapture(timestamp, COPY), source_url=COPY,
        opener=lambda *a, **k: Response(
            gzip.compress(MARKUP.replace(SOURCE.encode(), b'/original')),
            replay, encoding='gzip',
        ),
        resolver=resolver, timeout_seconds=1, html_max_bytes=8192,
        pdf_max_bytes=8192, extracted_max_bytes=8192, pdf_max_pages=1,
        pdf_parse_timeout_seconds=1, pdf_address_space_bytes=1024 * 1024,
    )
    assert result.material_origin == 'archived_copy'
    assert result.source_evidence.relations[0].url == 'https://copy.example/original'
