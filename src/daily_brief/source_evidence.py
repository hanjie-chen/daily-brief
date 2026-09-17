"""Small, publisher-declared source-identity signals for recovered articles.

This deliberately collects only explicit statements made in an article header or
introductory metadata.  It is not a general link scraper: links in prose,
quotes, navigation, and comment threads must not establish source identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from lxml import etree
from lxml import html as lxml_html


_RELATION_PATTERN = re.compile(
    r"\b(?P<crosspost>cross[ -]?posted\s+from)\b|"
    r"\b(?P<republication>originally\s+published\s+(?:at|on)|"
    r"republished\s+from|first\s+published\s+(?:at|on))\b|"
    r"\b(?P<narration>narration\s+of|reading\s+of|audio\s+version\s+of)\b",
    re.IGNORECASE,
)
_EXCLUDED_CONTEXT = re.compile(
    r"(?:comment|reply|discussion|nav|menu|footer|sidebar|related|share|"
    r"breadcrumb|cookie|modal)", re.IGNORECASE,
)
_MAX_CONTEXT_CHARS = 500
MAX_RELATIONS = 8


@dataclass(frozen=True)
class SourceRelation:
    url: str
    context: str
    kind: str


@dataclass(frozen=True)
class SourceEvidence:
    title: str
    author: str = ""
    relations: tuple[SourceRelation, ...] = ()


def extract_html_source_evidence(markup: str, page_url: str) -> SourceEvidence:
    """Read narrow source identity signals from server-rendered publisher HTML."""
    parser = lxml_html.HTMLParser(encoding="utf-8", no_network=True, huge_tree=False)
    try:
        document = lxml_html.document_fromstring(markup.encode("utf-8"), parser=parser)
    except (etree.ParserError, etree.XMLSyntaxError):
        return SourceEvidence(title="")

    title = _first_text(document.xpath("//article//h1 | //main//h1 | //h1"))
    if not title:
        title = _first_text(document.xpath("./head/title"))
    author = _metadata_content(document, "article:author")
    header_nodes = _header_nodes(document)
    if not author:
        author = _author_from_headers(header_nodes)
    relations = _relations_from_headers(header_nodes, page_url)
    canonical = _canonical_relation(document, page_url)
    if canonical is not None:
        relations += (canonical,)
    return SourceEvidence(title=title, author=author, relations=relations[:MAX_RELATIONS])


def extract_markdown_source_evidence(
    content: str,
    page_url: str,
    *,
    title: str = "",
    author: str = "",
    description: str = "",
) -> SourceEvidence:
    """Collect equivalent bounded signals from Reader/YouTube publisher metadata."""
    clean_title = _clean(title)[:512] or _markdown_heading(content)
    clean_author = _clean(author)[:256]
    intro = "\n".join((description[:2000], content[:4000]))
    relations = _relations_from_text(intro, page_url)
    return SourceEvidence(title=clean_title, author=clean_author, relations=relations)


def _header_nodes(document) -> tuple:
    # Explicit relationships are accepted only in an article header, or in a
    # short metadata region immediately preceding the article body.
    nodes = []
    seen = set()
    for node in document.xpath("//article/header | //main//header | //header"):
        if _excluded(node) or id(node) in seen:
            continue
        nodes.append(node)
        seen.add(id(node))
    article = next(iter(document.xpath("//article")), None)
    if article is not None:
        for child in list(article)[:3]:
            if (child.tag in {"header", "h1", "p", "div"}
                    and len(child.text_content()) <= 1000
                    and not _excluded(child)):
                if id(child) not in seen:
                    nodes.append(child)
                    seen.add(id(child))
    return tuple(nodes[:20])


def _relations_from_headers(nodes: tuple, page_url: str) -> tuple[SourceRelation, ...]:
    relations: list[SourceRelation] = []
    for node in nodes:
        for anchor in node.xpath(".//a[@href]"):
            relation = _relation_for_anchor(anchor, page_url)
            if relation is not None and relation not in relations:
                relations.append(relation)
                if len(relations) >= MAX_RELATIONS:
                    return tuple(relations)
    return tuple(relations)


def _relations_from_text(text: str, page_url: str) -> tuple[SourceRelation, ...]:
    # Markdown Reader output has no DOM.  Restrict matching to the first 4 KB,
    # and only accept an explicit relation immediately adjacent to its link.
    relations: list[SourceRelation] = []
    link_pattern = re.compile(r"^\s*:?\s*(?:\[[^\]]{1,160}\]\((https?://[^)\s]+)\)|(https?://[^\s<>]+))")
    nonempty = 0
    in_code = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if not line:
            continue
        nonempty += 1
        if nonempty > 12 or re.match(r"^#+\s*(?:comments|discussion)\b", line, re.I):
            break
        if in_code or line.startswith('>') or len(line) > _MAX_CONTEXT_CHARS:
            continue
        match = _RELATION_PATTERN.search(line)
        if not match:
            continue
        prefix = line[:match.start()].strip()
        # Allow a metadata separator/byline before the declaration, not prose
        # quoting someone else's cross-post statement.
        if prefix and not prefix.endswith(('·', '|')):
            continue
        link = link_pattern.match(line[match.end():])
        if link is None:
            continue
        href = urljoin(page_url, (link.group(1) or link.group(2)).rstrip('.,;'))
        if not href.startswith(("http://", "https://")):
            continue
        context = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        relation = SourceRelation(href, _clean(context), match.lastgroup)
        if relation not in relations:
            relations.append(relation)
        if len(relations) >= MAX_RELATIONS:
            break
    return tuple(relations)


def _relation_for_anchor(anchor, page_url: str) -> SourceRelation | None:
    parent = anchor.getparent()
    if parent is None or _excluded(anchor):
        return None
    context = _clean(parent.text_content())
    if len(context) > _MAX_CONTEXT_CHARS:
        return None
    prefix = parent.text or ""
    for sibling in parent:
        if sibling is anchor:
            break
        if isinstance(sibling.tag, str):
            prefix += sibling.text_content()
        prefix += sibling.tail or ""
    prefix = _clean(prefix)
    match = _RELATION_PATTERN.search(prefix)
    href = urljoin(page_url, anchor.get("href") or "")
    if (match is None or prefix[match.end():].strip(' :')
            or not href.startswith(("http://", "https://"))):
        return None
    before = prefix[:match.start()].strip()
    if before and not before.endswith(('·', '|')):
        return None
    return SourceRelation(url=href, context=context, kind=match.lastgroup)


def _canonical_relation(document, page_url: str) -> SourceRelation | None:
    canonical = document.xpath("./head/link[translate(@rel, 'CANONICAL', 'canonical')='canonical']/@href")
    if not canonical:
        return None
    href = urljoin(page_url, canonical[0])
    if not href.startswith(("http://", "https://")):
        return None
    return SourceRelation(url=href, context="Publisher-declared canonical URL.", kind="canonical")


def _author_from_headers(nodes: tuple) -> str:
    for node in nodes:
        for candidate in node.xpath(".//*[@rel='author'] | .//*[contains(translate(@class, 'BYLINE', 'byline'), 'byline')]"):
            value = _clean(candidate.text_content())
            if value:
                return value[:256]
        text = _clean(node.text_content())
        match = re.search(r"\bby\s+([A-Z][^·|]{1,120})", text)
        if match:
            return _clean(match.group(1))[:256]
        for part in node.xpath('.//p'):
            segments = _clean(part.text_content()).split('·')
            if len(segments) == 3 and _RELATION_PATTERN.search(segments[-1]):
                return segments[1].strip()[:256]
    return ""


def _metadata_content(document, property_name: str) -> str:
    values = document.xpath(
        "./head/meta[translate(@property, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')=$name]/@content",
        name=property_name,
    )
    return _clean(values[0])[:256] if values else ""


def _markdown_heading(content: str) -> str:
    match = re.search(r"(?m)^#{1,2}\s+(.{1,512})$", content[:4000])
    return _clean(match.group(1)) if match else ""


def _first_text(nodes) -> str:
    for node in nodes:
        value = _clean(node.text_content())
        if value:
            return value[:512]
    return ""


def _excluded(node) -> bool:
    for ancestor in (node, *node.iterancestors()):
        if ancestor.tag in {'blockquote', 'q', 'pre', 'code', 'script', 'style'}:
            return True
        if 'hidden' in ancestor.attrib or ancestor.get('aria-hidden') == 'true':
            return True
        marker = " ".join((ancestor.get("id") or "", ancestor.get("class") or "", ancestor.tag or ""))
        if _EXCLUDED_CONTEXT.search(marker):
            return True
    return False


def _clean(value: str) -> str:
    return " ".join((value or "").split())
