from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable, Mapping

from pypdf import PdfReader


CLIENT_ID_ENV = "PDF_SERVICES_CLIENT_ID"
CLIENT_SECRET_ENV = "PDF_SERVICES_CLIENT_SECRET"
DEFAULT_CONNECT_TIMEOUT_MS = 30_000
DEFAULT_READ_TIMEOUT_MS = 120_000


class AdobePDFExtractionFailure(RuntimeError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def credentials_status(env: Mapping[str, str] | None = None) -> str:
    """Return disabled, incomplete, or configured without exposing credentials."""
    values = os.environ if env is None else env
    has_client_id = bool(values.get(CLIENT_ID_ENV, "").strip())
    has_client_secret = bool(values.get(CLIENT_SECRET_ENV, "").strip())
    if has_client_id and has_client_secret:
        return "configured"
    if has_client_id or has_client_secret:
        return "incomplete"
    return "disabled"


def extract_pdf_bytes(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
    connect_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
    read_timeout_ms: int = DEFAULT_READ_TIMEOUT_MS,
    env: Mapping[str, str] | None = None,
    converter: Callable[..., bytes] | None = None,
) -> str:
    """Convert one bounded PDF to Markdown through Adobe PDF Services."""
    values = os.environ if env is None else env
    if credentials_status(values) != "configured":
        raise AdobePDFExtractionFailure(
            "Adobe PDF Services credentials are not fully configured",
            error_code="adobe_pdf_credentials_missing",
        )

    try:
        page_count = len(PdfReader(io.BytesIO(payload)).pages)
    except Exception as exc:
        raise AdobePDFExtractionFailure(
            "PDF parsing failed before Adobe conversion",
            error_code="pdf_parse_failed",
        ) from exc
    if page_count > max_pages:
        raise AdobePDFExtractionFailure(
            f"PDF has {page_count} pages; limit is {max_pages}",
            error_code="pdf_too_many_pages",
        )

    convert = converter or _convert_with_adobe
    markdown_bytes = convert(
        payload,
        client_id=values[CLIENT_ID_ENV].strip(),
        client_secret=values[CLIENT_SECRET_ENV].strip(),
        connect_timeout_ms=connect_timeout_ms,
        read_timeout_ms=read_timeout_ms,
    )
    try:
        markdown = markdown_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AdobePDFExtractionFailure(
            "Adobe PDF to Markdown returned invalid UTF-8",
            error_code="adobe_pdf_invalid_output",
        ) from exc
    if not markdown:
        raise AdobePDFExtractionFailure(
            "Adobe PDF to Markdown returned empty content",
            error_code="adobe_pdf_empty_content",
        )
    if len(markdown.encode("utf-8")) > max_text_bytes:
        raise AdobePDFExtractionFailure(
            "Adobe PDF to Markdown output is too large",
            error_code="extracted_content_too_large",
        )
    return markdown


def _convert_with_adobe(
    payload: bytes,
    *,
    client_id: str,
    client_secret: str,
    connect_timeout_ms: int,
    read_timeout_ms: int,
) -> bytes:
    try:
        from adobe.pdfservices.operation.auth.service_principal_credentials import (
            ServicePrincipalCredentials,
        )
        from adobe.pdfservices.operation.config.client_config import ClientConfig
        from adobe.pdfservices.operation.exception.exceptions import (
            SdkException,
            ServiceApiException,
            ServiceUsageException,
        )
        from adobe.pdfservices.operation.pdf_services import PDFServices
        from adobe.pdfservices.operation.pdf_services_media_type import (
            PDFServicesMediaType,
        )
        from adobe.pdfservices.operation.pdfjobs.jobs.pdf_to_markdown_job import (
            PDFToMarkdownJob,
        )
        from adobe.pdfservices.operation.pdfjobs.params.pdf_to_markdown.pdf_to_markdown_params import (
            PDFToMarkdownParams,
        )
        from adobe.pdfservices.operation.pdfjobs.result.pdf_to_markdown_result import (
            PDFToMarkdownResult,
        )
    except ImportError as exc:
        raise AdobePDFExtractionFailure(
            "Adobe PDF Services SDK is unavailable",
            error_code="adobe_pdf_dependency_missing",
        ) from exc

    credentials = ServicePrincipalCredentials(
        client_id=client_id,
        client_secret=client_secret,
    )
    client_config = ClientConfig(
        connect_timeout=connect_timeout_ms,
        read_timeout=read_timeout_ms,
    )
    service = PDFServices(credentials=credentials, client_config=client_config)
    input_asset = None
    output_asset = None
    try:
        with io.BytesIO(payload) as input_stream:
            input_asset = service.upload(input_stream, PDFServicesMediaType.PDF)
        params = PDFToMarkdownParams(get_figures=False)
        job = PDFToMarkdownJob(
            input_asset=input_asset,
            pdf_to_markdown_params=params,
        )
        location = service.submit(job)
        response = service.get_job_result(location, PDFToMarkdownResult)
        output_asset = response.get_result().get_asset()
        stream_asset = service.get_content(output_asset)
        mime_type = stream_asset.get_mime_type().partition(";")[0].strip().lower()
        if mime_type != "text/markdown":
            raise AdobePDFExtractionFailure(
                "Adobe PDF to Markdown returned an unexpected content type",
                error_code="adobe_pdf_invalid_output",
            )
        return stream_asset.get_input_stream()
    except ServiceUsageException as exc:
        raise AdobePDFExtractionFailure(
            "Adobe PDF Services quota is unavailable",
            error_code="adobe_pdf_quota_exhausted",
        ) from exc
    except ServiceApiException as exc:
        raise AdobePDFExtractionFailure(
            "Adobe PDF Services rejected the conversion",
            error_code="adobe_pdf_service_failed",
        ) from exc
    except SdkException as exc:
        raise AdobePDFExtractionFailure(
            "Adobe PDF Services request failed",
            error_code="adobe_pdf_request_failed",
        ) from exc
    except AdobePDFExtractionFailure:
        raise
    except Exception as exc:
        raise AdobePDFExtractionFailure(
            "Adobe PDF to Markdown conversion failed",
            error_code="adobe_pdf_conversion_failed",
        ) from exc
    finally:
        for asset in (output_asset, input_asset):
            if asset is None:
                continue
            try:
                service.delete_asset(asset)
            except Exception:
                pass


def main() -> int:
    max_pages = int(sys.argv[1])
    max_text_bytes = int(sys.argv[2])
    connect_timeout_ms = int(sys.argv[3])
    read_timeout_ms = int(sys.argv[4])
    address_space_limit = int(sys.argv[5])
    _set_address_space_limit(address_space_limit)
    payload = sys.stdin.buffer.read()

    try:
        text = extract_pdf_bytes(
            payload,
            max_pages=max_pages,
            max_text_bytes=max_text_bytes,
            connect_timeout_ms=connect_timeout_ms,
            read_timeout_ms=read_timeout_ms,
        )
        result = {"status": "success", "text": text}
    except AdobePDFExtractionFailure as exc:
        result = {
            "status": "error",
            "error_code": exc.error_code,
            "message": str(exc),
        }
    except Exception:
        result = {
            "status": "error",
            "error_code": "adobe_pdf_worker_failed",
            "message": "Adobe PDF worker failed",
        }

    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


def _set_address_space_limit(limit_bytes: int) -> None:
    if limit_bytes <= 0:
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ImportError, OSError, ValueError):
        return


if __name__ == "__main__":
    raise SystemExit(main())
