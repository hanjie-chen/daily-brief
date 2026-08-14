from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
YOUTUBE_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_MAX_METADATA_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_CAPTION_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TEXT_BYTES = 256 * 1024
MAX_DIAGNOSTIC_CHARS = 500


class YoutubeCaptionError(RuntimeError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class YoutubeCaptionResult:
    text: str
    language: str
    generated: bool


def youtube_video_id(url: str) -> str | None:
    """Return the ID for one supported public YouTube video URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username is not None:
        return None
    hostname = (parsed.hostname or "").lower()

    if hostname in YOUTUBE_SHORT_HOSTS:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif hostname in YOUTUBE_HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            candidate = values[0] if len(values) == 1 else ""
        else:
            parts = [part for part in parsed.path.split("/") if part]
            candidate = (
                parts[1]
                if len(parts) == 2 and parts[0] in {"embed", "live", "shorts"}
                else ""
            )
    else:
        return None

    return candidate if YOUTUBE_VIDEO_ID_PATTERN.fullmatch(candidate) else None


def fetch_youtube_caption(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES,
    max_caption_bytes: int = DEFAULT_MAX_CAPTION_BYTES,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    runner=None,
) -> YoutubeCaptionResult:
    """Fetch one preferred YouTube caption track without downloading media."""
    video_id = youtube_video_id(url)
    if video_id is None:
        raise YoutubeCaptionError(
            "unsupported YouTube video URL",
            error_code="youtube_invalid_url",
        )
    run = runner or subprocess.run

    with tempfile.TemporaryDirectory(prefix="daily-brief-youtube-") as directory:
        output_dir = Path(directory)
        metadata_command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-cache-dir",
            "--no-playlist",
            "--no-progress",
            "--quiet",
            "--no-warnings",
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--skip-download",
            "--write-info-json",
            "--paths",
            str(output_dir),
            "--output",
            "%(id)s.%(ext)s",
            url,
        ]
        _run_yt_dlp(
            metadata_command,
            runner=run,
            timeout_seconds=timeout_seconds,
            error_code="youtube_metadata_failed",
        )

        metadata_path = output_dir / f"{video_id}.info.json"
        metadata = _read_json_object(
            metadata_path,
            max_bytes=max_metadata_bytes,
            error_code="youtube_metadata_invalid",
        )
        if metadata.get("id") != video_id:
            raise YoutubeCaptionError(
                "yt-dlp metadata did not match the requested video",
                error_code="youtube_metadata_invalid",
            )

        language, generated = _select_caption_track(metadata)
        caption_flag = "--write-auto-subs" if generated else "--write-subs"
        caption_command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-cache-dir",
            "--no-playlist",
            "--no-progress",
            "--quiet",
            "--no-warnings",
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--skip-download",
            caption_flag,
            "--sub-langs",
            f"^{re.escape(language)}$",
            "--sub-format",
            "json3",
            "--paths",
            str(output_dir),
            "--output",
            "%(id)s.%(ext)s",
            "--load-info-json",
            str(metadata_path),
        ]
        _run_yt_dlp(
            caption_command,
            runner=run,
            timeout_seconds=timeout_seconds,
            error_code="youtube_caption_download_failed",
        )

        caption_paths = list(output_dir.glob(f"{video_id}.*.json3"))
        if len(caption_paths) != 1:
            raise YoutubeCaptionError(
                "yt-dlp did not produce exactly one JSON3 caption file",
                error_code="youtube_caption_invalid",
            )
        payload = _read_json_object(
            caption_paths[0],
            max_bytes=max_caption_bytes,
            error_code="youtube_caption_invalid",
        )
        text = parse_youtube_json3(payload, max_text_bytes=max_text_bytes)
        return YoutubeCaptionResult(
            text=text,
            language=language,
            generated=generated,
        )


def parse_youtube_json3(
    payload: dict,
    *,
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
) -> str:
    """Flatten JSON3 caption events into bounded text for summarization."""
    events = payload.get("events")
    if not isinstance(events, list):
        raise YoutubeCaptionError(
            "YouTube JSON3 captions do not contain an events array",
            error_code="youtube_caption_invalid",
        )

    lines = []
    text_bytes = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        parts = [
            segment.get("utf8", "")
            for segment in segments
            if isinstance(segment, dict) and isinstance(segment.get("utf8", ""), str)
        ]
        line = " ".join("".join(parts).split())
        if not line:
            continue
        line_bytes = len(line.encode("utf-8")) + (1 if lines else 0)
        text_bytes += line_bytes
        if text_bytes > max_text_bytes:
            raise YoutubeCaptionError(
                "extracted YouTube captions exceeded the text limit",
                error_code="extracted_content_too_large",
            )
        lines.append(line)

    if not lines:
        raise YoutubeCaptionError(
            "YouTube caption track contained no text",
            error_code="youtube_caption_empty",
        )
    return "\n".join(lines)


def _select_caption_track(metadata: dict) -> tuple[str, bool]:
    manual = _valid_caption_languages(metadata.get("subtitles"))
    automatic = _valid_caption_languages(metadata.get("automatic_captions"))
    declared = metadata.get("language")
    language = declared if isinstance(declared, str) else ""

    matching_manual = _matching_languages(manual, language)
    if matching_manual:
        return matching_manual[0], False

    original_automatic = sorted(
        code for code in automatic if code.casefold().endswith("-orig")
    )
    matching_original = _matching_languages(original_automatic, language)
    if matching_original:
        return matching_original[0], True
    if original_automatic:
        return original_automatic[0], True

    if manual:
        return sorted(manual)[0], False

    matching_automatic = _matching_languages(automatic, language)
    if matching_automatic:
        return matching_automatic[0], True
    if automatic:
        return sorted(automatic)[0], True

    raise YoutubeCaptionError(
        "the YouTube video has no available caption track",
        error_code="youtube_captions_unavailable",
    )


def _valid_caption_languages(value) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [
        language
        for language, formats in value.items()
        if isinstance(language, str)
        and YOUTUBE_LANGUAGE_PATTERN.fullmatch(language)
        and isinstance(formats, list)
        and formats
    ]


def _matching_languages(languages: list[str], declared: str) -> list[str]:
    if not declared:
        return []
    normalized = declared.casefold()
    base = normalized.split("-", 1)[0]
    return sorted(
        language
        for language in languages
        if language.casefold() == normalized
        or language.casefold().split("-", 1)[0] == base
    )


def _run_yt_dlp(
    command: list[str],
    *,
    runner,
    timeout_seconds: int,
    error_code: str,
) -> None:
    try:
        completed = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise YoutubeCaptionError(
            "yt-dlp timed out while retrieving YouTube captions",
            error_code="youtube_caption_timeout",
        ) from exc
    except Exception as exc:
        raise YoutubeCaptionError(
            f"yt-dlp could not start: {_diagnostic(str(exc))}",
            error_code=error_code,
        ) from exc

    if completed.returncode != 0:
        diagnostic = _diagnostic(
            completed.stderr.decode("utf-8", errors="replace")
        )
        raise YoutubeCaptionError(
            f"yt-dlp failed: {diagnostic or 'no diagnostic'}",
            error_code=error_code,
        )


def _read_json_object(path: Path, *, max_bytes: int, error_code: str) -> dict:
    try:
        if path.stat().st_size > max_bytes:
            raise YoutubeCaptionError(
                "yt-dlp output exceeded the size limit",
                error_code=error_code,
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except YoutubeCaptionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YoutubeCaptionError(
            "yt-dlp returned an invalid JSON file",
            error_code=error_code,
        ) from exc
    if not isinstance(payload, dict):
        raise YoutubeCaptionError(
            "yt-dlp JSON output is not an object",
            error_code=error_code,
        )
    return payload


def _diagnostic(value: str) -> str:
    return " ".join(value.split())[:MAX_DIAGNOSTIC_CHARS]
