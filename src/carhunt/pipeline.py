"""One poll run end to end: fetch, dedup, re-rate on price drop, analyze, notify."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .analyze import AnalyzeError, Verdict
from .config import Settings
from .models import Listing
from .notify import NotifyError
from .sources.fb import fetch_new
from .store import Store

log = logging.getLogger(__name__)

HEALTH_ALERT = "carhunt: Facebook не отдаёт объявления {n} запусков подряд"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class RunSummary:
    run_id: int
    cards: int = 0
    new: int = 0
    analyzed: int = 0
    notified: int = 0
    errors: int = 0
    blocked: bool = False
    alerted: bool = False


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _load(store: Store, lid: int) -> tuple[Listing, datetime, int | None]:
    r = store.db.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    listing = Listing(
        source=r["source"],
        source_id=r["source_id"],
        url=r["url"],
        title=r["title"] or "",
        price=r["price"] or 0,
        year=r["year"],
        make=r["make"],
        model=r["model"],
        trim=r["trim"],
        mileage=r["mileage"],
        transmission=r["transmission"],
        title_status=r["title_status"],
        location=r["location"],
        description=r["description"],
        photos=json.loads(r["photos"]) if r["photos"] else [],
        seller_type=r["seller_type"],
        published_at=_dt(r["published_at"]),
        raw=json.loads(r["raw"]) if r["raw"] else {},
    )
    return listing, _dt(r["detected_at"]) or utcnow(), r["repost_of"]


def _price_of(store: Store, lid: int) -> int | None:
    r = store.db.execute("SELECT price FROM listings WHERE id=?", (lid,)).fetchone()
    return r["price"] if r else None


def _is_drop(old: int | None, new: int, settings: Settings) -> bool:
    if not old or new >= old:
        return False
    drop = old - new
    return (
        drop >= settings.notify.price_drop_min_usd
        or drop / old * 100 >= settings.notify.price_drop_min_pct
    )


def _qualifies(v: Verdict, settings: Settings) -> bool:
    return (
        v.verdict == "worth_seeing"
        and v.reliability >= settings.notify.min_reliability
        and v.value >= settings.notify.min_value
        and v.reliability + v.value >= settings.notify.min_sum
    )


def poll(
    settings: Settings,
    store: Store,
    fetcher: Any,
    analyzer: Callable[[Listing, str], Verdict],
    notifier: Any,
    now: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> RunSummary:
    """``analyzer(listing, reason) -> Verdict`` must mark the listing analyzed
    on success and pending_analysis on AnalyzeError (as analyze.analyze does)."""
    run_id = store.start_run("fb")
    summary = RunSummary(run_id)
    known = {
        r["source_id"]
        for r in store.db.execute("SELECT source_id FROM listings WHERE source='fb'")
    }
    batch = fetch_new(fetcher, settings, known, sleep)
    summary.cards = len(batch.cards)
    summary.errors = len(batch.errors)

    if batch.blocked:
        summary.blocked = True
        summary.errors += 1
        store.finish_run(run_id, summary.cards, 0, summary.errors, "blocked")
        _health(settings, store, notifier, summary)
        return summary

    skip: set[int] = set()  # failed this run; retried next run

    def notify(lid: int, listing: Listing, verdict: Verdict, detected: datetime,
               reason: str, old_price: int | None = None) -> None:
        if not _qualifies(verdict, settings):
            return
        if reason != "price_drop" and store.was_notified(lid):
            return
        try:
            mid = notifier.send_card(listing, verdict, detected, reason, old_price)
        except NotifyError as exc:
            log.warning("notify failed for %s: %s", lid, exc)
            summary.errors += 1
            return
        store.record_notification(lid, mid)
        summary.notified += 1

    # 1. price drops on known listings
    for card in batch.cards:
        if card.source_id not in known:
            continue
        row = store.db.execute(
            "SELECT id, price, status FROM listings WHERE source='fb' AND source_id=?",
            (card.source_id,),
        ).fetchone()
        if row is None or row["status"] != "analyzed":
            continue
        if not _is_drop(row["price"], card.price, settings):
            continue
        lid = row["id"]
        listing, detected, _ = _load(store, lid)
        old = listing.price
        listing.price = card.price
        try:
            verdict = analyzer(listing, "price_drop")
        except AnalyzeError as exc:
            log.warning("re-analysis failed for %s: %s", lid, exc)
            store.set_status(lid, "analyzed")  # price not saved: drop is seen again
            summary.errors += 1
            continue
        store.upsert_listing(listing, now())
        summary.analyzed += 1
        notify(lid, listing, verdict, detected, "price_drop", old)

    # 2. new detailed listings
    for item in batch.listings:
        res = store.upsert_listing(item, now())
        if not res.is_new:
            continue
        summary.new += 1
        if res.repost_of is None:
            continue
        old = _price_of(store, res.repost_of)
        if not _is_drop(old, item.price, settings):
            store.set_status(res.listing_id, "analyzed")
            continue
        try:
            verdict = analyzer(item, "price_drop")
        except AnalyzeError as exc:
            log.warning("repost analysis failed for %s: %s", res.listing_id, exc)
            summary.errors += 1
            skip.add(res.listing_id)
            continue
        summary.analyzed += 1
        _, detected, _ = _load(store, res.listing_id)
        notify(res.listing_id, item, verdict, detected, "price_drop", old)

    # 3. pending analyses, oldest first, capped
    todo = [i for i in store.pending_analysis() if i not in skip]
    for lid in todo[: settings.poll.max_details_per_run]:
        listing, detected, _ = _load(store, lid)
        try:
            verdict = analyzer(listing, "new")
        except AnalyzeError as exc:
            log.warning("analysis failed for %s: %s", lid, exc)
            summary.errors += 1
            continue
        summary.analyzed += 1
        notify(lid, listing, verdict, detected, "new")

    store.finish_run(run_id, summary.cards, summary.new, summary.errors, None)
    _health(settings, store, notifier, summary)
    return summary


def _bad(run: dict) -> bool:
    return run.get("note") == "blocked" or run.get("cards") == 0


def _health(settings: Settings, store: Store, notifier: Any, summary: RunSummary) -> None:
    n = settings.health.fail_runs_before_alert
    runs = store.recent_runs(n + 1)
    if len(runs) < n or not all(_bad(r) for r in runs[:n]):
        return
    if len(runs) > n and _bad(runs[n]):
        return
    try:
        notifier.send_text(HEALTH_ALERT.format(n=n))
        summary.alerted = True
    except NotifyError as exc:
        log.warning("health alert failed: %s", exc)
