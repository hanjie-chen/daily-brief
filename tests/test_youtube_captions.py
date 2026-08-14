import json
import subprocess
from pathlib import Path

import pytest

from daily_brief.youtube_captions import (
    YoutubeCaptionError,
    _select_caption_track,
    fetch_youtube_caption,
    parse_youtube_json3,
    youtube_video_id,
)


TARGET_URL = "https://www.youtube.com/watch?v=68X8yEatepQ"
TARGET_VIDEO_ID = "68X8yEatepQ"


@pytest.mark.parametrize(
    "url",
    [
        TARGET_URL,
        "https://youtu.be/68X8yEatepQ",
        "https://www.youtube.com/embed/68X8yEatepQ",
        "https://www.youtube.com/live/68X8yEatepQ",
        "https://www.youtube.com/shorts/68X8yEatepQ",
    ],
)
def test_youtube_video_id_recognizes_supported_video_urls(url):
    assert youtube_video_id(url) == TARGET_VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=68X8yEatepQ",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/playlist?list=68X8yEatepQ",
        "https://www.youtube.com/watch?v=too-short",
        "https://user@www.youtube.com/watch?v=68X8yEatepQ",
    ],
)
def test_youtube_video_id_rejects_non_video_or_ambiguous_urls(url):
    assert youtube_video_id(url) is None


def test_parse_youtube_json3_flattens_target_video_style_events():
    payload = {
        "events": [
            {
                "tStartMs": 0,
                "segs": [
                    {"utf8": "So, 70% to 75% of AI revenues of Google,"},
                ],
            },
            {"tStartMs": 3670},
            {
                "tStartMs": 3680,
                "segs": [
                    {"utf8": "Amazon, and Microsoft, and 7% of\n"},
                    {"utf8": "Microsoft's annual revenue total is for OpenAI."},
                ],
            },
        ]
    }

    assert parse_youtube_json3(payload) == (
        "So, 70% to 75% of AI revenues of Google,\n"
        "Amazon, and Microsoft, and 7% of Microsoft's annual revenue total is for OpenAI."
    )


def test_parse_youtube_json3_enforces_clean_text_limit():
    with pytest.raises(YoutubeCaptionError) as caught:
        parse_youtube_json3(
            {"events": [{"segs": [{"utf8": "abcd"}]}]},
            max_text_bytes=3,
        )

    assert caught.value.error_code == "extracted_content_too_large"


def test_select_caption_prefers_declared_language_manual_track():
    assert _select_caption_track(
        {
            "language": "en",
            "subtitles": {
                "fr": [{"ext": "json3"}],
                "en": [{"ext": "json3"}],
            },
            "automatic_captions": {"en-orig": [{"ext": "json3"}]},
        }
    ) == ("en", False)


def test_select_caption_uses_original_automatic_track_before_translation():
    assert _select_caption_track(
        {
            "language": "en",
            "subtitles": {},
            "automatic_captions": {
                "zh-Hans": [{"ext": "json3"}],
                "en-orig": [{"ext": "json3"}],
            },
        }
    ) == ("en-orig", True)


def test_fetch_youtube_caption_uses_yt_dlp_without_downloading_media():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        output_dir = Path(command[command.index("--paths") + 1])
        if "--write-info-json" in command:
            (output_dir / f"{TARGET_VIDEO_ID}.info.json").write_text(
                json.dumps(
                    {
                        "id": TARGET_VIDEO_ID,
                        "language": "en",
                        "subtitles": {},
                        "automatic_captions": {
                            "en-orig": [{"ext": "json3"}]
                        },
                    }
                ),
                encoding="utf-8",
            )
        else:
            (output_dir / f"{TARGET_VIDEO_ID}.en-orig.json3").write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "segs": [
                                    {
                                        "utf8": (
                                            "No one has proven that there is a "
                                            "sustainable business."
                                        )
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    result = fetch_youtube_caption(TARGET_URL, runner=runner)

    assert result.text == "No one has proven that there is a sustainable business."
    assert result.language == "en-orig"
    assert result.generated is True
    assert len(calls) == 2
    for command, kwargs in calls:
        assert "--ignore-config" in command
        assert "--no-playlist" in command
        assert "--skip-download" in command
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 45
    assert "--write-auto-subs" in calls[1][0]
    assert "--load-info-json" in calls[1][0]


def test_fetch_youtube_caption_reports_missing_tracks_without_second_command():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        output_dir = Path(command[command.index("--paths") + 1])
        (output_dir / f"{TARGET_VIDEO_ID}.info.json").write_text(
            json.dumps(
                {
                    "id": TARGET_VIDEO_ID,
                    "language": "en",
                    "subtitles": {},
                    "automatic_captions": {},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    with pytest.raises(YoutubeCaptionError) as caught:
        fetch_youtube_caption(TARGET_URL, runner=runner)

    assert caught.value.error_code == "youtube_captions_unavailable"
    assert len(calls) == 1
