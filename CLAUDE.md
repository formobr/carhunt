# carhunt

Personal tool: polls Facebook Marketplace (Pittsburgh, used cars) every 15 min without login, rates every new
listing with Claude via the owner's subscription (`claude -p` CLI, NO Anthropic API key): haiku triage on every
listing, opus re-check of candidates, sends good ones to Telegram.

- Requirements: `docs/PRD.md`. Plan, critique, slices: `docs/PLAN.md`.
- Code in `src/carhunt/`, tests in `tests/` (`.venv/bin/pytest -q`). Fixtures in `tests/fixtures/` are real pages
  captured 2026-11-10; parsers are tested against them, never against the live site.
- Secrets only in `.env` (git-ignored); never print or log them. Settings in `config.yaml`.
- Tests never call the network or `claude`: HTTP, the claude runner and Telegram are injected and faked.
- Work lands through dev-protocol `land run` (briefs in `work/slices/`).
