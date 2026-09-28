# AGENTS

## Read First

- Start with the project root `README.md`.
- Before planning changes that affect content selection, section composition, or the reading experience, read `docs/product.md`.
- Before changing retrieval or recovery flow, read `docs/architecture.md`.
- Before planning or making non-trivial changes under `src/daily_brief`, read `src/daily_brief/README.md` and the relevant code and tests.
- Before running batch experiments that call live APIs, read `docs/api-quotas.md`.

## Documentation

- `README.md`, `docs/product.md`, and `docs/api-quotas.md` are owner-maintained. Do not edit them unless asked; when a change affects or conflicts with them, say so and suggest wording.
- Keep `docs/architecture.md` (Chinese) and `src/daily_brief/**/README.md` (English) in sync with the code in the same change. Describe how the system works; do not add product intent.

## Verification

- For code or behavior changes, run the smallest relevant tests during development and `pytest -q` before completion.
- Keep tests deterministic; do not call the live Hacker News APIs, `codex`, or the Gemini API from tests.
- For documentation-only changes, review accuracy and run `git diff --check`; the full test suite is not required.

## Git Workflow

- Make requested changes directly on `main` by default; create branches or pull requests only when the user explicitly asks.
