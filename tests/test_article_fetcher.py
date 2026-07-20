from email.message import Message

import pytest

from daily_brief.article_fetcher import ArticleFetchError, extract_html, fetch_article_text


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
