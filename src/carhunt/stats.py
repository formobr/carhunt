"""Journal statistics: run health, funnel counts, detection latency, AI cost."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from .store import Store


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p in 0..100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def find_gaps(starts: list[datetime], interval_min: float) -> list[tuple[datetime, datetime]]:
    """Consecutive run starts further apart than 2x the poll interval."""
    limit = timedelta(minutes=2 * interval_min)
    ordered = sorted(starts)
    return [(a, b) for a, b in pairwise(ordered) if b - a > limit]


def compute(store: Store, now: datetime, days: float, interval_min: float) -> dict:
    since = now - timedelta(days=days)
    runs = [
        r for r in (dict(x) for x in store.db.execute("SELECT * FROM runs ORDER BY id"))
        if (parse_ts(r["started_at"]) or since) >= since
    ]
    listings = [
        dict(x)
        for x in store.db.execute("SELECT * FROM listings ORDER BY id")
        if (parse_ts(x["detected_at"]) or since) >= since
    ]
    ids = {l["id"] for l in listings}

    latest: dict[int, str | None] = {}
    for row in store.db.execute(
        "SELECT listing_id, verdict FROM analyses WHERE verdict IS NOT NULL ORDER BY id"
    ):
        latest[row["listing_id"]] = row["verdict"]
    verdicts = {"worth_seeing": 0, "doubtful": 0, "skip": 0}
    for lid, v in latest.items():
        if lid in ids and v in verdicts:
            verdicts[v] += 1

    notified = {
        r["listing_id"] for r in store.db.execute("SELECT listing_id FROM notifications")
    } & ids

    latencies = []
    for l in listings:
        pub, det = parse_ts(l["published_at"]), parse_ts(l["detected_at"])
        if pub and det:
            latencies.append((det - pub).total_seconds())

    ai: dict[str, dict] = {}
    for r in store.db.execute(
        "SELECT listing_id, model, duration_s, created_at FROM analyses WHERE model IS NOT NULL"
    ):
        created = parse_ts(r["created_at"])
        if created is None or created < since:
            continue
        a = ai.setdefault(r["model"], {"calls": 0, "durations": []})
        a["calls"] += 1
        if r["duration_s"] is not None:
            a["durations"].append(r["duration_s"])
    ai_out = {
        m: {
            "calls": a["calls"],
            "mean_s": sum(a["durations"]) / len(a["durations"]) if a["durations"] else None,
        }
        for m, a in sorted(ai.items())
    }

    starts = [s for s in (parse_ts(r["started_at"]) for r in runs) if s]
    return {
        "days": days,
        "runs": len(runs),
        "blocked": sum(1 for r in runs if r["note"] == "blocked"),
        "errors": sum(1 for r in runs if (r["errors"] or 0) > 0),
        "gaps": find_gaps(starts, interval_min),
        "found": len(listings),
        "analyzed": sum(1 for l in listings if l["status"] == "analyzed"),
        "pending": sum(1 for l in listings if l["status"] != "analyzed"),
        "notified": len(notified),
        "verdicts": verdicts,
        "latency_n": len(latencies),
        "latency_p50": percentile(latencies, 50),
        "latency_p90": percentile(latencies, 90),
        "ai": ai_out,
    }


def _min(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds / 60:.1f} min"


def format_stats(s: dict) -> str:
    days = s["days"]
    label = f"{days:g} d" if days > 1 else f"{days * 24:g} h"
    v = s["verdicts"]
    lines = [
        f"carhunt stats, last {label}",
        f"runs: {s['runs']} (blocked {s['blocked']}, with errors {s['errors']})",
        f"gaps > 2x interval: {len(s['gaps'])}",
    ]
    for a, b in s["gaps"]:
        lines.append(f"  {a:%Y-%m-%d %H:%M} -> {b:%Y-%m-%d %H:%M} UTC")
    lines += [
        (
            f"listings found: {s['found']}, analyzed: {s['analyzed']}, "
            f"pending: {s['pending']}, notified: {s['notified']}"
        ),
        f"verdicts: worth_seeing {v['worth_seeing']}, doubtful {v['doubtful']}, skip {v['skip']}",
        (
            f"detection latency (n={s['latency_n']}): p50 {_min(s['latency_p50'])}, "
            f"p90 {_min(s['latency_p90'])}"
        ),
    ]
    if s["ai"]:
        for m, a in s["ai"].items():
            mean = "n/a" if a["mean_s"] is None else f"{a['mean_s']:.1f}s"
            lines.append(f"AI {m}: {a['calls']} calls, mean {mean}")
    else:
        lines.append("AI calls: none")
    return "\n".join(lines)
