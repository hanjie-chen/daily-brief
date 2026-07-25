import hashlib
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from daily_brief.publisher import PublishError, publish_pending


def _payload(date_label="2026-07-25"):
    return {
        "schema_version": 1,
        "date": date_label,
        "generated_at": f"{date_label}T08:04:00+08:00",
        "timezone": "Asia/Singapore",
        "sections": {
            "ai": {"note": "", "items": [{"hn_item_id": "1"}]},
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

    result = publish_pending(
        brief_dir,
        data_dir,
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

    first = publish_pending(
        brief_dir, data_dir, endpoint="https://example.com", token="x", opener=opener
    )
    second = publish_pending(
        brief_dir, data_dir, endpoint="https://example.com", token="x", opener=opener
    )
    forced = publish_pending(
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

    result = publish_pending(
        brief_dir,
        tmp_path / "data",
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
        publish_pending(
            brief_dir,
            data_dir,
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

    with pytest.raises(PublishError, match="empty brief"):
        publish_pending(
            brief_dir,
            tmp_path / "data",
            endpoint="https://example.com",
            token="x",
            opener=lambda *_args, **_kwargs: FakeResponse(),
        )


def test_publish_requires_endpoint_and_token(tmp_path):
    with pytest.raises(PublishError, match="DAILY_BRIEF_PUBLISH_URL"):
        publish_pending(tmp_path, tmp_path, endpoint="", token="")

    with pytest.raises(PublishError, match="DAILY_BRIEF_PUBLISH_TOKEN"):
        publish_pending(tmp_path, tmp_path, endpoint="https://example.com", token="")
