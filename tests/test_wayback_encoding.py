import gzip
from email.message import Message
from io import BytesIO

import pytest

from daily_brief.article_fetcher.contracts import ArticleFetchError
from daily_brief.article_fetcher.responses import _read_wayback_response
from daily_brief.article_fetcher.wayback import _WaybackCapture, _fetch_wayback_capture


class Response(BytesIO):
    def __init__(self, payload, encoding="gzip"):
        super().__init__(payload)
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        if encoding is not None:
            self.headers["Content-Encoding"] = encoding

    def geturl(self):
        return "https://web.archive.org/web/20260914085721id_/https://example.com/article"


@pytest.mark.parametrize("encoding", [None, "identity", "gzip", " GZip "])
def test_wayback_decodes_supported_encodings_at_exact_limit(encoding):
    text = b"article text " * 100
    payload = (
        gzip.compress(text)
        if encoding and encoding.strip().lower() == "gzip"
        else text
    )
    assert _read_wayback_response(Response(payload, encoding), len(text)) == text


@pytest.mark.parametrize("encoding", ["br", "deflate", "gzip, br"])
def test_wayback_rejects_unknown_or_stacked_encodings(encoding):
    with pytest.raises(ArticleFetchError) as caught:
        _read_wayback_response(Response(b"unused", encoding), 1024)
    assert caught.value.error_code == "wayback_unsupported_content_encoding"
    assert encoding in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"not gzip",
        gzip.compress(b"article")[:-3],
        gzip.compress(b"article")[:-8] + b"\0" * 8,
    ],
)
def test_wayback_rejects_invalid_truncated_and_bad_checksum_gzip(payload):
    with pytest.raises(ArticleFetchError) as caught:
        _read_wayback_response(Response(payload), 1024)
    assert caught.value.error_code == "wayback_invalid_content_encoding"


def test_wayback_limits_compressed_bytes():
    with pytest.raises(ArticleFetchError) as caught:
        _read_wayback_response(Response(gzip.compress(b"article")), 10)
    assert caught.value.error_code == "response_too_large"


@pytest.mark.parametrize("members", [1, 2])
def test_wayback_limits_total_decompressed_bytes(members):
    payload = gzip.compress(b"x" * 1024) * members
    assert len(payload) < 100
    with pytest.raises(ArticleFetchError) as caught:
        _read_wayback_response(Response(payload), 100)
    assert caught.value.error_code == "response_too_large"


def test_wayback_still_rejects_challenge_after_gzip_decode():
    payload = gzip.compress(
        b"<html><body><h1>Vercel Security Checkpoint</h1>"
        b"<p>Verifying your browser</p></body></html>"
    )
    with pytest.raises(ArticleFetchError) as caught:
        _fetch_wayback_capture(
            _WaybackCapture("20260914085721", "https://example.com/article"),
            source_url="https://example.com/article",
            opener=lambda *args, **kwargs: Response(payload),
            resolver=lambda host, port, type: [(2, type, 6, "", ("93.184.216.34", port))],
            timeout_seconds=1,
            html_max_bytes=4096,
            pdf_max_bytes=4096,
            extracted_max_bytes=4096,
            pdf_max_pages=1,
            pdf_parse_timeout_seconds=1,
            pdf_address_space_bytes=1024 * 1024,
        )
    assert caught.value.error_code == "wayback_challenge_page"
