"""Reader origin redirects still require safe URLs and usable article material."""

import json
import logging

import pytest

from daily_brief.article_fetcher import ArticleFetchError, fetch_article
from daily_brief.article_fetcher.jina import _fetch_jina_reader
from test_article_fetcher import FakeResponse, http_error, make_jina_payload, resolver_for


URL = "https://example.com/article?gift=test-only-gift"
READER_URL = f"https://r.jina.ai/{URL}"
BODY = "# Article\n\nThe author explains a specific technical change.\n\nArticle ending."
MISSING = object()


def reader_response(*, origin_status=302, content=BODY, origin_url=URL, **data_fields):
    envelope = json.loads(make_jina_payload(content, status=20000, url=origin_url))
    if origin_status is MISSING:
        envelope["data"].pop("httpStatus")
    else:
        envelope["data"]["httpStatus"] = origin_status
    envelope["data"].update(data_fields)
    response = FakeResponse(
        json.dumps(envelope).encode(),
        content_type="application/json",
        final_url=READER_URL,
    )
    response.status = 200
    return response


def read_response(response):
    return _fetch_jina_reader(
        URL, opener=lambda request, timeout: response,
        resolver=resolver_for({"127.0.0.1": "127.0.0.1"}),
    )


@pytest.mark.parametrize("origin_status", [200, 206, 299, 301, 302, 303, 307, 308])
def test_reader_accepts_success_and_redirect_statuses_with_body(origin_status):
    result = read_response(reader_response(origin_status=origin_status))
    assert result.text == BODY
    assert result.origin_url == URL
    assert result.attempts == 1


@pytest.mark.parametrize(
    "origin_status", [300, 304, 305, 306, 309, 401, 403, 404, 500, MISSING, None, "302", True, False]
)
def test_reader_rejects_other_or_invalid_origin_statuses(origin_status):
    with pytest.raises(ArticleFetchError) as caught:
        read_response(reader_response(origin_status=origin_status))
    assert caught.value.error_code == "jina_origin_status"
    if isinstance(origin_status, int) and not isinstance(origin_status, bool):
        assert f"origin_http_status={origin_status}" in str(caught.value)


def test_cloudflare_recovery_accepts_reader_redirect_without_wayback():
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == URL:
            raise http_error(URL, 403, cf_mitigated="challenge")
        assert request.full_url == READER_URL
        return reader_response()

    result = fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert requests == [URL, READER_URL]
    assert result.text == BODY
    assert result.method == "jina"
    assert result.extractor == "jina"
    assert result.fallback_reason == "cloudflare_challenge"
    assert result.attempts == 2
    assert result.retrieved_url == URL


@pytest.mark.parametrize(
    ("data_fields", "error_code"),
    [
        ({"content": " \n "}, "jina_invalid_content"),
        ({"content": None}, "jina_invalid_content"),
        ({"content": "Vercel Security Checkpoint\nWe are verifying your browser"}, "challenge_page"),
        ({"origin_url": "http://127.0.0.1/private"}, "jina_invalid_url"),
        ({"origin_url": "https://example.com/cdn-cgi/challenge-platform/test"}, "challenge_page"),
    ],
)
def test_reader_redirect_preserves_material_and_url_validation(data_fields, error_code):
    with pytest.raises(ArticleFetchError) as caught:
        read_response(reader_response(**data_fields))
    assert caught.value.error_code == error_code


def test_reader_prefers_explicit_publication_time_over_metadata():
    result = read_response(reader_response(
        publishedTime="2026-10-03T11:00:00Z",
        metadata={"article:published_time": "2026-10-04T12:00:00Z"},
    ))
    assert result.source_evidence.published_at == "2026-10-03T11:00:00Z"


@pytest.mark.parametrize("published_time", [MISSING, None, 123, False, "", " \t ", "x" * 65])
def test_reader_falls_back_to_publication_metadata_for_unusable_time(published_time):
    fields = {"metadata": {"article:published_time": "2026-10-03T11:00:00Z"}}
    if published_time is not MISSING:
        fields["publishedTime"] = published_time
    result = read_response(reader_response(**fields))
    assert result.source_evidence.published_at == "2026-10-03T11:00:00Z"


@pytest.mark.parametrize(
    "metadata",
    [None, [], "invalid", {"article:published_time": 123}, {"article:published_time": " "},
     {"article:published_time": "x" * 65}, {"article:modified_time": "2026-10-04T12:00:00Z"}],
)
def test_reader_does_not_infer_publication_from_unusable_or_modified_metadata(metadata):
    result = read_response(reader_response(
        metadata=metadata, modifiedTime="2026-10-04T12:00:00Z"
    ))
    assert result.source_evidence.published_at == ""


@pytest.mark.parametrize("origin_status", [302, 403])
def test_reader_logs_numeric_response_details_without_material_or_credentials(origin_status, caplog):
    response = reader_response(origin_status=origin_status)
    with caplog.at_level(logging.INFO):
        if origin_status == 302:
            read_response(response)
        else:
            with pytest.raises(ArticleFetchError):
                read_response(response)
    assert "http_status=200" in caplog.text
    assert "code=200" in caplog.text
    assert "status=20000" in caplog.text
    assert f"origin_http_status={origin_status}" in caplog.text
    assert f"content_chars={len(BODY)}" in caplog.text
    assert BODY not in caplog.text
    assert "test-only-gift" not in caplog.text


def test_reader_does_not_echo_untrusted_status_fields_in_logs_or_errors(caplog):
    envelope = json.loads(make_jina_payload())
    envelope["code"] = "secret-credential-in-untrusted-code"
    envelope["status"] = "secret-credential-in-untrusted-status"
    envelope["data"]["httpStatus"] = "secret-credential-in-untrusted-origin"
    response = FakeResponse(json.dumps(envelope).encode(), content_type="application/json", final_url=READER_URL)
    response.status = 200
    with caplog.at_level(logging.INFO), pytest.raises(ArticleFetchError) as caught:
        read_response(response)
    assert "secret-credential" not in caplog.text + str(caught.value)
