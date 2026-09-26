from __future__ import annotations

import io
import json
import sys

from pypdf import PdfReader


class PDFExtractionFailure(RuntimeError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def extract_pdf_bytes(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
) -> str:
    """Extract bounded layout text from a PDF inside the worker process."""
    try:
        reader = PdfReader(io.BytesIO(payload))
        page_count = len(reader.pages)
    except Exception as exc:
        raise PDFExtractionFailure(
            f"PDF parsing failed: {exc}", error_code="pdf_parse_failed"
        ) from exc

    if page_count > max_pages:
        raise PDFExtractionFailure(
            f"PDF has {page_count} pages; limit is {max_pages}",
            error_code="pdf_too_many_pages",
        )

    extracted_pages: list[str] = []
    try:
        for page in reader.pages:
            page_text = (
                page.extract_text(extraction_mode="layout")
                if "/Contents" in page
                else ""
            ) or ""
            lines = [" ".join(line.split()) for line in page_text.splitlines()]
            normalized_page = "\n".join(line for line in lines if line)
            if normalized_page:
                extracted_pages.append(normalized_page)
            candidate = "\n\n".join(extracted_pages)
            if len(candidate.encode("utf-8")) > max_text_bytes:
                raise PDFExtractionFailure(
                    "extracted PDF text is too large",
                    error_code="extracted_content_too_large",
                )
    except PDFExtractionFailure:
        raise
    except Exception as exc:
        raise PDFExtractionFailure(
            f"PDF text extraction failed: {exc}",
            error_code="pdf_parse_failed",
        ) from exc

    text = "\n\n".join(extracted_pages).strip()
    if not text:
        raise PDFExtractionFailure(
            "PDF contains no extractable text",
            error_code="pdf_no_extractable_text",
        )
    return text


def _set_address_space_limit(limit_bytes: int) -> None:
    if limit_bytes <= 0:
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ImportError, OSError, ValueError):
        # Download, page, output, and parent-process timeout limits still apply on
        # platforms that cannot enforce RLIMIT_AS reliably.
        return


def main() -> int:
    max_pages = int(sys.argv[1])
    max_text_bytes = int(sys.argv[2])
    address_space_limit = int(sys.argv[3])
    _set_address_space_limit(address_space_limit)
    payload = sys.stdin.buffer.read()

    try:
        text = extract_pdf_bytes(
            payload,
            max_pages=max_pages,
            max_text_bytes=max_text_bytes,
        )
        result = {"status": "success", "text": text}
    except PDFExtractionFailure as exc:
        result = {
            "status": "error",
            "error_code": exc.error_code,
            "message": str(exc),
        }
    except Exception as exc:
        result = {
            "status": "error",
            "error_code": "pdf_parse_failed",
            "message": f"PDF worker failed: {exc}",
        }

    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
