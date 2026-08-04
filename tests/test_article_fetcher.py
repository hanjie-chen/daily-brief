from email.message import Message
from io import BytesIO
import json
import subprocess
from urllib.error import HTTPError

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

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
    response = FakeResponse(b"Direct facts.", content_type="text/plain")

    result = fetch_article(
        "https://example.com/article",
        opener=lambda request, timeout: response,
        resolver=resolver_for({}),
    )

    assert result.text == "Direct facts."
    assert result.method == "direct"
    assert result.extractor == "plain_text"
    assert result.fallback_reason == ""


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

    assert result.text == "# TurboFieldfare Grounded README facts."
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
