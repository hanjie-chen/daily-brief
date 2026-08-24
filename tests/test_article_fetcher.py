import json
import ssl
import subprocess
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from daily_brief import article_fetcher
from daily_brief.article_fetcher import (
    ArticleFetchError,
    _create_public_connection,
    _validate_public_http_url,
    extract_html,
    fetch_article,
    fetch_article_text,
    fetch_github_readme_text,
    fetch_jina_reader_text,
)
from daily_brief.youtube_captions import YoutubeCaptionResult


PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def disable_adobe_pdf_api_by_default(monkeypatch):
    monkeypatch.delenv("PDF_SERVICES_CLIENT_ID", raising=False)
    monkeypatch.delenv("PDF_SERVICES_CLIENT_SECRET", raising=False)


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


def tls_verification_error(code, message):
    reason = ssl.SSLCertVerificationError(1, message)
    reason.verify_code = code
    reason.verify_message = message
    return URLError(reason)


def resolver_for(addresses):
    def resolve(host, port, type):
        address = addresses.get(host, PUBLIC_ADDRESS)
        return [(2, type, 6, "", (address, port))]

    return resolve


def make_jina_payload(
    content="Luna costs 80% less.",
    *,
    code=200,
    status=200,
    http_status=200,
    url="https://example.com/article",
):
    return json.dumps(
        {
            "code": code,
            "status": status,
            "data": {
                "httpStatus": http_status,
                "url": url,
                "content": content,
            },
        },
        ensure_ascii=False,
    ).encode()


def make_wayback_payload(*rows):
    return json.dumps(
        [
            [
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
                "length",
            ],
            *rows,
        ]
    ).encode()


def make_pdf(*page_texts):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for page_text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        if page_text is not None:
            escaped = (
                page_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            )
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_extract_html_removes_non_content_and_collapses_whitespace():
    markup = """
    <html><head><style>hidden</style></head><body>
      <article>Hello <b>world</b></article>
      <script>bad()</script><noscript>fallback</noscript>
    </body></html>
    """

    assert extract_html(markup) == "Hello world"


def test_extract_html_preserves_content_block_boundaries():
    markup = """
    <html><body><article>
      <h1>Useful facts</h1>
      <p>The first paragraph explains the mechanism in useful detail.</p>
      <h2>What to do</h2>
      <ul><li>Keep the context focused.</li><li>Clear stale output.</li></ul>
    </article></body></html>
    """

    assert extract_html(markup).splitlines() == [
        "Useful facts",
        "The first paragraph explains the mechanism in useful detail.",
        "What to do",
        "- Keep the context focused.",
        "- Clear stale output.",
    ]


def test_extract_html_preserves_preformatted_code_indentation():
    markup = """
    <html><body><article>
      <h1>Code example</h1>
      <p>This article explains the following implementation in useful detail.</p>
      <pre><code>def main():
    if x:
        return 1</code></pre>
      <p>The function returns one when the condition is true.</p>
    </article></body></html>
    """

    assert "def main():\n    if x:\n        return 1" in extract_html(markup)


def test_extract_html_preserves_nested_list_indentation():
    markup = """
    <html><body><article>
      <h1>Nested checklist</h1>
      <p>This article explains a nested checklist in useful detail.</p>
      <ul>
        <li>Top item<ul><li>Nested item</li></ul></li>
        <li>Second top</li>
      </ul>
      <p>The hierarchy is part of the article's meaning.</p>
    </article></body></html>
    """

    assert "- Top item\n  - Nested item\n- Second top" in extract_html(markup)


def test_fetch_article_text_extracts_html_from_public_url():
    response = FakeResponse(
        b"""
        <html><body><article><h1>Useful facts</h1>
        <p>This grounded article explains the first important fact in detail.</p>
        <p>It also supplies enough context for reliable local extraction.</p>
        </article></body></html>
        """
    )

    text = fetch_article_text(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert "Useful facts" in text
    assert "reliable local extraction" in text


def test_fetch_article_reports_direct_retrieval_method():
    response = FakeResponse(
        b"Direct facts.",
        content_type="text/plain",
        final_url="https://example.com/final-article",
    )

    result = fetch_article(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.text == "Direct facts."
    assert result.method == "direct"
    assert result.extractor == "plain_text"
    assert result.fallback_reason == ""
    assert result.attempts == 1
    assert result.retrieved_url == "https://example.com/final-article"


def test_fetch_article_routes_target_youtube_video_to_caption_extractor(monkeypatch):
    fetched_urls = []

    def fetch_caption(url, **kwargs):
        fetched_urls.append((url, kwargs))
        return YoutubeCaptionResult(
            text="The interview argues that AI infrastructure demand is concentrated.",
            language="en-orig",
            generated=True,
        )

    monkeypatch.setattr(article_fetcher, "fetch_youtube_caption", fetch_caption)

    result = fetch_article(
        "https://www.youtube.com/watch?v=68X8yEatepQ",
        resolver=resolver_for({"www.youtube.com": PUBLIC_ADDRESS}),
    )

    assert result.text.startswith("The interview argues")
    assert result.method == "youtube_caption"
    assert result.extractor == "yt_dlp"
    assert fetched_urls == [
        (
            "https://www.youtube.com/watch?v=68X8yEatepQ",
            {"max_text_bytes": 256 * 1024},
        )
    ]


def test_html_larger_than_old_limit_is_extracted_with_separate_raw_limit():
    body = (
        "The local extractor keeps this grounded article body and its useful facts. "
        * 12
    )
    markup = (
        "<html><head><style>"
        + ("x" * (300 * 1024))
        + "</style></head><body><article><h1>Local extraction</h1><p>"
        + body
        + "</p></article></body></html>"
    ).encode()
    response = FakeResponse(markup)

    result = fetch_article(
        "https://example.com/large-page",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert len(markup) > 256 * 1024
    assert result.method == "direct"
    assert result.extractor == "trafilatura"
    assert "Local extraction" in result.text
    assert "grounded article body" in result.text
    assert "x" * 100 not in result.text


def test_html_over_separate_raw_limit_fails_before_extraction(monkeypatch):
    response = FakeResponse(b"<html>" + (b"x" * 101) + b"</html>")
    extracted = []
    monkeypatch.setattr(
        "daily_brief.article_fetcher.extract_html",
        lambda markup: extracted.append(markup) or "unexpected",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/too-large",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            html_max_bytes=100,
        )

    assert caught.value.error_code == "response_too_large"
    assert extracted == []


def test_html_extraction_excludes_page_chrome_scripts_and_comments():
    body = (
        "The article body contains grounded reporting about a useful technical "
        "result, with enough detail for extraction. " * 10
    )
    markup = f"""
    <html><head><style>STYLE_SECRET</style></head><body>
      <nav>LOGIN_NAVIGATION_SECRET</nav>
      <main><article><h1>Grounded report</h1><p>{body}</p></article></main>
      <div class="comments"><h2>Comments</h2>
        <p>COMMENT_THREAD_SECRET repeated reader discussion.</p>
      </div>
      <script>SCRIPT_SECRET</script>
    </body></html>
    """

    text = extract_html(markup)

    assert "Grounded report" in text
    assert "grounded reporting" in text
    assert "LOGIN_NAVIGATION_SECRET" not in text
    assert "COMMENT_THREAD_SECRET" not in text
    assert "STYLE_SECRET" not in text
    assert "SCRIPT_SECRET" not in text


@pytest.mark.parametrize("provider_status", [200, 20000])
def test_empty_trafilatura_result_uses_jina_once(monkeypatch, provider_status, caplog):
    monkeypatch.setattr(
        "daily_brief.article_fetcher.trafilatura.extract", lambda *args, **kwargs: None
    )
    direct_response = FakeResponse(
        b"<html><body><nav>Navigation must not become article text.</nav></body></html>"
    )
    jina_response = FakeResponse(
        make_jina_payload("Grounded Jina article facts.", status=provider_status),
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/no-article",
    )
    requests = []

    def open_response(request, timeout):
        requests.append(request)
        return direct_response if len(requests) == 1 else jina_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            "https://example.com/no-article",
            opener=open_response,
            resolver=resolver_for({}),
        )

    assert [request.full_url for request in requests] == [
        "https://example.com/no-article",
        "https://r.jina.ai/https://example.com/no-article",
    ]
    assert result.text == "Grounded Jina article facts."
    assert result.method == "jina"
    assert result.extractor == "jina"
    assert result.fallback_reason == "empty_content"
    assert (
        "method=direct extractor=trafilatura status=empty_content fallback=jina"
        in caplog.text
    )
    assert (
        "method=jina extractor=jina status=success fallback_reason=empty_content"
        in caplog.text
    )


@pytest.mark.parametrize(
    ("source_url", "direct_final_url"),
    [
        (
            "https://openreview.net/challenge?redirect=%2Fforum%3Fid%3Dpaper-id",
            "https://openreview.net/challenge?redirect=%2Fforum%3Fid%3Dpaper-id",
        ),
        (
            "https://openreview.net/forum?id=paper-id",
            "https://openreview.net/challenge?redirect=%2Fforum%3Fid%3Dpaper-id",
        ),
    ],
)
def test_challenge_url_uses_jina_once(source_url, direct_final_url, caplog):
    direct_response = FakeResponse(
        b"<html><body>Challenge shell</body></html>",
        final_url=direct_final_url,
    )
    jina_response = FakeResponse(
        make_jina_payload(
            "Grounded OpenReview paper facts.",
            url="https://openreview.net/forum?id=paper-id",
        ),
        content_type="application/json",
        final_url=f"https://r.jina.ai/{source_url}",
    )
    requests = []

    def open_response(request, timeout):
        requests.append(request.full_url)
        return direct_response if len(requests) == 1 else jina_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            source_url,
            opener=open_response,
            resolver=resolver_for({}),
        )

    assert requests == [source_url, f"https://r.jina.ai/{source_url}"]
    assert result.text == "Grounded OpenReview paper facts."
    assert result.method == "jina"
    assert result.extractor == "jina"
    assert result.fallback_reason == "challenge_page"
    assert "method=direct status=challenge_page fallback=jina" in caplog.text


def test_http_200_cloudflare_challenge_header_uses_jina_once():
    direct_response = FakeResponse(b"<html><body>Challenge shell</body></html>")
    direct_response.headers["cf-mitigated"] = "challenge"
    jina_response = FakeResponse(
        make_jina_payload("Grounded article facts."),
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )
    responses = iter((direct_response, jina_response))

    result = fetch_article(
        "https://example.com/article",
        opener=lambda request, timeout: next(responses),
        resolver=resolver_for({}),
    )

    assert result.text == "Grounded article facts."
    assert result.method == "jina"
    assert result.fallback_reason == "challenge_page"


def test_turnstile_html_uses_jina_once():
    direct_response = FakeResponse(
        b"""
        <html><body>
          <div class="cf-turnstile"></div>
          <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
        </body></html>
        """
    )
    jina_response = FakeResponse(
        make_jina_payload("Grounded article facts."),
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )
    responses = iter((direct_response, jina_response))

    result = fetch_article(
        "https://example.com/article",
        opener=lambda request, timeout: next(responses),
        resolver=resolver_for({}),
    )

    assert result.text == "Grounded article facts."
    assert result.method == "jina"
    assert result.fallback_reason == "challenge_page"


def test_nonempty_challenge_content_from_direct_and_jina_is_a_fetch_failure():
    direct_response = FakeResponse(
        b"""
        <html><body><main>
          <h1>Verifying your browser</h1>
          <p>Complete the check below to continue to OpenReview.</p>
          <p>Please complete the verification above.</p>
        </main></body></html>
        """,
    )
    jina_response = FakeResponse(
        make_jina_payload(
            "Complete the check below to continue to OpenReview. "
            "Please complete the verification above.",
            url="https://openreview.net/forum?id=paper-id",
        ),
        content_type="application/json",
        final_url="https://r.jina.ai/https://openreview.net/forum?id=paper-id",
    )
    requests = []

    def open_response(request, timeout):
        requests.append(request.full_url)
        return direct_response if len(requests) == 1 else jina_response

    with pytest.raises(
        ArticleFetchError,
        match=(
            "direct=browser verification challenge page; "
            "jina=Jina Reader returned a browser verification challenge page"
        ),
    ) as caught:
        fetch_article(
            "https://openreview.net/forum?id=paper-id",
            opener=open_response,
            resolver=resolver_for({}),
        )

    assert requests == [
        "https://openreview.net/forum?id=paper-id",
        "https://r.jina.ai/https://openreview.net/forum?id=paper-id",
    ]
    assert caught.value.error_code == "challenge_page"
    assert caught.value.method == "jina"
    assert caught.value.extractor == "jina"
    assert caught.value.fallback_attempted is True
    assert caught.value.fallback_reason == "challenge_page"


def test_article_discussing_browser_verification_is_not_a_challenge_page():
    response = FakeResponse(
        b"""
        <html><body><article>
          <h1>Designing browser verification</h1>
          <p>This article compares several verification mechanisms.</p>
          <p>It explains their implementation and usability tradeoffs.</p>
        </article></body></html>
        """
    )

    result = fetch_article(
        "https://example.com/browser-verification",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.method == "direct"
    assert "verification mechanisms" in result.text


def test_html_extracted_text_has_its_own_limit(monkeypatch):
    monkeypatch.setattr(
        "daily_brief.article_fetcher.extract_html", lambda markup: "grounded " * 20
    )
    response = FakeResponse(b"<html><article>small response</article></html>")

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/long-result",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            extracted_max_bytes=50,
        )

    assert caught.value.error_code == "extracted_content_too_large"
    assert caught.value.extractor == "trafilatura"


def test_html_extraction_exception_does_not_use_jina(monkeypatch):
    def fail_extraction(*args, **kwargs):
        raise ValueError("broken parser")

    monkeypatch.setattr(
        "daily_brief.article_fetcher.trafilatura.extract", fail_extraction
    )
    response = FakeResponse(b"<html><article>Facts</article></html>")
    requested_urls = []

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/article",
            opener=lambda request, timeout: (
                requested_urls.append(request.full_url) or response
            ),
            resolver=resolver_for({}),
        )

    assert requested_urls == ["https://example.com/article"]
    assert caught.value.error_code == "html_extraction_failed"
    assert caught.value.method == "direct"
    assert caught.value.extractor == "trafilatura"
    assert caught.value.fallback_attempted is False


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/drumih/turbo-fieldfare",
        "https://github.com/drumih/turbo-fieldfare/",
        "https://github.com/drumih/turbo-fieldfare.git?tab=readme#usage",
    ],
)
def test_fetch_article_uses_github_readme_api_for_repository_root(url):
    requests = []
    response = FakeResponse(
        b"# TurboFieldfare\n\nGrounded README facts.",
        content_type="application/vnd.github.raw+json",
        final_url=("https://api.github.com/repos/drumih/turbo-fieldfare/readme"),
    )

    def open_response(request, timeout):
        requests.append((request, timeout))
        return response

    result = fetch_article(
        url,
        opener=open_response,
        resolver=resolver_for({}),
        timeout_seconds=7,
    )

    assert result.text == "# TurboFieldfare\n\nGrounded README facts."
    assert result.method == "github_readme"
    assert result.extractor == "plain_text"
    assert result.fallback_reason == ""
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == (
        "https://api.github.com/repos/drumih/turbo-fieldfare/readme"
    )
    assert request.get_header("Accept") == "application/vnd.github.raw+json"
    assert request.get_header("X-github-api-version") == "2022-11-28"
    assert timeout == 7


def test_github_readme_preserves_markdown_lists_and_code_blocks():
    response = FakeResponse(
        b"# Project\n\n- First item\n- Second item\n\n```python\nvalue  = 1\n```",
        content_type="application/vnd.github.raw+json",
        final_url="https://api.github.com/repos/example/project/readme",
    )

    result = fetch_article(
        "https://github.com/example/project",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.text == (
        "# Project\n\n- First item\n- Second item\n\n```python\nvalue  = 1\n```"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/drumih",
        "https://github.com/drumih/turbo-fieldfare/issues/1",
        "https://gist.github.com/drumih/abc123",
        "https://github.com.evil.example/drumih/turbo-fieldfare",
        "https://github.com/drumih%2Fother/turbo-fieldfare",
    ],
)
def test_fetch_article_does_not_route_other_urls_to_github_readme_api(url):
    requests = []
    response = FakeResponse(b"Generic page facts.", content_type="text/plain")

    def open_response(request, timeout):
        requests.append(request.full_url)
        return response

    result = fetch_article(
        url,
        opener=open_response,
        resolver=resolver_for({}),
    )

    assert result.method == "direct"
    assert requests == [url]


def test_fetch_github_readme_reports_api_failure_without_jina_fallback():
    requested_urls = []

    def deny(request, timeout):
        requested_urls.append(request.full_url)
        raise http_error(request.full_url, 403, cf_mitigated="challenge")

    with pytest.raises(ArticleFetchError, match="GitHub README API") as caught:
        fetch_article_text(
            "https://github.com/drumih/turbo-fieldfare",
            opener=deny,
            resolver=resolver_for({}),
        )

    assert requested_urls == [
        "https://api.github.com/repos/drumih/turbo-fieldfare/readme"
    ]
    assert caught.value.error_code == "http_403"
    assert caught.value.method == "github_readme"
    assert caught.value.fallback_attempted is False


def test_fetch_github_readme_reuses_response_size_limit():
    response = FakeResponse(
        b"x" * 11,
        content_type="application/vnd.github.raw+json",
        final_url="https://api.github.com/repos/example/project/readme",
    )

    with pytest.raises(ArticleFetchError, match="too large") as caught:
        fetch_github_readme_text(
            "example",
            "project",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            max_bytes=10,
        )

    assert caught.value.error_code == "response_too_large"
    assert caught.value.method == "github_readme"


def test_github_blob_pdf_uses_raw_file_and_pypdf():
    blob_url = "https://github.com/example/project/blob/main/report.pdf"
    raw_url = "https://raw.githubusercontent.com/example/project/main/report.pdf"
    requests = []
    response = FakeResponse(
        make_pdf("Fixed branch PDF facts."),
        content_type="application/octet-stream",
        final_url=raw_url,
    )

    def open_response(request, timeout):
        requests.append(request.full_url)
        return response

    result = fetch_article(
        blob_url,
        opener=open_response,
        resolver=resolver_for({}),
    )

    assert requests == [raw_url]
    assert result.method == "github_raw"
    assert result.extractor == "pypdf"
    assert result.text == "Fixed branch PDF facts."


def test_github_blob_404_is_terminal_and_does_not_call_jina():
    blob_url = "https://github.com/example/project/blob/master/removed.pdf"
    requested_urls = []

    def deny(request, timeout):
        requested_urls.append(request.full_url)
        raise http_error(request.full_url, 404, cf_mitigated="challenge")

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            blob_url,
            opener=deny,
            resolver=resolver_for({}),
        )

    assert requested_urls == [
        "https://raw.githubusercontent.com/example/project/master/removed.pdf"
    ]
    assert caught.value.error_code == "github_file_not_found"
    assert caught.value.method == "github_raw"
    assert caught.value.fallback_attempted is False


def test_github_fixed_commit_blob_fetches_exact_raw_pdf():
    commit = "15c6504be51b884a0adc5d77e4dba41f94431454"
    blob_url = f"https://github.com/example/project/blob/{commit}/report.pdf"
    raw_url = f"https://raw.githubusercontent.com/example/project/{commit}/report.pdf"
    response = FakeResponse(
        make_pdf("Exact commit PDF facts."),
        content_type="application/octet-stream",
        final_url=raw_url,
    )
    requested_urls = []

    result = fetch_article(
        blob_url,
        opener=lambda request, timeout: (
            requested_urls.append(request.full_url) or response
        ),
        resolver=resolver_for({}),
    )

    assert requested_urls == [raw_url]
    assert result.text == "Exact commit PDF facts."
    assert result.method == "github_raw"
    assert result.extractor == "pypdf"


def test_github_blob_ref_with_encoded_slash_is_not_guessed():
    opened = []

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://github.com/example/project/blob/feature%2Fdocs/report.pdf",
            opener=lambda request, timeout: opened.append(request.full_url),
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "unsupported_github_path"
    assert caught.value.method == "github_raw"
    assert opened == []


def test_fetch_article_text_decodes_plain_text():
    response = FakeResponse("中文正文".encode(), content_type="text/plain")

    text = fetch_article_text(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert text == "中文正文"


def test_direct_pdf_extracts_layout_text_in_subprocess():
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="application/pdf",
        final_url="https://example.com/report.pdf",
    )

    result = fetch_article(
        "https://example.com/report.pdf",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.text == "Grounded PDF facts."
    assert result.method == "direct"
    assert result.extractor == "pypdf"


def test_direct_pdf_uses_adobe_markdown_when_configured(monkeypatch):
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="application/pdf",
        final_url="https://example.com/report.pdf",
    )
    monkeypatch.setenv("PDF_SERVICES_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PDF_SERVICES_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(
        article_fetcher,
        "_extract_pdf_with_adobe_in_subprocess",
        lambda *args, **kwargs: "# Report\n\nClean Adobe paragraph.",
    )
    monkeypatch.setattr(
        article_fetcher,
        "_extract_pdf_in_subprocess",
        lambda *args, **kwargs: pytest.fail("pypdf fallback should not run"),
    )

    result = fetch_article(
        "https://example.com/report.pdf",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.text == "# Report\n\nClean Adobe paragraph."
    assert result.method == "direct"
    assert result.extractor == "adobe_pdf_to_markdown"
    assert result.fallback_reason == ""


def test_direct_pdf_falls_back_to_pypdf_when_adobe_fails(monkeypatch, caplog):
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="application/pdf",
    )
    monkeypatch.setenv("PDF_SERVICES_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PDF_SERVICES_CLIENT_SECRET", "test-client-secret")

    def fail_adobe(*args, **kwargs):
        raise ArticleFetchError(
            "Adobe request failed",
            error_code="adobe_pdf_request_failed",
            extractor="adobe_pdf_to_markdown",
        )

    monkeypatch.setattr(
        article_fetcher,
        "_extract_pdf_with_adobe_in_subprocess",
        fail_adobe,
    )
    monkeypatch.setattr(
        article_fetcher,
        "_extract_pdf_in_subprocess",
        lambda *args, **kwargs: "Local pypdf facts.",
    )

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            "https://example.com/report.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )

    assert result.text == "Local pypdf facts."
    assert result.extractor == "pypdf"
    assert result.fallback_reason == "adobe_pdf_request_failed"
    assert "status=failed code=adobe_pdf_request_failed fallback=pypdf" in caplog.text


def test_direct_pdf_uses_pypdf_for_incomplete_adobe_credentials(monkeypatch):
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="application/pdf",
    )
    monkeypatch.setenv("PDF_SERVICES_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        article_fetcher,
        "_extract_pdf_in_subprocess",
        lambda *args, **kwargs: "Local pypdf facts.",
    )

    result = fetch_article(
        "https://example.com/report.pdf",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.extractor == "pypdf"
    assert result.fallback_reason == "adobe_pdf_credentials_incomplete"


def test_direct_pdf_preserves_adobe_fallback_when_pypdf_also_fails(monkeypatch):
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="application/pdf",
    )
    monkeypatch.setenv("PDF_SERVICES_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PDF_SERVICES_CLIENT_SECRET", "test-client-secret")

    def fail_adobe(*args, **kwargs):
        raise ArticleFetchError(
            "Adobe request failed",
            error_code="adobe_pdf_request_failed",
            extractor="adobe_pdf_to_markdown",
        )

    def fail_pypdf(*args, **kwargs):
        raise ArticleFetchError(
            "pypdf failed",
            error_code="pdf_parse_failed",
            extractor="pypdf",
        )

    monkeypatch.setattr(
        article_fetcher,
        "_extract_pdf_with_adobe_in_subprocess",
        fail_adobe,
    )
    monkeypatch.setattr(article_fetcher, "_extract_pdf_in_subprocess", fail_pypdf)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(
            "https://example.com/report.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "pdf_parse_failed"
    assert caught.value.extractor == "pypdf"
    assert caught.value.fallback_attempted is True
    assert caught.value.fallback_reason == "adobe_pdf_request_failed"


def test_adobe_pdf_worker_uses_bounded_subprocess_and_parses_markdown(monkeypatch):
    calls = []

    def complete(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {"status": "success", "text": "# Report\n\nClean text."}
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(article_fetcher.subprocess, "run", complete)

    text = article_fetcher._extract_pdf_with_adobe_in_subprocess(
        b"%PDF-test",
        max_pages=100,
        max_text_bytes=1024,
        timeout_seconds=240,
        address_space_bytes=512 * 1024 * 1024,
    )

    assert text == "# Report\n\nClean text."
    command, kwargs = calls[0]
    assert command[1:3] == ["-m", "daily_brief.adobe_pdf_extractor"]
    assert command[-1] == str(512 * 1024 * 1024)
    assert kwargs["input"] == b"%PDF-test"
    assert kwargs["timeout"] == 240


def test_adobe_pdf_worker_hard_timeout_has_stable_error(monkeypatch):
    def time_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(article_fetcher.subprocess, "run", time_out)

    with pytest.raises(ArticleFetchError) as caught:
        article_fetcher._extract_pdf_with_adobe_in_subprocess(
            b"%PDF-test",
            max_pages=100,
            max_text_bytes=1024,
            timeout_seconds=1,
            address_space_bytes=512 * 1024 * 1024,
        )

    assert caught.value.error_code == "adobe_pdf_timeout"
    assert caught.value.extractor == "adobe_pdf_to_markdown"


def test_direct_pdf_accepts_octet_stream_when_url_identifies_pdf():
    response = FakeResponse(
        make_pdf("Octet-stream PDF facts."),
        content_type="application/octet-stream",
        final_url="https://example.com/report.pdf?download=1",
    )

    result = fetch_article(
        "https://example.com/report.pdf?download=1",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.text == "Octet-stream PDF facts."
    assert result.method == "direct"
    assert result.extractor == "pypdf"


def test_direct_octet_stream_is_rejected_when_url_does_not_identify_pdf():
    response = FakeResponse(
        make_pdf("Unidentified PDF facts."),
        content_type="application/octet-stream",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/download",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "unsupported_content_type"


def test_direct_pdf_rejects_non_pdf_mime_with_pdf_magic():
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="text/plain",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/report.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "pdf_content_type_mismatch"
    assert caught.value.extractor == "pypdf"


def test_direct_pdf_enforces_download_limit_before_parsing(monkeypatch):
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="application/pdf",
    )
    subprocess_calls = []
    monkeypatch.setattr(
        "daily_brief.article_fetcher.subprocess.run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/report.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            pdf_max_bytes=100,
        )

    assert caught.value.error_code == "response_too_large"
    assert subprocess_calls == []


def test_direct_pdf_enforces_page_limit():
    response = FakeResponse(
        make_pdf("Page one.", "Page two."),
        content_type="application/pdf",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/report.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            pdf_max_pages=1,
        )

    assert caught.value.error_code == "pdf_too_many_pages"
    assert caught.value.method == "direct"
    assert caught.value.extractor == "pypdf"


def test_direct_pdf_reports_no_extractable_text():
    response = FakeResponse(
        make_pdf(None),
        content_type="application/pdf",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/scanned.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "pdf_no_extractable_text"
    assert caught.value.extractor == "pypdf"


def test_direct_pdf_reports_parse_failure():
    response = FakeResponse(
        b"%PDF-not-a-real-document",
        content_type="application/pdf",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/broken.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "pdf_parse_failed"
    assert caught.value.extractor == "pypdf"


def test_direct_pdf_reports_subprocess_timeout(monkeypatch):
    response = FakeResponse(
        make_pdf("Grounded PDF facts."),
        content_type="application/pdf",
    )

    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("daily_brief.article_fetcher.subprocess.run", time_out)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/report.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            pdf_parse_timeout_seconds=1,
        )

    assert caught.value.error_code == "pdf_parse_timeout"
    assert caught.value.extractor == "pypdf"


def test_direct_pdf_enforces_extracted_text_limit():
    response = FakeResponse(
        make_pdf("Grounded PDF facts exceed this tiny output limit."),
        content_type="application/pdf",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article_text(
            "https://example.com/report.pdf",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            extracted_max_bytes=10,
        )

    assert caught.value.error_code == "extracted_content_too_large"
    assert caught.value.extractor == "pypdf"


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
            resolver=resolver_for(
                {
                    "127.0.0.1": address,
                    "router.local": address,
                    "metadata.internal": address,
                }
            ),
        )


def test_fetch_article_text_revalidates_final_redirect_url():
    response = FakeResponse(b"secret", final_url="http://127.0.0.1/private")

    with pytest.raises(ArticleFetchError, match="safe public HTTP"):
        fetch_article_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({"127.0.0.1": "127.0.0.1"}),
        )


def test_connection_revalidates_and_rejects_dns_rebinding(monkeypatch):
    resolutions = iter([PUBLIC_ADDRESS, "127.0.0.1"])

    def rebinding_resolver(host, port, type):
        address = next(resolutions)
        return resolver_for({host: address})(host, port, type)

    opened_sockets = []
    monkeypatch.setattr(
        "daily_brief.article_fetcher.socket.socket",
        lambda *args: opened_sockets.append(args),
    )

    _validate_public_http_url("https://example.com/article", rebinding_resolver)
    with pytest.raises(ArticleFetchError) as caught:
        _create_public_connection(
            ("example.com", 443),
            resolver=rebinding_resolver,
        )

    assert caught.value.error_code == "unsafe_url"
    assert opened_sockets == []


def test_connection_uses_the_exact_validated_socket_address(monkeypatch):
    connected_addresses = []

    class FakeSocket:
        def settimeout(self, timeout):
            pass

        def bind(self, source_address):
            pass

        def connect(self, address):
            connected_addresses.append(address)

        def close(self):
            pass

    monkeypatch.setattr(
        "daily_brief.article_fetcher.socket.socket",
        lambda *args: FakeSocket(),
    )

    connection = _create_public_connection(
        ("example.com", 443),
        timeout=3,
        resolver=resolver_for({"example.com": PUBLIC_ADDRESS}),
    )

    assert isinstance(connection, FakeSocket)
    assert connected_addresses == [(PUBLIC_ADDRESS, 443)]


def test_fetch_article_text_rejects_pdf_with_invalid_magic():
    response = FakeResponse(b"%PDF", content_type="application/pdf")
    requested_urls = []

    with pytest.raises(ArticleFetchError, match="file signature") as caught:
        fetch_article_text(
            "https://example.com/file.pdf",
            opener=lambda request, timeout: (
                requested_urls.append(request.full_url) or response
            ),
            resolver=resolver_for({}),
        )

    assert requested_urls == ["https://example.com/file.pdf"]
    assert caught.value.error_code == "pdf_magic_mismatch"
    assert caught.value.extractor == "pypdf"
    assert caught.value.fallback_attempted is False


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
        make_jina_payload(),
        content_type="application/json",
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
    assert requests[1][0].get_header("Accept") == "application/json"
    assert requests[1][0].get_header("X-cache-tolerance") == "300"
    assert "method=direct status=cloudflare_challenge fallback=jina" in caplog.text
    assert "method=jina extractor=jina status=success" in caplog.text


def test_fetch_article_reports_jina_retrieval_method():
    requests = []
    jina_response = FakeResponse(
        make_jina_payload(status=20000),
        content_type="application/json",
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
    assert result.extractor == "jina"
    assert result.fallback_reason == "cloudflare_challenge"


def test_fetch_article_uses_jina_for_vercel_challenge_without_wayback(caplog):
    requests = []
    source_url = "https://example.com/article"
    jina_response = FakeResponse(
        make_jina_payload("Recovered from Jina."),
        content_type="application/json",
        final_url=f"https://r.jina.ai/{source_url}",
    )

    def open_response(request, timeout):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise http_error(
                request.full_url,
                429,
                server="Vercel",
                x_vercel_mitigated="challenge",
            )
        return jina_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            source_url,
            opener=open_response,
            resolver=resolver_for({}),
        )

    assert requests == [source_url, f"https://r.jina.ai/{source_url}"]
    assert result.text == "Recovered from Jina."
    assert result.method == "jina"
    assert result.fallback_reason == "vercel_challenge"
    assert result.attempts == 2
    assert result.material_origin == "original"
    assert "method=direct status=vercel_challenge fallback=jina" in caplog.text
    assert "fallback=wayback" not in caplog.text


def test_vercel_and_jina_failures_use_recent_wayback_capture(caplog):
    requests = []
    source_url = "https://www.felonybench.com/"
    capture_timestamp = "20260822062417"
    replay_url = (
        f"https://web.archive.org/web/{capture_timestamp}id_/{source_url}"
    )
    archived_html = b"""
    <html><body><main>
      <h1>Felony Bench</h1>
      <p>A benchmark you really do not want models to be saturated with.</p>
      <p>Anthropic scored eight incidents and OpenAI scored eight incidents.</p>
      <h2>Methodology</h2>
      <p>The benchmark counts unique cases where agents affect third parties.</p>
    </main></body></html>
    """

    def open_response(request, timeout):
        requests.append(request.full_url)
        if request.full_url == source_url:
            raise http_error(
                request.full_url,
                429,
                server="Vercel",
                x_vercel_mitigated="challenge",
            )
        if request.full_url.startswith("https://r.jina.ai/"):
            return FakeResponse(
                make_jina_payload(
                    "Vercel Security Checkpoint",
                    http_status=429,
                    url=source_url,
                ),
                content_type="application/json",
                final_url=f"https://r.jina.ai/{source_url}",
            )
        if request.full_url.startswith("https://web.archive.org/cdx/search/cdx?"):
            return FakeResponse(
                make_wayback_payload(
                    [
                        capture_timestamp,
                        source_url,
                        "text/html",
                        "200",
                        "CAPTUREDIGEST",
                        str(len(archived_html)),
                    ]
                ),
                content_type="application/json",
                final_url=request.full_url,
            )
        assert request.full_url == replay_url
        return FakeResponse(
            archived_html,
            final_url=replay_url,
        )

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            source_url,
            opener=open_response,
            resolver=resolver_for({}),
            wayback_not_before=datetime(2026, 8, 21, 15, 17, tzinfo=UTC),
            wayback_not_after=datetime(2026, 8, 23, 0, 0, tzinfo=UTC),
        )

    assert len(requests) == 4
    cdx_query = parse_qs(urlparse(requests[2]).query)
    assert cdx_query["matchType"] == ["exact"]
    assert cdx_query["from"] == ["20260821151700"]
    assert cdx_query["to"] == ["20260823000000"]
    assert cdx_query["filter"] == ["statuscode:200", "mimetype:text/html"]
    assert cdx_query["limit"] == ["-5"]
    assert cdx_query["gzip"] == ["false"]
    assert result.method == "wayback"
    assert result.extractor == "trafilatura"
    assert result.fallback_reason == "vercel_challenge"
    assert result.attempts == 4
    assert result.retrieved_url == replay_url
    assert result.material_origin == "archived_copy"
    assert "Felony Bench" in result.text
    assert "unique cases where agents affect third parties" in result.text
    assert "method=jina extractor=jina status=failed" in caplog.text
    assert "method=wayback extractor=trafilatura status=success" in caplog.text


def test_wayback_rejects_index_row_outside_allowed_window():
    source_url = "https://example.com/article"
    requested_urls = []

    def open_response(request, timeout):
        requested_urls.append(request.full_url)
        if request.full_url == source_url:
            raise http_error(
                request.full_url,
                429,
                x_vercel_mitigated="challenge",
            )
        if request.full_url.startswith("https://r.jina.ai/"):
            raise http_error(request.full_url, 403)
        return FakeResponse(
            make_wayback_payload(
                [
                    "20260819000000",
                    source_url,
                    "text/html",
                    "200",
                    "OLDDIGEST",
                    "2000",
                ]
            ),
            content_type="application/json",
            final_url=request.full_url,
        )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(
            source_url,
            opener=open_response,
            resolver=resolver_for({}),
            wayback_not_before=datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
            wayback_not_after=datetime(2026, 8, 23, 0, 0, tzinfo=UTC),
        )

    assert len(requested_urls) == 3
    assert caught.value.error_code == "wayback_invalid_index"
    assert caught.value.method == "wayback"
    assert caught.value.fallback_attempted is True
    assert caught.value.fallback_reason == "vercel_challenge"
    assert caught.value.attempts == 3
    assert "direct=vercel challenge" in str(caught.value)
    assert "jina=Jina Reader request failed" in str(caught.value)


def test_wayback_reports_no_capture_without_requesting_replay():
    source_url = "https://example.com/article"
    requested_urls = []

    def open_response(request, timeout):
        requested_urls.append(request.full_url)
        if request.full_url == source_url:
            raise http_error(
                request.full_url,
                429,
                x_vercel_mitigated="challenge",
            )
        if request.full_url.startswith("https://r.jina.ai/"):
            raise http_error(request.full_url, 403)
        return FakeResponse(
            make_wayback_payload(),
            content_type="application/json",
            final_url=request.full_url,
        )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(
            source_url,
            opener=open_response,
            resolver=resolver_for({}),
            wayback_not_before=datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
            wayback_not_after=datetime(2026, 8, 23, 0, 0, tzinfo=UTC),
        )

    assert len(requested_urls) == 3
    assert caught.value.error_code == "wayback_no_capture"
    assert caught.value.attempts == 3


def test_wayback_rejects_replay_redirect_to_live_source():
    source_url = "https://example.com/article"
    capture_timestamp = "20260822062417"

    def open_response(request, timeout):
        if request.full_url == source_url:
            raise http_error(
                request.full_url,
                429,
                x_vercel_mitigated="challenge",
            )
        if request.full_url.startswith("https://r.jina.ai/"):
            raise http_error(request.full_url, 403)
        if request.full_url.startswith("https://web.archive.org/cdx/search/cdx?"):
            return FakeResponse(
                make_wayback_payload(
                    [
                        capture_timestamp,
                        source_url,
                        "text/html",
                        "200",
                        "CAPTUREDIGEST",
                        "2000",
                    ]
                ),
                content_type="application/json",
                final_url=request.full_url,
            )
        return FakeResponse(
            b"<html><body><article>Archived facts.</article></body></html>",
            final_url=source_url,
        )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(
            source_url,
            opener=open_response,
            resolver=resolver_for({}),
            wayback_not_before=datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
            wayback_not_after=datetime(2026, 8, 23, 0, 0, tzinfo=UTC),
        )

    assert caught.value.error_code == "wayback_invalid_replay_url"
    assert caught.value.method == "wayback"
    assert caught.value.attempts == 4


@pytest.mark.parametrize("status_code", [401, 403])
def test_fetch_article_uses_jina_for_datadome_challenge(caplog, status_code):
    requests = []
    jina_response = FakeResponse(
        make_jina_payload("Recovered Reuters article facts."),
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    def open_response(request, timeout):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise http_error(
                request.full_url,
                status_code,
                server="CloudFront",
                x_datadome="protected",
                x_dd_b="1",
            )
        return jina_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            "https://example.com/article",
            opener=open_response,
            resolver=resolver_for({}),
        )

    assert requests == [
        "https://example.com/article",
        "https://r.jina.ai/https://example.com/article",
    ]
    assert result.text == "Recovered Reuters article facts."
    assert result.method == "jina"
    assert result.extractor == "jina"
    assert result.fallback_reason == "datadome_challenge"
    assert result.retrieved_url == "https://example.com/article"
    assert "method=direct status=datadome_challenge fallback=jina" in caplog.text


@pytest.mark.parametrize(
    ("status_code", "headers"),
    [
        (429, {"x_datadome": "protected", "x_dd_b": "1"}),
        (401, {"x_dd_b": "1"}),
    ],
)
def test_datadome_detection_requires_supported_status_and_explicit_header(
    status_code, headers
):
    requested_urls = []

    def deny(request, timeout):
        requested_urls.append(request.full_url)
        raise http_error(request.full_url, status_code, **headers)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(
            "https://example.com/article",
            opener=deny,
            resolver=resolver_for({}),
        )

    assert requested_urls == ["https://example.com/article"]
    assert caught.value.error_code == f"http_{status_code}"
    assert caught.value.method == "direct"
    assert caught.value.fallback_attempted is False


def test_fetch_article_reports_datadome_and_jina_failures():
    def fail(request, timeout):
        if request.full_url.startswith("https://r.jina.ai/"):
            raise http_error(request.full_url, 403)
        raise http_error(request.full_url, 401, x_datadome="protected", x_dd_b="1")

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(
            "https://example.com/article",
            opener=fail,
            resolver=resolver_for({}),
        )

    assert caught.value.error_code == "http_403"
    assert caught.value.method == "jina"
    assert caught.value.extractor == "jina"
    assert caught.value.fallback_attempted is True
    assert caught.value.fallback_reason == "datadome_challenge"
    assert "direct=datadome challenge" in str(caught.value)


def test_fetch_jina_reader_text_preserves_markdown_structure():
    content = "# Report\n\n## Findings\n\n- First result\n- Second result"
    response = FakeResponse(
        make_jina_payload(content),
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    text = fetch_jina_reader_text(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert text == content


@pytest.mark.parametrize("status_code", [401, 403, 404])
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


def test_fetch_article_uses_jina_when_tls_issuer_is_unavailable(caplog):
    requests = []
    jina_response = FakeResponse(
        make_jina_payload("Recovered article facts."),
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    def open_response(request, timeout):
        requests.append(request.full_url)
        if len(requests) == 1:
            raise tls_verification_error(20, "unable to get local issuer certificate")
        return jina_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            "https://example.com/article",
            opener=open_response,
            resolver=resolver_for({}),
        )

    assert requests == [
        "https://example.com/article",
        "https://r.jina.ai/https://example.com/article",
    ]
    assert result.text == "Recovered article facts."
    assert result.method == "jina"
    assert result.extractor == "jina"
    assert result.fallback_reason == "tls_issuer_unavailable"
    assert result.attempts == 2
    assert "status=tls_issuer_unavailable fallback=jina" in caplog.text
    assert (
        "method=jina extractor=jina status=success "
        "fallback_reason=tls_issuer_unavailable"
    ) in caplog.text


@pytest.mark.parametrize(
    "timeout_error",
    [
        TimeoutError("The read operation timed out"),
        URLError(TimeoutError("The handshake operation timed out")),
    ],
)
def test_fetch_article_retries_network_timeout_once_before_direct_success(
    caplog, timeout_error
):
    requested_urls = []
    sleeps = []
    direct_response = FakeResponse(
        b"Recovered direct facts.", content_type="text/plain"
    )

    def open_response(request, timeout):
        requested_urls.append(request.full_url)
        if len(requested_urls) == 1:
            raise timeout_error
        return direct_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            "https://example.com/article",
            opener=open_response,
            resolver=resolver_for({}),
            sleeper=sleeps.append,
        )

    assert requested_urls == [
        "https://example.com/article",
        "https://example.com/article",
    ]
    assert sleeps == [article_fetcher.DIRECT_RETRY_DELAY_SECONDS]
    assert result.text == "Recovered direct facts."
    assert result.method == "direct"
    assert result.attempts == 2
    assert "status=network_timeout attempt=1/2 retry_in=1s" in caplog.text


def test_fetch_article_uses_jina_after_two_network_timeouts(caplog):
    requested_urls = []
    sleeps = []
    jina_response = FakeResponse(
        make_jina_payload("Recovered through Jina."),
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    def open_response(request, timeout):
        requested_urls.append(request.full_url)
        if request.full_url == "https://example.com/article":
            raise URLError(TimeoutError("The handshake operation timed out"))
        return jina_response

    with caplog.at_level("INFO", logger="daily_brief.article_fetcher"):
        result = fetch_article(
            "https://example.com/article",
            opener=open_response,
            resolver=resolver_for({}),
            sleeper=sleeps.append,
        )

    assert requested_urls == [
        "https://example.com/article",
        "https://example.com/article",
        "https://r.jina.ai/https://example.com/article",
    ]
    assert sleeps == [article_fetcher.DIRECT_RETRY_DELAY_SECONDS]
    assert result.text == "Recovered through Jina."
    assert result.method == "jina"
    assert result.extractor == "jina"
    assert result.fallback_reason == "network_timeout"
    assert result.attempts == 3
    assert "status=network_timeout attempt=2/2 fallback=jina" in caplog.text


def test_network_timeout_and_jina_failure_preserve_attempt_count():
    requested_urls = []

    def fail(request, timeout):
        requested_urls.append(request.full_url)
        if request.full_url.startswith("https://r.jina.ai/"):
            raise http_error(request.full_url, 502)
        raise URLError(TimeoutError("The handshake operation timed out"))

    with pytest.raises(
        ArticleFetchError, match="network timeout after 2 attempts"
    ) as caught:
        fetch_article(
            "https://example.com/article",
            opener=fail,
            resolver=resolver_for({}),
            sleeper=lambda delay: None,
        )

    assert requested_urls == [
        "https://example.com/article",
        "https://example.com/article",
        "https://r.jina.ai/https://example.com/article",
    ]
    assert caught.value.method == "jina"
    assert caught.value.fallback_attempted is True
    assert caught.value.fallback_reason == "network_timeout"
    assert caught.value.attempts == 3


@pytest.mark.parametrize(
    ("verify_code", "verify_message"),
    [
        (10, "certificate has expired"),
        (18, "self-signed certificate"),
        (62, "hostname mismatch"),
    ],
)
def test_fetch_article_does_not_use_jina_for_other_tls_verification_errors(
    verify_code, verify_message
):
    requested_urls = []

    def deny(request, timeout):
        requested_urls.append(request.full_url)
        raise tls_verification_error(verify_code, verify_message)

    with pytest.raises(
        ArticleFetchError, match="direct article request failed"
    ) as caught:
        fetch_article(
            "https://example.com/article",
            opener=deny,
            resolver=resolver_for({}),
        )

    assert requested_urls == ["https://example.com/article"]
    assert caught.value.error_code == "request_failed"
    assert caught.value.method == "direct"
    assert caught.value.fallback_attempted is False


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


def test_empty_trafilatura_and_jina_failure_preserve_combined_provenance(
    monkeypatch,
):
    monkeypatch.setattr(
        "daily_brief.article_fetcher.trafilatura.extract", lambda *args, **kwargs: None
    )
    direct_response = FakeResponse(b"<html><body>Client shell</body></html>")
    jina_response = FakeResponse(
        b"not-json",
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )
    requests = []

    def open_response(request, timeout):
        requests.append(request.full_url)
        return direct_response if len(requests) == 1 else jina_response

    with pytest.raises(
        ArticleFetchError,
        match=(
            "article retrieval failed: direct=trafilatura empty_content; "
            "jina=Jina Reader returned malformed JSON"
        ),
    ) as caught:
        fetch_article_text(
            "https://example.com/article",
            opener=open_response,
            resolver=resolver_for({}),
        )

    assert requests == [
        "https://example.com/article",
        "https://r.jina.ai/https://example.com/article",
    ]
    assert caught.value.error_code == "jina_malformed_json"
    assert caught.value.method == "jina"
    assert caught.value.extractor == "jina"
    assert caught.value.fallback_attempted is True
    assert caught.value.fallback_reason == "empty_content"


def test_fetch_article_text_rejects_empty_content_with_stable_error_code():
    response = FakeResponse(b"   ", content_type="text/plain")
    requested_urls = []

    with pytest.raises(ArticleFetchError, match="no extractable text") as caught:
        fetch_article_text(
            "https://example.com/article",
            opener=lambda request, timeout: (
                requested_urls.append(request.full_url) or response
            ),
            resolver=resolver_for({}),
        )

    assert requested_urls == ["https://example.com/article"]
    assert caught.value.error_code == "empty_content"
    assert caught.value.method == "direct"
    assert caught.value.extractor == "plain_text"
    assert caught.value.fallback_attempted is False


def test_fetch_jina_reader_text_reuses_response_size_limit():
    response = FakeResponse(
        b"x" * 11,
        content_type="application/json",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    with pytest.raises(ArticleFetchError, match="too large"):
        fetch_jina_reader_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            max_bytes=10,
        )


@pytest.mark.parametrize(
    ("payload", "content_type", "error_code"),
    [
        (b"not-json", "application/json", "jina_malformed_json"),
        (b"[]", "application/json", "jina_invalid_envelope"),
        (
            json.dumps({"code": 200, "status": 200, "data": None}).encode(),
            "application/json",
            "jina_invalid_envelope",
        ),
        (make_jina_payload(code=500), "application/json", "jina_provider_status"),
        (
            make_jina_payload(status=50000),
            "application/json",
            "jina_provider_status",
        ),
        (
            make_jina_payload(http_status=404),
            "application/json",
            "jina_origin_status",
        ),
        (
            make_jina_payload(url="http://127.0.0.1/private"),
            "application/json",
            "jina_invalid_url",
        ),
        (
            make_jina_payload(content=None),
            "application/json",
            "jina_invalid_content",
        ),
        (
            make_jina_payload(content="  \n  "),
            "application/json",
            "jina_invalid_content",
        ),
        (make_jina_payload(), "text/plain", "jina_unsupported_content_type"),
    ],
)
def test_fetch_jina_reader_text_validates_json_envelope(
    payload, content_type, error_code
):
    response = FakeResponse(
        payload,
        content_type=content_type,
        final_url="https://r.jina.ai/https://example.com/article",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_jina_reader_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({"127.0.0.1": "127.0.0.1"}),
        )

    assert caught.value.error_code == error_code
    assert caught.value.method == "jina"
    assert caught.value.extractor == "jina"


def test_fetch_jina_reader_text_enforces_content_limit_after_json_decode():
    content = "界" * 300
    payload = json.dumps(
        {
            "code": 200,
            "status": 200,
            "data": {
                "httpStatus": 200,
                "url": "https://example.com/article",
                "content": content,
            },
        },
        ensure_ascii=False,
    ).encode("utf-16")
    assert len(payload) < len(content.encode("utf-8"))
    response = FakeResponse(
        payload,
        content_type="application/json",
        charset="utf-16",
        final_url="https://r.jina.ai/https://example.com/article",
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_jina_reader_text(
            "https://example.com/article",
            opener=lambda request, timeout: response,
            resolver=resolver_for({}),
            max_bytes=len(payload),
        )

    assert caught.value.error_code == "extracted_content_too_large"
    assert caught.value.extractor == "jina"
