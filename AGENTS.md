# AGENTS

## Read First

- Start with the project root `README.md`.
- Read the relevant code and tests before changing behavior.

## Working Rules

- Prefer the simplest change that fully addresses the current task.
- For small, well-scoped tasks, work directly. Use detailed plans or subagents only when complexity or coordination risk makes them useful.
- Diagnose bugs with evidence before changing code or configuration.
- Keep project documentation focused and current: `README.md` is human-facing, while `AGENTS.md` contains only actionable instructions for agents.

## Verification

- For code or behavior changes, run the smallest relevant tests during development and `pytest -q` before completion.
- Keep tests deterministic; do not call the live Hacker News APIs or `codex` from tests.
- For documentation-only changes, review accuracy and run `git diff --check`; the full test suite is not required.
