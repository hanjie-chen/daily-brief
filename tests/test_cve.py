"""CVE record pages use bounded official structured evidence."""

import json
from dataclasses import replace
from urllib.error import URLError

import pytest

from daily_brief.article_fetcher import (
    CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY, ArticleFetchError, fetch_article,
)
from daily_brief.article_fetcher.budget import retrieval_budget
from daily_brief.llm.summarizer import build_summary_material_context, split_page_material
from daily_brief.models import Candidate
from fakes import story
from test_article_fetcher import FakeResponse, http_error, make_jina_payload, resolver_for
from test_github_issue_redirects import transport

URL = "https://www.cve.org/CVERecord?id=CVE-2026-12345"
API_URL = "https://cveawg.mitre.org/api/cve/CVE-2026-12345"
DESCRIPTION = "An out-of-bounds write in Example Paint permits code execution."
PUBLISHED = "2026-11-06T10:00:00Z"


def record():
    return {
        "dataType": "CVE_RECORD", "dataVersion": "5.1",
        "cveMetadata": {"cveId": "CVE-2026-12345", "state": "PUBLISHED",
                        "datePublished": PUBLISHED, "dateUpdated": "2026-11-07T12:00:00Z"},
        "containers": {"adp": [{"providerMetadata": {"shortName": "Supplemental Authority"},
                                "notes": "Supplemental remediation advice"}], "cna": {
            "title": "Example Paint code execution",
            "descriptions": [{"lang": "en", "value": DESCRIPTION}],
            "affected": [{"vendor": "Example", "product": "Paint", "defaultStatus": "unaffected",
                          "versions": [{"version": "11.0.0.0", "lessThan": "11.2605.91.0",
                                        "status": "affected", "versionType": "custom",
                                        "changes": [{"at": "11.2600.0.0", "status": "unaffected"}]}]}],
            "problemTypes": [{"descriptions": [{"type": "CWE", "cweId": "CWE-787",
                                                  "description": "Out-of-bounds Write"}]}],
            "metrics": [{"cvssV3_1": {"version": "3.1", "baseScore": 8.8,
                                        "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:H"}}],
            "references": [{"url": "https://example.com/advisory", "tags": ["vendor-advisory"]}],
        }},
    }


def response(data=None, *, final_url=API_URL):
    return FakeResponse(json.dumps(record() if data is None else data).encode(),
                        content_type="application/json", final_url=final_url)


@pytest.mark.parametrize("policy", [CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY])
@pytest.mark.parametrize("url", [URL, "http://cve.org/CVERecord/?id=cve-2026-12345",
                                  "https://cve.org:443/CVERecord?id=CVE-2026-12345&view=full#details"])
def test_cve_api_preserves_structured_material_and_metadata(policy, url, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-secret")
    requests = []

    def opener(request, timeout):
        requests.append(request)
        assert timeout == 5
        return response()

    with retrieval_budget(timeout_seconds=5, clock=lambda: 10) as budget:
        result = fetch_article(url, opener=opener, resolver=resolver_for({}), policy=policy)
        assert budget.requests == 1
    assert [request.full_url for request in requests] == [API_URL]
    assert requests[0].get_header("Accept") == "application/json"
    assert requests[0].get_header("Authorization") is None
    assert result.method == result.extractor == "cve_api"
    assert result.retrieved_url == url
    assert result.source_evidence.title == "Example Paint code execution"
    assert result.source_evidence.published_at == PUBLISHED
    metadata, body = split_page_material(result.text)
    assert "Example Paint code execution" in metadata
    for value in ["CVE-2026-12345", DESCRIPTION, "11.0.0.0", "11.2605.91.0", "11.2600.0.0",
                  "unaffected", "CWE-787", "8.8", "CVSS:3.1", "https://example.com/advisory",
                  PUBLISHED, "2026-11-07T12:00:00Z", "Supplemental remediation advice"]:
        assert value in body
    candidate = Candidate(story=replace(story("123", "Example Paint"), fetched_text=result.text))
    context = build_summary_material_context(candidate)
    assert "web_body" in context.sections
    assert DESCRIPTION in context.text


@pytest.mark.parametrize("url", [
    "https://cve.org/", "https://cve.org/CVERecord", "https://cve.org/CVERecord?id=CVE-26-12345",
    "https://cve.org/CVERecord?id=CVE-2026-123", "https://cve.org/CVERecord?id=CVE-2026-１２３４５",
    "https://cve.org/CVERecord?id=CVE-2026-12345&id=CVE-2026-12345",
    "https://cve.org/CVERecord?id=CVE-2026-12345&id=", "https://cve.org/CVERecord/extra?id=CVE-2026-12345",
    "https://cve.org.evil.example/CVERecord?id=CVE-2026-12345",
    "https://cve.org:8443/CVERecord?id=CVE-2026-12345",
    "https://user:password@cve.org/CVERecord?id=CVE-2026-12345",
])
def test_other_urls_keep_ordinary_route(url):
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(b"Other page facts.", content_type="text/plain", final_url=url)

    assert fetch_article(url, opener=opener, resolver=resolver_for({})).method == "direct"
    assert requests == [url]


@pytest.mark.parametrize("case", ["list", "missing", "type", "wrong_id", "rejected", "missing_cna",
                                  "descriptions_wrong_type", "description_wrong_type"])
def test_invalid_record_is_terminal(case):
    data = record()
    if case == "list":
        data = []
    elif case == "missing":
        data = {}
    elif case == "type":
        data["dataType"] = "OTHER"
    elif case == "wrong_id":
        data["cveMetadata"]["cveId"] = "CVE-2026-54321"
    elif case == "rejected":
        data["cveMetadata"]["state"] = "REJECTED"
    elif case == "missing_cna":
        data["containers"] = {}
    elif case == "descriptions_wrong_type":
        data["containers"]["cna"]["descriptions"] = "not a list"
    else:
        data["containers"]["cna"]["descriptions"] = [{"lang": "en", "value": 123}]
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return response(data)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == "invalid_response"
    assert requests == [API_URL]


@pytest.mark.parametrize("descriptions", [[], [{"lang": "en", "value": " \n "}]])
@pytest.mark.parametrize("policy", [CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY])
def test_empty_description_recovers_original_page_only_for_summary(descriptions, policy):
    data = record()
    data["containers"]["cna"]["descriptions"] = descriptions
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == API_URL:
            return response(data)
        assert request.full_url == "https://r.jina.ai/" + URL
        return FakeResponse(make_jina_payload(content=DESCRIPTION, url=URL), content_type="application/json")

    if policy == CLASSIFICATION_FETCH_POLICY:
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(URL, opener=opener, resolver=resolver_for({}), policy=policy)
        assert caught.value.error_code == "empty_content"
        assert requests == [API_URL]
    else:
        result = fetch_article(URL, opener=opener, resolver=resolver_for({}), policy=policy)
        assert result.method == "jina"
        assert result.fallback_reason == "empty_content"
        assert DESCRIPTION in result.text
        assert requests == [API_URL, "https://r.jina.ai/" + URL]


@pytest.mark.parametrize(("limits", "code"), [({"html_max_bytes": 10}, "response_too_large"),
                                             ({"extracted_max_bytes": 10}, "extracted_content_too_large")])
def test_cve_size_limits_remain_terminal(limits, code):
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return response()

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}), **limits)
    assert caught.value.error_code == code
    assert requests == [API_URL]


def test_cve_recovery_shares_request_budget():
    data = record()
    data["containers"]["cna"]["descriptions"] = []
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return response(data)

    with retrieval_budget(max_requests=1):
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == "retrieval_budget_exhausted"
    assert requests == [API_URL]


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_transient_api_failure_retries_then_recovers_original_page(failure):
    requests, sleeps = [], []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == API_URL:
            if failure == "timeout":
                raise URLError(TimeoutError("timed out"))
            raise http_error(API_URL, 503)
        assert request.full_url == "https://r.jina.ai/" + URL
        return FakeResponse(make_jina_payload(content=DESCRIPTION, url=URL), content_type="application/json")

    result = fetch_article(URL, opener=opener, resolver=resolver_for({}), sleeper=sleeps.append)
    assert requests == [API_URL, API_URL, "https://r.jina.ai/" + URL]
    assert result.method == "jina"
    assert sleeps == [1]


def test_ordinary_429_retries_api_without_switching_channels():
    requests, sleeps = [], []

    def opener(request, timeout):
        requests.append(request.full_url)
        raise http_error(API_URL, 429, Retry_After="1")

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}), sleeper=sleeps.append)
    assert caught.value.error_code == "http_429"
    assert requests == [API_URL, API_URL]
    assert sleeps == [1]


@pytest.mark.parametrize("final_url", ["https://evil.example/api/cve/CVE-2026-12345",
                                        "http://cveawg.mitre.org/api/cve/CVE-2026-12345",
                                        "https://cveawg.mitre.org/api/cve/CVE-2026-54321",
                                        "https://cveawg.mitre.org/api/cve/CVE-2026-12345?other=1",
                                        "http://127.0.0.1/record"])
def test_changed_final_api_url_is_rejected(final_url):
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=lambda request, timeout: response(final_url=final_url),
                      resolver=resolver_for({"127.0.0.1": "127.0.0.1"}))
    assert caught.value.error_code in {"unsafe_url", "invalid_response"}


@pytest.mark.parametrize("destination", ["https://evil.example/record",
                                          "https://cveawg.mitre.org/api/cve/CVE-2026-54321",
                                          "http://cveawg.mitre.org/api/cve/CVE-2026-12345"])
def test_redirect_hop_cannot_leave_exact_api_endpoint(destination):
    requests = []
    opener = transport({API_URL: (302, destination), destination: record()}, requests)
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code in {"unsafe_url", "invalid_response"}
    assert [request.full_url for request in requests] == [API_URL]


def test_private_api_resolution_is_rejected_before_request():
    requests = []
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=lambda request, timeout: requests.append(request),
                      resolver=resolver_for({"cveawg.mitre.org": "127.0.0.1"}))
    assert caught.value.error_code == "unsafe_url"
    assert requests == []


@pytest.mark.parametrize("payload", [b"not JSON", b'{"dataType":', b"\xff"])
def test_malformed_json_is_terminal(payload):
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(payload, content_type="application/json", final_url=API_URL)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == "invalid_response"
    assert requests == [API_URL]


def test_short_substantive_description_is_accepted_without_optional_fields():
    data = record()
    data["containers"]["cna"] = {"descriptions": [{"lang": "en", "value": "Memory corruption."}]}
    result = fetch_article(URL, opener=lambda request, timeout: response(data), resolver=resolver_for({}))
    assert result.method == "cve_api"
    assert "Memory corruption." in result.text
    assert result.source_evidence.title == ""
