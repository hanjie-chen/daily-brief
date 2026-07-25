# AGENTS

## Read First

- Start with the project root `README.md`.
- Before planning or making non-trivial changes under `src/daily_brief`,
  read `src/daily_brief/README.md` and the relevant code and tests.

## Documentation

- Keep the root `README.md` focused on project goals, usage, and user-visible
  behavior. Use `src/daily_brief/README.md` as the source of truth for package
  architecture, module responsibilities, and implementation invariants.
- Write the root `README.md` in Chinese, using English technical terms when
  clearer. Write `AGENTS.md` and `src/daily_brief/README.md` in English.
- Update `src/daily_brief/README.md` in the same change when documented files,
  module responsibilities, generation flow, or invariants change. Update the
  root `README.md` when project-level or user-visible behavior changes.

## Verification

- For code or behavior changes, run the smallest relevant tests during
  development and `pytest -q` before completion.
- Keep tests deterministic; do not call the live Hacker News APIs or `codex`
  from tests.
- For documentation-only changes, review accuracy and run `git diff --check`;
  the full test suite is not required.