from __future__ import annotations

import re

from lxml import etree
from lxml import html as lxml_html
import trafilatura


_TABLE_TAG_PATTERN = re.compile(r"<table(?:\s|>)", re.IGNORECASE)
_IMPORTANT_SUFFIX_PATTERN = re.compile(r"\s*!\s*important\s*$", re.IGNORECASE)
_METADATA_TITLE_MAX_CHARS = 512
_METADATA_DESCRIPTION_MAX_CHARS = 2000


def extract_html(markup: str) -> str:
    """Extract body text and separately labeled, bounded publisher metadata."""
    extracted = trafilatura.extract(
        _normalize_semantic_tables(markup),
        include_comments=False,
        favor_precision=True,
        include_tables=True,
    )
    body = _normalize_extracted_blocks(extracted or "")
    # Metadata supplements body text; it must not suppress empty-body recovery.
    if not body:
        return ""
    metadata = _extract_page_metadata(markup)
    if not metadata:
        return body
    return (
        "Page metadata (publisher-provided context, not article body):\n"
        + "\n".join(metadata)
        + "\n\nExtracted body:\n"
        + body
    )


def _extract_page_metadata(markup: str) -> list[str]:
    parser = lxml_html.HTMLParser(
        encoding="utf-8", no_network=True, huge_tree=False,
    )
    try:
        document = lxml_html.document_fromstring(markup.encode("utf-8"), parser=parser)
    except (etree.ParserError, etree.XMLSyntaxError):
        return []

    fields: dict[str, str] = {}
    for element in document.xpath("./head/title | ./head/meta"):
        if element.tag == "title":
            name = "title"
            value = element.text_content()
            limit = _METADATA_TITLE_MAX_CHARS
        else:
            name = (element.get("name") or element.get("property") or "").strip().casefold()
            if name not in {"description", "og:description"}:
                continue
            value = element.get("content") or ""
            limit = _METADATA_DESCRIPTION_MAX_CHARS
        value = " ".join(value.split())[:limit]
        if value and name not in fields:
            fields[name] = value

    result = []
    seen: set[str] = set()
    for name in ("title", "description", "og:description"):
        value = fields.get(name, "")
        if value and value.casefold() not in seen:
            result.append(f"{name}: {value}")
            seen.add(value.casefold())
    return result


def _normalize_semantic_tables(markup: str) -> str:
    """Preserve narrowly scoped table semantics that Trafilatura discards."""
    if not markup or _TABLE_TAG_PATTERN.search(markup) is None:
        return markup

    parser = lxml_html.HTMLParser(
        encoding="utf-8",
        no_network=True,
        huge_tree=False,
    )
    try:
        document = lxml_html.document_fromstring(markup.encode("utf-8"), parser=parser)
    except (etree.ParserError, etree.XMLSyntaxError):
        return markup

    changed = False
    for element in document.xpath(".//table//*"):
        if _is_explicitly_hidden(element):
            _drop_element_preserving_tail(element)
            changed = True

    for table in document.iter("table"):
        for header in table.iter("th"):
            if (header.get("scope") or "").strip().casefold() != "row":
                continue
            for span in tuple(header.iterdescendants("span")):
                if (
                    span.getparent() is not None
                    and len(span) == 0
                    and not (span.text or "").strip()
                    and (span.tail or "").strip()
                ):
                    span.drop_tag()
                    changed = True

        for time_element in tuple(table.iterdescendants("time")):
            if (
                time_element.getparent() is not None
                and time_element.text_content().strip()
            ):
                time_element.drop_tag()
                changed = True

    if not changed:
        return markup
    return etree.tostring(document, encoding="unicode", method="html")


def _is_explicitly_hidden(element) -> bool:
    if "hidden" in element.attrib:
        return True
    if (element.get("aria-hidden") or "").strip().casefold() == "true":
        return True

    for declaration in (element.get("style") or "").split(";"):
        property_name, separator, value = declaration.partition(":")
        if not separator:
            continue
        property_name = property_name.strip().casefold()
        value = _IMPORTANT_SUFFIX_PATTERN.sub("", value).strip().casefold()
        if property_name == "display" and value == "none":
            return True
        if property_name == "visibility" and value == "hidden":
            return True
    return False


def _drop_element_preserving_tail(element) -> None:
    """Drop an element subtree without dropping following visible text."""
    parent = element.getparent()
    if parent is None:
        return
    tail = element.tail
    previous = element.getprevious()
    parent.remove(element)
    if not tail:
        return
    if previous is None:
        parent.text = f"{parent.text or ''}{tail}"
    else:
        previous.tail = f"{previous.tail or ''}{tail}"


def _normalize_extracted_blocks(value: str) -> str:
    """Normalize extractor text while retaining one line per content block."""
    lines = [line.rstrip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line.strip()).strip("\n")
