from io import BytesIO

import pytest
from pypdf import PdfWriter

from daily_brief.adobe_pdf_extractor import (
    AdobePDFExtractionFailure,
    credentials_status,
    extract_pdf_bytes,
)


def make_blank_pdf(page_count: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def configured_env() -> dict[str, str]:
    return {
        "PDF_SERVICES_CLIENT_ID": "test-client-id",
        "PDF_SERVICES_CLIENT_SECRET": "test-client-secret",
    }


def test_credentials_status_does_not_require_exposing_values():
    assert credentials_status({}) == "disabled"
    assert credentials_status({"PDF_SERVICES_CLIENT_ID": "client"}) == "incomplete"
    assert credentials_status(configured_env()) == "configured"


def test_extract_pdf_bytes_returns_bounded_markdown_from_converter():
    calls = []

    def convert(payload, **kwargs):
        calls.append((payload, kwargs))
        return b"# Abstract\n\nA clean paragraph.\n"

    pdf = make_blank_pdf()
    text = extract_pdf_bytes(
        pdf,
        max_pages=1,
        max_text_bytes=100,
        connect_timeout_ms=11_000,
        read_timeout_ms=22_000,
        env=configured_env(),
        converter=convert,
    )

    assert text == "# Abstract\n\nA clean paragraph."
    assert calls == [
        (
            pdf,
            {
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "connect_timeout_ms": 11_000,
                "read_timeout_ms": 22_000,
            },
        )
    ]


def test_extract_pdf_bytes_rejects_incomplete_credentials_before_conversion():
    with pytest.raises(AdobePDFExtractionFailure) as caught:
        extract_pdf_bytes(
            make_blank_pdf(),
            max_pages=1,
            max_text_bytes=100,
            env={"PDF_SERVICES_CLIENT_ID": "client"},
            converter=lambda *args, **kwargs: pytest.fail("must not convert"),
        )

    assert caught.value.error_code == "adobe_pdf_credentials_missing"


def test_extract_pdf_bytes_enforces_page_limit_before_conversion():
    with pytest.raises(AdobePDFExtractionFailure) as caught:
        extract_pdf_bytes(
            make_blank_pdf(2),
            max_pages=1,
            max_text_bytes=100,
            env=configured_env(),
            converter=lambda *args, **kwargs: pytest.fail("must not convert"),
        )

    assert caught.value.error_code == "pdf_too_many_pages"


@pytest.mark.parametrize(
    ("output", "max_text_bytes", "error_code"),
    [
        (b"", 100, "adobe_pdf_empty_content"),
        (b"\xff", 100, "adobe_pdf_invalid_output"),
        (b"too large", 3, "extracted_content_too_large"),
    ],
)
def test_extract_pdf_bytes_rejects_invalid_output(
    output, max_text_bytes, error_code
):
    with pytest.raises(AdobePDFExtractionFailure) as caught:
        extract_pdf_bytes(
            make_blank_pdf(),
            max_pages=1,
            max_text_bytes=max_text_bytes,
            env=configured_env(),
            converter=lambda *args, **kwargs: output,
        )

    assert caught.value.error_code == error_code
