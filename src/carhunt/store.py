"""SQLite journal."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    price INTEGER,
    year INTEGER,
    make TEXT,
    model TEXT,
    trim TEXT,
    mileage INTEGER,
    transmission TEXT,
    title_status TEXT,
    location TEXT,
    description TEXT,
    photos TEXT,
    seller_type TEXT,
    raw TEXT,
    published_at TEXT,
    detected_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    repost_of INTEGER REFERENCES listings(id),
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'pending_analysis', 'analyzed')),
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    price INTEGER NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    stage TEXT NOT NULL,
    model TEXT,
    verdict TEXT,
    reliability REAL,
    value REAL,
    json TEXT,
    duration_s REAL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    sent_at TEXT NOT NULL,
    telegram_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source TEXT NOT NULL,
    cards INTEGER,
    new INTEGER,
    errors INTEGER,
    note TEXT
);
"""


@dataclass(frozen=True)
class UpsertResult:
    listing_id: int
    is_new: bool
    old_price: int | None
    repost_of: int | None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def upsert_listing(self, l: Listing, now: datetime) -> UpsertResult:
        now_s = _iso(now)
        row = self.db.execute(
            "SELECT id, price, repost_of FROM listings WHERE source=? AND source_id=?",
            (l.source, l.source_id),
        ).fetchone()
        with self.db:
            if row is not None:
                old_price = None
                if row["price"] != l.price:
                    old_price = row["price"]
                    self.db.execute(
                        "UPDATE listings SET price=? WHERE id=?", (l.price, row["id"])
                    )
                    self.db.execute(
                        "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,?,?)",
                        (row["id"], l.price, now_s),
                    )
                return UpsertResult(row["id"], False, old_price, row["repost_of"])

            fp = l.fingerprint
            match = self.db.execute(
                "SELECT id FROM listings WHERE fingerprint=? ORDER BY id LIMIT 1",
                (fp,),
            ).fetchone()
            repost_of = match["id"] if match else None
            cur = self.db.execute(
                """INSERT INTO listings (source, source_id, url, title, price, year, make,
                model, trim, mileage, transmission, title_status, location, description,
                photos, seller_type, raw, published_at, detected_at, fingerprint,
                repost_of, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')""",
                (
                    l.source, l.source_id, l.url, l.title, l.price, l.year, l.make,
                    l.model, l.trim, l.mileage, l.transmission, l.title_status,
                    l.location, l.description, json.dumps(l.photos), l.seller_type,
                    json.dumps(l.raw), _iso(l.published_at), now_s, fp, repost_of,
                ),
            )
            lid = cur.lastrowid
            self.db.execute(
                "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,?,?)",
                (lid, l.price, now_s),
            )
        return UpsertResult(lid, True, None, repost_of)

    def record_analysis(
        self,
        listing_id: int,
        stage: str,
        model: str | None,
        verdict: str | None,
        reliability: float | None,
        value: float | None,
        payload: dict | None,
        duration_s: float | None,
        reason: str | None,
    ) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO analyses (listing_id, stage, model, verdict, reliability,
                value, json, duration_s, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    listing_id, stage, model, verdict, reliability, value,
                    json.dumps(payload) if payload is not None else None,
                    duration_s, reason, _utcnow_iso(),
                ),
            )
        return cur.lastrowid

    def latest_analysis(self, listing_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM analyses WHERE listing_id=? ORDER BY id DESC LIMIT 1",
            (listing_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["json"] = json.loads(d["json"]) if d["json"] else None
        return d

    def pending_analysis(self) -> list[int]:
        rows = self.db.execute(
            "SELECT id FROM listings WHERE status IN ('new','pending_analysis') ORDER BY id"
        ).fetchall()
        return [r["id"] for r in rows]

    def set_status(self, listing_id: int, status: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE listings SET status=? WHERE id=?", (status, listing_id)
            )

    def record_notification(self, listing_id: int, message_id: int | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO notifications (listing_id, sent_at, telegram_message_id) VALUES (?,?,?)",
                (listing_id, _utcnow_iso(), message_id),
            )

    def was_notified(self, listing_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM notifications WHERE listing_id=? LIMIT 1", (listing_id,)
            ).fetchone()
            is not None
        )

    def start_run(self, source: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (started_at, source) VALUES (?,?)",
                (_utcnow_iso(), source),
            )
        return cur.lastrowid

    def finish_run(
        self, run_id: int, cards: int, new: int, errors: int, note: str | None = None
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at=?, cards=?, new=?, errors=?, note=? WHERE id=?",
                (_utcnow_iso(), cards, new, errors, note, run_id),
            )

    def recent_runs(self, n: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    def similar_prices(
        self, make: str, model: str, year: int, window: int = 2
    ) -> list[int]:
        rows = self.db.execute(
            """SELECT price FROM listings
               WHERE lower(make)=lower(?) AND lower(model)=lower(?)
               AND year BETWEEN ? AND ? AND price IS NOT NULL ORDER BY id""",
            (make, model, year - window, year + window),
        ).fetchall()
        return [r["price"] for r in rows]
