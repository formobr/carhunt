---
covers: [pyproject.toml, "*.env.example", pyyaml, httpx, pydantic]
sources: [https://pypi.org/project/PyYAML/, https://pypi.org/project/httpx/, https://pypi.org/project/pydantic/, https://core.telegram.org/bots/api]
verified: "2026-11-10: uv installed PyYAML 6.0.3, httpx 0.28.1, pydantic 2.14.3 into .venv (Python 3.12.3); all import. Telegram env names TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (Bot API)."
critics: [{family: codex, verdict: PASS-WITH-CONDITIONS, receipt: codex-review-2026-11-10-deps}, {family: claude, verdict: PASS-WITH-CONDITIONS}]
---

# Python runtime dependencies (carhunt)

`[project].dependencies` sets lower bounds equal to the versions verified live (codex condition: untested minima are not claimed), no upper caps (one-off personal tool, one venv):

- `pyyaml>=6.0.3` — config.yaml loader (`yaml.safe_load` only).
- `httpx>=0.28.1` — Facebook/Telegram HTTP; 0.28 removed the `proxies=` arg and `app=` shortcut, neither used. Redirects are OFF by default since 0.20: every Facebook client sets `follow_redirects=True` explicitly.
- `pydantic>=2.14` — the AI verdict schema (`model_json_schema()`, `model_validate`); v2 API only (`Optional[T]` needs an explicit `= None` default).
- dev: `pytest`, `ruff` (unpinned).

No Anthropic SDK: AI runs through the owner's subscription via the `claude` CLI subprocess (no API key).

`.env.example` carries only `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, required for notifications; there is no fallback
bot. An empty string counts as unset,
and the pair is taken whole: never a token from one source with a chat id from the other.

Conditions both critics raised, applied: floors raised to the tested versions; redirects made explicit; empty = unset;
fallback pair taken whole.
