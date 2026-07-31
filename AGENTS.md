# AGENTS

## Read First

- Start with the project root `README.md`.
- Before planning changes that affect content selection, section composition, or the reading experience, read `docs/product.md`.
- Before planning or making non-trivial changes under `src/daily_brief`, read `src/daily_brief/README.md` and the relevant code and tests.

## Documentation

- Keep the root `README.md` focused on usage and user-visible behavior. Use `docs/product.md` for product intent and direction, and `src/daily_brief/README.md` for package architecture and implementation invariants.
- Write the root `README.md` and `docs/product.md` in Chinese, using English technical terms when clearer. Write `AGENTS.md` and `src/daily_brief/README.md` in English.
- Update the corresponding document in the same change when product intent, user-visible behavior, or package architecture changes.

## Verification

- For code or behavior changes, run the smallest relevant tests during development and `pytest -q` before completion.
- Keep tests deterministic; do not call the live Hacker News APIs, `codex`, or the Gemini API from tests.
- For documentation-only changes, review accuracy and run `git diff --check`; the full test suite is not required.