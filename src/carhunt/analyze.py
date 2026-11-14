"""AI judgment via the `claude` CLI (owner's subscription; no SDK, no API key)."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from .config import SearchSettings, Settings
from .models import Listing
from .store import Store

log = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class AnalyzeError(Exception):
    """The AI stage failed; the listing stays pending_analysis."""


class MarketPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: int
    high: int
    basis: str


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["worth_seeing", "doubtful", "skip"]
    reliability: int
    value: int
    summary: str
    reasons_for: list[str]
    risks: list[str]
    expected_costs: str
    market_price: MarketPrice | None = None
    negotiation_target: int | None = None
    questions_for_seller: list[str]
    needs_clarification_urgent: bool
    facts_unknown: list[str]


MAX_REASONS = 3
MAX_RISKS = 4
MAX_QUESTIONS = 4
MAX_UNKNOWN = 3


def _bounded(v: Verdict) -> None:
    for name in ("reliability", "value"):
        if not 0 <= getattr(v, name) <= 100:
            raise ValueError(f"{name} out of range 0-100")


def _trim(v: Verdict) -> None:
    v.reasons_for = v.reasons_for[:MAX_REASONS]
    v.risks = v.risks[:MAX_RISKS]
    v.questions_for_seller = v.questions_for_seller[:MAX_QUESTIONS]
    v.facts_unknown = v.facts_unknown[:MAX_UNKNOWN]


def build_prompt(
    listing: Listing,
    comparables: list[int],
    stage: str,
    photos: list[str] | None = None,
    region: str = SearchSettings.region,
) -> str:
    fields = [
        ("Title", listing.title),
        ("Price (USD)", listing.price),
        ("Year", listing.year),
        ("Make", listing.make),
        ("Model", listing.model),
        ("Trim", listing.trim),
        ("Mileage", listing.mileage),
        ("Transmission", listing.transmission),
        ("Title status", listing.title_status),
        ("Location", listing.location),
        ("Seller type", listing.seller_type),
    ]
    lines = [f"{k}: {v if v not in (None, '') else 'unknown'}" for k, v in fields]
    parts = [
        (PROMPTS_DIR / "analyze.md").read_text(encoding="utf-8").strip().replace("{region}", region),
        "## Buyer criteria\n"
        + (PROMPTS_DIR / "criteria.md").read_text(encoding="utf-8").strip().replace("{region}", region),
        "## Listing\n" + "\n".join(lines),
        "## Description\n" + (listing.description or "(none)"),
        "## Similar listings seen so far (prices, USD)\n"
        + (", ".join(str(p) for p in comparables) if comparables else "(none)"),
        "## Photo files (in the current directory)\n"
        + ("\n".join(photos) if photos else "(no photos)"),
    ]
    if stage == "judge":
        parts.append(
            "## Extra for this stage\nUse web search for recent comparable "
            "private-party prices and for the known issues of this exact "
            "engine/transmission."
        )
    return "\n\n".join(parts)


@dataclass
class RunResult:
    output: dict
    duration_s: float
    model_usage: dict = field(default_factory=dict)


class ClaudeRunner:
    def __init__(self, binary: str = "claude"):
        self.binary = binary

    def run(
        self,
        prompt: str,
        model: str,
        schema: dict,
        cwd: str | Path,
        allow_web: bool,
        timeout_s: float,
    ) -> RunResult:
        argv = [
            self.binary, "-p", "--model", model,
            "--setting-sources", "project",
            "--strict-mcp-config", "--no-session-persistence",
            "--allowedTools", "Read,WebSearch" if allow_web else "Read",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            prompt,
        ]
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=str(cwd), text=True,
                start_new_session=True,
            )
        except OSError as e:
            raise AnalyzeError(f"cannot start claude: {e.__class__.__name__}") from e
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise AnalyzeError(f"claude timed out after {timeout_s}s") from None
        duration = time.monotonic() - start
        if proc.returncode != 0:
            raise AnalyzeError(f"claude exited with code {proc.returncode}: {(stderr or '').strip()[:200]}")
        try:
            data = json.loads(stdout)
        except ValueError:
            raise AnalyzeError("claude reply is not JSON") from None
        if not isinstance(data, dict):
            raise AnalyzeError("claude reply is not a JSON object")
        if data.get("is_error"):
            raise AnalyzeError("claude reported is_error")
        out = data.get("structured_output")
        if not isinstance(out, dict):
            raise AnalyzeError("claude reply has no structured_output")
        usage = data.get("modelUsage")
        return RunResult(out, duration, usage if isinstance(usage, dict) else {})


def download_photos(urls: list[str], dir: str | Path, client: Any, max_photos: int) -> list[str]:
    names: list[str] = []
    for url in urls[:max_photos]:
        try:
            resp = client.get(url)
            resp.raise_for_status()
            content = resp.content
        except httpx.HTTPError as e:
            log.warning("photo download failed: %s", e.__class__.__name__)
            continue
        if not content:
            continue
        name = f"photo_{len(names) + 1:02d}.jpg"
        (Path(dir) / name).write_bytes(content)
        names.append(name)
    return names


def _listing_id(listing: Listing, store: Store) -> int:
    row = store.db.execute(
        "SELECT id FROM listings WHERE source=? AND source_id=?",
        (listing.source, listing.source_id),
    ).fetchone()
    if row is None:
        raise AnalyzeError("listing is not in the store")
    return row["id"]


def analyze(
    listing: Listing,
    store: Store,
    runner: Any,
    http: Any,
    settings: Settings,
    reason: str,
) -> Verdict:
    lid = _listing_id(listing, store)
    ai = settings.ai
    schema = Verdict.model_json_schema()
    comparables: list[int] = []
    if listing.make and listing.model and listing.year:
        comparables = store.similar_prices(listing.make, listing.model, listing.year)
    final: Verdict | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="carhunt-") as tmp:
            photos = download_photos(listing.photos, tmp, http, ai.max_photos)
            stages = [("triage", ai.triage_model)]
            while stages:
                stage, model = stages.pop(0)
                prompt = build_prompt(listing, comparables, stage, photos, settings.search.region)
                res = runner.run(prompt, model, schema, tmp, stage == "judge", ai.timeout_s)
                try:
                    verdict = Verdict.model_validate(res.output)
                    _bounded(verdict)
                except (ValidationError, ValueError):
                    raise AnalyzeError(f"{stage}: reply does not match the verdict schema") from None
                _trim(verdict)
                store.record_analysis(
                    lid, stage, model, verdict.verdict, verdict.reliability,
                    verdict.value,
                    {**verdict.model_dump(), "model_usage": res.model_usage},
                    res.duration_s, reason,
                )
                final = verdict
                if stage == "triage" and (
                    verdict.verdict != "skip" or verdict.needs_clarification_urgent
                ):
                    stages.append(("judge", ai.judge_model))
    except AnalyzeError:
        store.set_status(lid, "pending_analysis")
        raise
    store.set_status(lid, "analyzed")
    assert final is not None
    return final
