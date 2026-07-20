# Daily Brief Quality Pipeline Design

## Goal

Improve the daily brief using failures observed in the July 17–20 outputs:

- recognize important AI stories whose titles contain new product or model names;
- keep heat important enough that repeated keyword mentions cannot dominate ranking;
- avoid recommending the same Hacker News item on consecutive days;
- ground summaries in the HN post or fetched article text;
- degrade locally when an item, article, classifier, or summarizer fails.

Existing files in `briefs/` and dated candidate snapshots in `data/` remain unchanged.

## Pipeline

The generator will use the following order:

1. Fetch the last-day Algolia stories and current official HN hot stories.
2. Build and deduplicate scored candidates from both sources.
3. Exclude HN item IDs recommended during the previous seven dates. A rerun for the same date ignores that date's history so generation remains idempotent.
4. Route candidates with non-weak keyword matches directly to the AI pool.
5. Send at most 30 highest-heat unmatched or weak-only candidates to one batch Codex topic-classification call. Valid returned IDs join the AI pool; classifier failure leaves deterministic keyword routing intact.
6. Select at most five AI stories and two non-AI hot stories.
7. Fetch article text only for selected external-link stories that have no HN `story_text`. Each fetch is isolated and optional.
8. Summarize selected stories, render the brief and diagnostic candidate snapshot, then update the dedicated recommendation-history file after outputs have been written successfully.

## Topic Classification

Known signals remain deterministic. The keyword list will add currently missing, explicit AI names and phrases such as GPT, Codex, Qwen, Kimi, Grok, DeepSeek, Fable, Moonshot, and open weights. Ambiguous brand-like names will be matched case-sensitively where appropriate.

The Codex fallback classifier receives only candidate IDs, titles, and source hosts. It must return a JSON array of AI-related HN item IDs. Returned values are accepted only when they belong to the supplied batch. Titles and hosts are marked as untrusted input. A timeout, malformed response, or command failure is logged and does not stop generation.

The classifier supplements keyword routing rather than replacing it. This preserves deterministic coverage for known topics while allowing high-heat new products to enter the AI pool.

## Ranking and Presentation

Scoring keeps the existing logarithmic heat calculation. Repeated occurrences of the same keyword count once. The global keyword bonus cap becomes 5 and the topic bonus cap becomes 1; weak matches no longer add a separate occurrence bonus. Relevance can therefore identify the topic without overwhelming large differences in HN attention.

`Why` displays distinct keywords in first-seen order. AI candidates admitted only by the topic classifier use `topic classifier: AI` as their reason.

The minimum-points threshold remains 10. Raising it now would remove potentially useful early developer-tool launches without reliably removing high-point novelty stories. Future tuning should use explicit reading feedback.

## Recommendation History

A dedicated `data/recommendation-history.json` state file lives alongside generated data but is not a dated diagnostic snapshot. It maps date labels to selected HN item IDs. Before selection, IDs from the previous seven calendar dates are ineligible in both sections and receive a `recently_selected` rejection reason.

The current date is excluded from filtering and replaces its own history entry after successful generation. Old entries are pruned and state updates use atomic file replacement. A missing or malformed state file is logged and treated as empty so it cannot prevent brief generation.

## Article Fetching and Summaries

Article fetching accepts only public HTTP or HTTPS destinations, revalidates redirects, rejects local/private/reserved addresses, limits response size, permits text or HTML content, and applies a short timeout. HTML extraction removes non-content elements and collapses whitespace. Fetch failures are logged per item and leave `fetched_text` empty.

The summary prompt uses `story_text`, otherwise fetched article text, otherwise the title alone. It asks for facts explicitly present in the supplied material, forbids inferred causes or later outcomes, and does not include HN points/comments or ask why the story is worth reading. Those concerns already have separate `Stats` and `Why` lines.

Summary failure produces an honest unavailable-summary message rather than repeating the title and statistics.

## HN Failure Isolation

Topstories and beststories list requests fail independently. If one succeeds, its IDs are still processed. A failed item request is logged and skipped while later items continue. If both list requests fail, the source reports terminal failure through the existing source-level boundary.

## Testing

All new behavior is developed test-first with no live HN, article, or Codex calls:

- new model names route to AI, while weak-only ordinary stories remain non-AI;
- the batch classifier admits only supplied IDs and fails closed;
- unique-keyword scoring and reduced caps allow heat to influence ordering;
- history excludes prior-date IDs, permits same-date reruns, and prunes old records;
- article extraction, size/type restrictions, unsafe destinations, and per-item fallback are deterministic;
- prompts use available content and prohibit unsupported inference and HN-stat repetition;
- one failed HN item or list request preserves other successful results;
- `Why` removes duplicate keyword display;
- end-to-end generation still writes both outputs under partial failures.

The smallest relevant tests run during each red-green cycle, followed by `pytest -q` before completion.
