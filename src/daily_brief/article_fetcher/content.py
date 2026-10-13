"""Conservative shell detection shared by HTML retrieval and Reader output."""

from __future__ import annotations

import re


# Match entire notices, never mentions of JavaScript inside ordinary prose.
_JS_NOTICE = re.compile(
    r"(?:"
    r"you have javascript disabled|"
    r"javascript is (?:disabled|not enabled)|"
    r"(?:please )?enable javascript(?: in (?:your |the )browser(?: settings)?)?"
    r"(?: (?:to (?:continue|view this (?:page|website)|use this (?:site|website|app))"
    r"|and (?:reload|refresh) (?:this |the )page))?|"
    r"you (?:need to|must) enable javascript to (?:run this app|use this (?:site|website))|"
    r"(?:this (?:site|website|page|app|application)) requires javascript"
    r"(?: to (?:run|work|function)(?: properly)?)?|"
    r"to use the site, you must enable javascript in the browser settings and reload the page"
    r")[.!]?",
    re.IGNORECASE,
)
_NAVIGATION = re.compile(
    r"(?:(?:home|menu|navigation|about(?: us)?|contact(?: us)?|search|"
    r"sign in|log in|login|sign up|register|privacy(?: policy)?|"
    r"terms(?: of (?:use|service))?|cookie policy|skip to content|"
    r"region|select region|choose region|language|select language)"
    r"(?:\s*[|/•·›>]\s*|\s+|$))+",
    re.IGNORECASE,
)
# Only interpreted as options when a corresponding selector label is present.
# Unknown options/prose keep the page; this is not a geography/language classifier.
_REGION_OPTIONS = re.compile(
    r"(?:(?:all|global|worldwide|africa|americas|asia|europe|australia|"
    r"russia|kazakhstan|china|japan|india|united states|united kingdom|us|uk|eu)"
    r"(?:\s*[|/•·,]\s*|\s+|$))+", re.IGNORECASE,
)
_LANGUAGE_OPTIONS = re.compile(
    r"(?:(?:english|русский|қазақша|中文|日本語|deutsch|français|español)"
    r"(?:\s*[|/•·,]\s*|\s+|$))+", re.IGNORECASE,
)
_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^\n]*?\)")
_LINK = re.compile(r"\[([^\]\n]*)\]\([^\n]*?\)")


def _is_js_notice(line: str) -> bool:
    return all(_JS_NOTICE.fullmatch(part) for part in re.split(r"(?<=[.!?])\s+", line))


def is_page_shell(text: str, *, title: str = "") -> bool:
    """Reject only recognized notices/navigation with no other body remaining.

    Publisher metadata is not evidence of a body. Callers pass the extracted body
    separately, with an optional title to recognize a repeated site heading.
    No minimum length or semantic sufficiency requirement applies to real text.
    This function decides acceptance only; accepted material is never rewritten.
    """
    lines = []
    for raw_line in text.splitlines():
        # Long or code/quoted lines are ambiguous content, not a known shell.
        if len(raw_line) > 512 or raw_line.lstrip().startswith((">", "`", "~~~")):
            return False
        line = _LINK.sub(r"\1", _IMAGE.sub("", raw_line))
        line = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+)", "", line)
        line = " ".join(line.strip(" *_").split())
        if line and not re.fullmatch(r"[-=*_]+", line):
            lines.append(line)

    has_region = any(line.casefold() in {"region", "select region", "choose region"} for line in lines)
    has_language = any(line.casefold() in {"language", "select language"} for line in lines)
    has_js_notice = any(_is_js_notice(line) for line in lines)
    normalized_title = " ".join(title.split()).casefold()
    recognized_shell = False
    for line in lines:
        # Several notice sentences can share a paragraph after HTML extraction.
        if _is_js_notice(line):
            recognized_shell = True
        elif _NAVIGATION.fullmatch(line):
            recognized_shell = True
        elif has_region and _REGION_OPTIONS.fullmatch(line):
            continue
        elif has_language and _LANGUAGE_OPTIONS.fullmatch(line):
            continue
        elif (
            has_js_notice and normalized_title and line.casefold() == normalized_title
            and not re.search(r"[.!?\d]", line)
        ):
            # Only a repeated site heading alongside a JS notice can be ignored.
            # A title can itself be a short status/release announcement.
            continue
        elif (
            re.match(r"^(?:select |choose )?region\s+", line, re.IGNORECASE)
            and _REGION_OPTIONS.fullmatch(re.sub(
                r"^(?:select |choose )?region\s+", "", line, flags=re.IGNORECASE,
            ))
        ):
            recognized_shell = True
        else:
            return False
    return recognized_shell
