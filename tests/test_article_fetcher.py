from email.message import Message
from urllib.error import HTTPError

import pytest

from daily_brief.article_fetcher import (
    ArticleFetchError,
    extract_html,
    fetch_article,
    fetch_article_text,
    fetch_jina_reader_text,
)


PUBLIC_ADDRESS = "93.184.216.34"


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_type: str = "text/html",
        charset: str = "utf-8",
        final_url: str = "https://example.com/article",
    ):
        self.payload = payload
        self.final_url = final_url
        self.headers = Message()
        self.headers["Content-Type"] = f"{content_type}; charset={charset}"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, amount):
        return self.payload[:amount]

    def geturl(self):
        return self.final_url


def http_error(url, code, **headers):
    response_headers = Message()
    for name, value in headers.items():
        response_headers[name.replace("_", "-")] = value
    return HTTPError(url, code, "request failed", response_headers, None)


def resolver_for(addresses):
    def resolve(host, port, type):
        address = addresses.get(host, PUBLIC_ADDRESS)
        return [(2, type, 6, "", (address, port))]

    return resolve


def test_extract_html_removes_non_content_and_collapses_whitespace():
    markup = """
    <html><head><style>hidden</style></head><body>
      <article>Hello <b>world</b></article>
      <script>bad()</script><noscript>fallback</noscript>
    </body></html>
    """

    assert extract_html(markup) == "Hello world"


def test_fetch_article_text_extracts_html_from_public_url():
    response = FakeResponse(b"<article>Useful <b>facts</b>.</article>")

    text = fetch_article_text(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert text == "Useful facts."


def test_fetch_article_reports_direct_retrieval_method():
    response = FakeResponse(b"Direct facts.", content_type="text/plain")

    result = fetch_article(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.text == "Direct facts."
    assert result.method == "direct"
    assert result.fallback_reason == ""


def test_fetch_article_text_decodes_plain_text():
    response = FakeResponse("中文正文".encode(), content_type="text/plain")

    text = fetch_article_text(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert text == "中文正文"


@pytest.mark.parametrize(
    "url,address",
    [
        ("ftp://example.com/file", PUBLIC_ADDRESS),
        ("http://127.0.0.1/private", "127.0.0.1"),
        ("http://router.local/private", "192.168.1.1"),
        ("http://metadata.internal/private", "169.254.169.254"),
    ],
)
def test_fetch_article_text_rejects_unsafe_destinations(url, address):
    def fail_if_opened(request, timeout):
        raise AssertionError("unsafe URL should not be opened")

    with pytest.raises(ArticleFetchError, match="safe public HTTP"):
        fetch_article_text(
            url,
            opener=fail_if_opened,
            resolver=resolver_for({"127.0.0.1": address, "router.local": address, "metadata.internal": address}),
        )


def test_fetch_article_text_revalidates_final_redirect_url():
    response = FakeResponse(b"secret", final_url="http://127.0.0.1/private")

    with pytest.raises(ArticleFetchError, match="safe public HTTP"):
        fetch_article_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({"127.0.0.1": "127.0.0.1"}),
        )


def test_fetch_article_text_rejects_non_text_content():
    response = FakeResponse(b"%PDF", content_type="application/pdf")

    with pytest.raises(ArticleFetchError, match="content type"):
        fetch_article_text(
            "https://example.com/file.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )


def test_fetch_article_text_rejects_oversized_content():
    response = FakeResponse(b"x" * 11, content_type="text/plain")

    with pytest.raises(ArticleFetchError, match="too large"):
        fetch_article_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            max_bytes=10,
        )


def test_fetch_article_text_uses_jina_for_cloudflare_challenge(caplog):
    requests = []
    jina_response = FakeResponse(
        b"Luna costs 80% less.",
        content_type="text/plain",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    def open_response(request, timeout):
        requests.append((request, timeout))
        if len(requests) == 1:
            raise http_error(
                request.full_url, 403, server="cloudflare", cf_mitigated="challenge"
            )
        return jina_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        text = fetch_article_text(
            "https://example.com/article",
            opener=open_response,
            resolver=resolver_for({}),
            timeout_seconds=7,
        )

    assert text == "Luna costs 80% less."
    assert [request.full_url for request, _ in requests] == [
        "https://example.com/article",
        "https://r.jina.ai/https://example.com/article",
    ]
    assert [timeout for _, timeout in requests] == [7, 7]
    assert requests[1][0].get_header("Accept") == "text/plain"
    assert requests[1][0].get_header("X-cache-tolerance") == "300"
    assert "method=direct status=cloudflare_challenge fallback=jina" in caplog.text
    assert "method=jina status=success" in caplog.text


def test_fetch_article_reports_jina_retrieval_method():
    requests = []
    jina_response = FakeResponse(
        b"Luna costs 80% less.",
        content_type="text/plain",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    def open_response(request, timeout):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise http_error(request.full_url, 403, cf_mitigated="challenge")
        return jina_response

    result = fetch_article(
        "https://example.com/article",
        opener=open_response,
        resolver=resolver_for({}),
    )

    assert result.text == "Luna costs 80% less."
    assert result.method == "jina"
    assert result.fallback_reason == "cloudflare_challenge"


@pytest.mark.parametrize("status_code", [403, 404])
def test_fetch_article_text_does_not_use_jina_for_other_http_errors(status_code):
    requested_urls = []

    def deny(request, timeout):
        requested_urls.append(request.full_url)
        raise http_error(request.full_url, status_code, server="cloudflare")

    with pytest.raises(
        ArticleFetchError, match="direct article request failed"
    ) as caught:
        fetch_article_text(
            "https://example.com/article",
            opener=deny,
            resolver=resolver_for({}),
        )

    assert requested_urls == ["https://example.com/article"]
    assert caught.value.error_code == f"http_{status_code}"
    assert caught.value.method == "direct"


def test_fetch_article_text_reports_direct_and_jina_failures():
    def fail(request, timeout):
        if request.full_url.startswith("https://r.jina.ai/"):
            raise http_error(request.full_url, 502)
        raise http_error(request.full_url, 403, cf_mitigated="challenge")

    with pytest.raises(
        ArticleFetchError,
        match=(
            "article retrieval failed: direct=cloudflare challenge; "
            "jina=Jina Reader request failed"
        ),
    ) as caught:
        fetch_article_text(
            "https://example.com/article",
            opener=fail,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "http_502"
    assert caught.value.method == "jina"
    assert caught.value.fallback_attempted is True
    assert caught.value.fallback_reason == "cloudflare_challenge"


def test_fetch_article_text_rejects_empty_content_with_stable_error_code():
    response = FakeResponse(b"   ", content_type="text/plain")

    with pytest.raises(ArticleFetchError, match="no visible text") as caught:
        fetch_article_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "empty_content"
    assert caught.value.method == "direct"


def test_fetch_jina_reader_text_reuses_response_size_limit():
    response = FakeResponse(
        b"x" * 11,
        content_type="text/plain",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    with pytest.raises(ArticleFetchError, match="too large"):
        fetch_jina_reader_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            max_bytes=10,
        )
