"""Deterministic, source-preserving selection before model input construction.

Relevance scores locate evidence; they never establish a claim or sufficiency.
Offsets refer to the supplied extracted text, not HTML or a different retrieval.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from urllib.parse import unquote, urlsplit

DEFAULT_EVIDENCE_MAX_CHARS = 24_000
_BLOCK_CHARS = 800
_STOP_WORDS = frozenset(
    'a an the this that these those is are was were be been being to of in on at '
    'for from with without by and or but if then than as it its now new how why '
    'what when where who which does do did has have had can could will would '
    'should not no yes into about my your our their show hn ask reads supports'.split()
)
_TOKEN = re.compile(r'[a-z0-9][a-z0-9_.+-]*|[\u3400-\u9fff]+', re.I)


@dataclass(frozen=True)
class SelectedEvidence:
    text: str
    strategy: str
    source_chars: int
    selected_chars: int
    sections: tuple[str, ...] = ()


def _terms(value: str) -> set[str]:
    words = set()
    for match in _TOKEN.finditer(value.lower()):
        word = match.group().strip('._+-')
        if re.fullmatch(r'[\u3400-\u9fff]+', word):
            words.update(word[i:i + 2] for i in range(len(word) - 1))
        elif len(word) >= 3 and word not in _STOP_WORDS:
            words.add(word)
    return words


def _blocks(text: str) -> list[tuple[int, int]]:
    """Respect line boundaries where possible; bound even a single huge line."""
    spans = []
    start = 0
    while start < len(text):
        end = min(start + _BLOCK_CHARS, len(text))
        if end < len(text):
            boundary = text.rfind('\n', start + _BLOCK_CHARS // 2, end)
            if boundary < 0:
                boundary = text.rfind(' ', start + _BLOCK_CHARS // 2, end)
            if boundary >= 0:
                end = boundary + 1
        spans.append((start, end))
        start = end
    return spans


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _render(text: str, spans: list[tuple[int, int]], strategy: str) -> str:
    note = (
        '[Source excerpts; omissions exist. Title/anchor matching only locates material; '
        'it does not verify the title. Assess relevance and sufficiency for this item. '
        'Missing evidence here does not establish absence from the full source.]'
    )
    if strategy == 'sampled_excerpts':
        note += '\n[No lexical match found; these are distributed samples, not a relevance decision.]'
    return note + ''.join(
        f'\n\n[Source characters {start}:{end}]\n{text[start:end]}'
        for start, end in spans
    )


def _fill_distributed(
    text: str, blocks: list[tuple[int, int]], selected: list[tuple[int, int]],
    *, strategy: str, max_chars: int,
) -> list[tuple[int, int]]:
    """Add evenly spaced remaining blocks without evicting priority evidence."""
    remaining = [
        span for span in blocks
        if not any(lo <= span[0] and span[1] <= hi for lo, hi in selected)
    ]
    while remaining:
        available = max_chars - len(_render(text, selected, strategy))
        # Allow for an excerpt marker per block; merging often frees more room.
        count = min(len(remaining), max(1, available // (_BLOCK_CHARS + 80)))
        # With room for only one estimated block, make a final middle-out pass
        # for small/overlapping blocks rather than rebuilding the list per miss.
        indices = (
            sorted(range(len(remaining)), key=lambda i: (abs(2 * i - len(remaining) + 1), -i))
            if count == 1 else
            [(2 * i + 1) * len(remaining) // (2 * count) for i in range(count)]
        )
        for i in indices:
            trial = _merge(selected + [remaining[i]])
            if len(_render(text, trial, strategy)) <= max_chars:
                selected = trial
        # Retry with evenly spaced blocks from the remaining gaps until none fit.
        # Each block is tried at most once, including when the budget is full.
        tried = set(indices)
        remaining = [span for i, span in enumerate(remaining) if i not in tried]
    return selected


def select_evidence(
    text: str, *, title: str, url: str = '',
    max_chars: int = DEFAULT_EVIDENCE_MAX_CHARS,
    head_chars: int = 0,
    fill_remaining: bool = False,
) -> SelectedEvidence:
    if max_chars < 1_000:
        raise ValueError('evidence budget must be at least 1000 characters')
    if head_chars < 0:
        raise ValueError('head characters must be nonnegative')
    if len(text) <= max_chars:
        return SelectedEvidence(text, 'full_text', len(text), len(text))

    blocks = _blocks(text)
    query = _terms(title[:2_000])
    anchor = unquote(urlsplit(url).fragment)[:1_000]
    query |= _terms(anchor.replace('-', ' '))
    terms = [_terms(text[start:end]) & query for start, end in blocks]
    frequencies = Counter(term for block in terms for term in block)
    weights = {term: math.log1p(len(blocks) / count) for term, count in frequencies.items()}
    scores = [sum(weights[t] for t in sorted(block)) for block in terms]
    strategy = 'relevant_excerpts' if any(scores) else 'sampled_excerpts'
    minimum_score = max(scores) * 0.45
    ranked = sorted(
        (i for i, score in enumerate(scores) if score >= minimum_score and score > 0),
        key=lambda i: (-scores[i], i),
    )
    if not any(scores):
        # Head/middle/tail coverage when lexical matching cannot help, including
        # translated titles. The model must still decide relevance from evidence.
        ranked = list(dict.fromkeys(round(i * (len(blocks) - 1) / 7) for i in (0, 7, 3, 5, 1, 6, 2, 4)))

    selected: list[tuple[int, int]] = []
    # Reserve the publisher metadata block in its entirety (bounded by extractor),
    # so description attribution cannot be lost by selecting an unlabeled fragment.
    body_marker = '\n\nExtracted body:\n'
    metadata_end = text.find(body_marker) + len(body_marker)
    metadata = (text.startswith('Page metadata (publisher-provided context, not article body):')
                and len(body_marker) <= metadata_end <= 5_000)
    if metadata:
        initial = (0, metadata_end)
        if len(_render(text, [initial], strategy)) <= max_chars - 1_000:
            selected.append(initial)
    if head_chars:
        # Summary callers reserve the body introduction independently of title
        # matches. Metadata alone is not the article's introduction.
        body_start = metadata_end if metadata else 0
        selected = _merge(selected + [(body_start, min(len(text), body_start + head_chars))])
        if len(_render(text, selected, strategy)) > max_chars - 1_000:
            raise ValueError('evidence budget must leave room for matches after the head')
    for i in ranked:
        # Include a preceding block and a following block. This retains nearby
        # section/version/date headings and qualifying sentences around a match.
        lo, hi = max(0, i - 1), min(len(blocks) - 1, i + 1)
        span = (blocks[lo][0], blocks[hi][1])
        trial = _merge(selected + [span])
        if len(_render(text, trial, strategy)) <= max_chars:
            selected = trial
        else:
            # A useful single block can still fit after larger context windows.
            trial = _merge(selected + [blocks[i]])
            if len(_render(text, trial, strategy)) <= max_chars:
                selected = trial
        if max_chars - len(_render(text, selected, strategy)) < _BLOCK_CHARS:
            break
    if not selected:
        # Only reachable with a caller budget smaller than a block plus markers.
        start, end = blocks[ranked[0]]
        selected = [(start, min(end, start + max_chars - 500))]
    if fill_remaining:
        selected = _fill_distributed(
            text, blocks, selected, strategy=strategy, max_chars=max_chars,
        )
    result = _render(text, selected, strategy)
    return SelectedEvidence(
        result, strategy, len(text), len(result),
        tuple(f'chars:{start}:{end}' for start, end in selected),
    )
