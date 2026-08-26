import hashlib
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from daily_brief.publisher import PublishError, publish_brief


def _payload(date_label="2026-07-25"):
    return {
        "schema_version": 2,
        "date": date_label,
        "generated_at": f"{date_label}T08:04:00+08:00",
        "timezone": "Asia/Singapore",
        "sections": {
            "ai": {
                "note": "",
                "items": [
                    {
                        "hn_item_id": "1",
                        "title": "Example",
                        "summary": "Example summary",
                        "content_status": "ok",
                        "why": "Example reason",
                        "source_url": "https://example.com/story",
                        "discussion_url": "https://news.ycombinator.com/item?id=1",
                        "points": 10,
                        "comments": 2,
                    }
                ],
            },
            "non_ai_hot": {"note": "", "items": []},
        },
    }


def _write_brief(directory, date_label="2026-07-25"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{date_label}.json"
    path.write_text(json.dumps(_payload(date_label)), encoding="utf-8")
    return path


class FakeResponse:
    def __init__(self, status=201):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status


def test_publish_sends_auth_header_and_records_success_hash(tmp_path):
    brief_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"
    path = _write_brief(brief_dir)
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return FakeResponse()

    result = publish_brief(
        brief_dir,
        data_dir,
        date_label="2026-07-25",
        endpoint="https://hanjie-chen.com/internal/briefs",
        token="secret",
        opener=opener,
    )

    assert result.published == 1
    assert result.skipped == 0
    request, timeout = calls[0]
    assert request.full_url == "https://hanjie-chen.com/internal/briefs"
    assert request.get_header("X-daily-brief-token") == "secret"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("User-agent") == "daily-brief-publisher/1.0"
    assert timeout == 10
    state = json.loads((data_dir / "publish-state.json").read_text(encoding="utf-8"))
    assert (
        state["published"]["2026-07-25"]
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_publish_skips_unchanged_payload_and_force_resends(tmp_path):
    brief_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"
    _write_brief(brief_dir)
    calls = []

    def opener(request, timeout):
        calls.append(request)
        return FakeResponse(200)

    first = publish_brief(
        brief_dir,
        data_dir,
        date_label="2026-07-25",
        endpoint="https://example.com",
        token="x",
        opener=opener,
    )
    second = publish_brief(
        brief_dir,
        data_dir,
        date_label="2026-07-25",
        endpoint="https://example.com",
        token="x",
        opener=opener,
    )
    forced = publish_brief(
        brief_dir,
        data_dir,
        date_label="2026-07-25",
        force=True,
        endpoint="https://example.com",
        token="x",
        opener=opener,
    )

    assert first.published == 1
    assert second.skipped == 1
    assert forced.published == 1
    assert len(calls) == 2


def test_publish_retries_network_and_server_failures(tmp_path):
    brief_dir = tmp_path / "briefs"
    _write_brief(brief_dir)
    attempts = iter(
        [
            URLError("offline"),
            HTTPError("https://example.com", 503, "unavailable", {}, BytesIO()),
            FakeResponse(201),
        ]
    )
    sleeps = []

    def opener(request, timeout):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    result = publish_brief(
        brief_dir,
        tmp_path / "data",
        date_label="2026-07-25",
        endpoint="https://example.com",
        token="x",
        opener=opener,
        sleeper=sleeps.append,
    )

    assert result.published == 1
    assert sleeps == [1, 2]


def test_publish_does_not_retry_client_error_or_record_state(tmp_path):
    brief_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"
    _write_brief(brief_dir)
    calls = []

    def opener(request, timeout):
        calls.append(request)
        raise HTTPError("https://example.com", 403, "forbidden", {}, BytesIO())

    with pytest.raises(PublishError, match="HTTP 403"):
        publish_brief(
            brief_dir,
            data_dir,
            date_label="2026-07-25",
            endpoint="https://example.com",
            token="x",
            opener=opener,
            sleeper=lambda _seconds: None,
        )

    assert len(calls) == 1
    assert not (data_dir / "publish-state.json").exists()


def test_publish_rejects_empty_brief(tmp_path):
    brief_dir = tmp_path / "briefs"
    brief_dir.mkdir()
    payload = _payload()
    payload["sections"]["ai"]["items"] = []
    (brief_dir / "2026-07-25.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PublishError, match="at least one item"):
        publish_brief(
            brief_dir,
            tmp_path / "data",
            date_label="2026-07-25",
            endpoint="https://example.com",
            token="x",
            opener=lambda *_args, **_kwargs: FakeResponse(),
        )


def test_publish_repeatedly_skips_no_content_marker_without_network_or_state(
    tmp_path,
):
    brief_dir = tmp_path / "briefs"
    brief_dir.mkdir()
    (brief_dir / "2026-07-25.no-content").write_bytes(b"")
    calls = []

    first = publish_brief(
        brief_dir,
        tmp_path / "data",
        date_label="2026-07-25",
        endpoint="https://example.com",
        token="x",
        opener=lambda *_args, **_kwargs: calls.append(True),
    )
    second = publish_brief(
        brief_dir,
        tmp_path / "data",
        date_label="2026-07-25",
        endpoint="https://example.com",
        token="x",
        opener=lambda *_args, **_kwargs: calls.append(True),
    )

    assert first == second
    assert first.published == 0
    assert first.skipped == 1
    assert calls == []
    assert not (tmp_path / "data/publish-state.json").exists()


def test_existing_invalid_json_is_not_hidden_by_no_content_marker(tmp_path):
    brief_dir = tmp_path / "briefs"
    brief_dir.mkdir()
    (brief_dir / "2026-07-25.json").write_text("not json", encoding="utf-8")
    (brief_dir / "2026-07-25.no-content").write_bytes(b"")

    with pytest.raises(PublishError, match="invalid brief JSON"):
        publish_brief(
            brief_dir,
            tmp_path / "data",
            date_label="2026-07-25",
            endpoint="https://example.com",
            token="x",
            opener=lambda *_args, **_kwargs: pytest.fail(
                "invalid JSON must not reach the network"
            ),
        )


def test_valid_json_takes_precedence_over_stale_no_content_marker(tmp_path):
    brief_dir = tmp_path / "briefs"
    _write_brief(brief_dir)
    (brief_dir / "2026-07-25.no-content").write_bytes(b"")
    calls = []

    result = publish_brief(
        brief_dir,
        tmp_path / "data",
        date_label="2026-07-25",
        endpoint="https://example.com",
        token="x",
        opener=lambda request, timeout: calls.append(request) or FakeResponse(),
    )

    assert result.published == 1
    assert result.skipped == 0
    assert len(calls) == 1


def test_publish_rejects_schema_v1_before_network(tmp_path):
    brief_dir = tmp_path / "briefs"
    path = _write_brief(brief_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PublishError, match="unsupported schema_version"):
        publish_brief(
            brief_dir,
            tmp_path / "data",
            date_label="2026-07-25",
            endpoint="https://example.com",
            token="x",
            opener=lambda *_args, **_kwargs: pytest.fail(
                "invalid payload must not reach the network"
            ),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.pop("content_status"), "exact schema v2 fields"),
        (lambda item: item.update({"unexpected": True}), "exact schema v2 fields"),
        (
            lambda item: item.update({"content_status": "unknown"}),
            "unsupported content_status",
        ),
        (
            lambda item: item.update(
                {"discussion_url": ("https://news.ycombinator.com/item?id=999")}
            ),
            "discussion_url must match hn_item_id",
        ),
    ],
)
def test_publish_rejects_invalid_item_contract_before_network(
    tmp_path, mutation, message
):
    brief_dir = tmp_path / "briefs"
    path = _write_brief(brief_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload["sections"]["ai"]["items"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PublishError, match=message):
        publish_brief(
            brief_dir,
            tmp_path / "data",
            date_label="2026-07-25",
            endpoint="https://example.com",
            token="x",
            opener=lambda *_args, **_kwargs: pytest.fail(
                "invalid payload must not reach the network"
            ),
        )


def test_publish_requires_endpoint_and_token(tmp_path):
    with pytest.raises(PublishError, match="DAILY_BRIEF_PUBLISH_URL"):
        publish_brief(tmp_path, tmp_path, "2026-07-25", endpoint="", token="")

    with pytest.raises(PublishError, match="DAILY_BRIEF_PUBLISH_TOKEN"):
        publish_brief(
            tmp_path,
            tmp_path,
            "2026-07-25",
            endpoint="https://example.com",
            token="",
        )


def test_publish_requested_date_ignores_invalid_older_brief(tmp_path):
    brief_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"
    older_path = _write_brief(brief_dir, "2026-07-30")
    older_payload = json.loads(older_path.read_text(encoding="utf-8"))
    older_payload["sections"]["ai"]["items"] = []
    older_path.write_text(json.dumps(older_payload), encoding="utf-8")
    requested_path = _write_brief(brief_dir, "2026-07-31")
    calls = []

    def opener(request, timeout):
        calls.append(json.loads(request.data))
        return FakeResponse()

    result = publish_brief(
        brief_dir,
        data_dir,
        date_label="2026-07-31",
        endpoint="https://example.com",
        token="x",
        opener=opener,
    )

    assert result.published == 1
    assert result.skipped == 0
    assert calls == [json.loads(requested_path.read_text(encoding="utf-8"))]
    state = json.loads((data_dir / "publish-state.json").read_text(encoding="utf-8"))
    assert set(state["published"]) == {"2026-07-31"}
