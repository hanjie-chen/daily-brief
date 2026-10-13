# AGENTS

## Read First

- Start with the root `README.md`.
- Before planning content selection, summary, or retrieval/recovery changes, read `docs/product.md`.
- For retrieval/recovery changes, read `docs/architecture.md` before editing.
- Before non-trivial work under `src/daily_brief`, read `src/daily_brief/README.md` and the relevant code and tests.
- Before running live-API batch experiments, read `docs/api-quotas.md`.

## Design Principles

- This is a personal project; backward compatibility is not required. Remove superseded code instead of keeping compatibility layers, and update affected callers and configuration together.
- Prefer the simplest solution that fully addresses the problem and related cases within the current requirements. Do not optimize for the smallest diff or add abstractions for hypothetical needs.
- Treat existing implementation choices as revisable, not as requirements. Preserve explicit user constraints.

## Documentation

- `README.md`, `docs/product.md`, and `docs/api-quotas.md` are owner-maintained. Do not edit them unless asked; when a change affects or conflicts with them, say so and suggest wording.
- Keep `docs/architecture.md` (Chinese) and `src/daily_brief/**/README.md` (English) in sync with the code in the same change. Describe how the system works; do not add product intent.

## Verification

- For behavior changes, verify the intended outcome and key neighboring cases that should remain unaffected.
- For code or behavior changes, run the smallest relevant tests during development and `pytest -q` before completion.
- Keep tests deterministic; do not call live external services (Hacker News, retrieval, search, or model providers) from tests.
- For documentation-only changes, review accuracy and run `git diff --check`; the full test suite is not required.

## Git Workflow

- Make requested changes directly on `main` by default; create branches or pull requests only when the user explicitly asks.
