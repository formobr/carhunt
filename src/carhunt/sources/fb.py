"""Facebook Marketplace source: logged-out search and item pages.

Everything is read from the ``<script type="application/json">`` payloads the
page embeds; the visual markup is never parsed.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from carhunt.config import SearchSettings, Settings
from carhunt.models import Listing

ITEM_URL = "https://www.facebook.com/marketplace/item/{}/"
_KM_TO_MILES = 0.621371
_SCRIPT_RE = re.compile(
    r"<script[^>]*type=\"application/json\"[^>]*>(.*?)</script>", re.DOTALL
)
_YEAR_RE = re.compile(r"^\s*(\d{4})(?!\d)")


class ItemUnavailable(Exception):
    """The item page carries no listing payload (removed, or blocked)."""


@dataclass
class Card:
    source_id: str
    url: str
    title: str
    price: int
    mileage_text: str | None = None
    city: str | None = None
    state: str | None = None
    primary_photo: str | None = None
    creation_time: datetime | None = None
    is_sold: bool = False
    is_pending: bool = False


@dataclass
class SearchPage:
    cards: list[Card] = field(default_factory=list)
    has_next_page: bool = False
    blocked: bool = False


def search_url(search: SearchSettings) -> str:
    return (
        f"https://www.facebook.com/marketplace/{search.source_city}/vehicles"
        f"?minPrice={search.min_price}&maxPrice={search.max_price}"
        "&sortBy=creation_time_descend&exact=false"
    )


# ---------------------------------------------------------------- payload walk


def _payloads(html: str) -> Iterator[Any]:
    for m in _SCRIPT_RE.finditer(html):
        try:
            yield json.loads(m.group(1))
        except ValueError:
            continue


def _walk(node: Any) -> Iterator[dict]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _dicts(html: str) -> Iterator[dict]:
    for payload in _payloads(html):
        yield from _walk(payload)


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _price(listing: dict) -> int | None:
    amount = _get(listing, "listing_price", "amount")
    try:
        return int(float(amount))
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------- search page


def _card(d: dict) -> Card | None:
    sid = d.get("id")
    title = d.get("marketplace_listing_title")
    price = _price(d)
    if not sid or title is None or price is None:
        return None
    sid = str(sid)
    subtitles = d.get("custom_sub_titles_with_rendering_flags")
    mileage_text = None
    if isinstance(subtitles, list) and subtitles:
        mileage_text = _get(subtitles[0], "subtitle")
    geo = _get(d, "location", "reverse_geocode") or {}
    return Card(
        source_id=sid,
        url=ITEM_URL.format(sid),
        title=title,
        price=price,
        mileage_text=mileage_text,
        city=geo.get("city") if isinstance(geo, dict) else None,
        state=geo.get("state") if isinstance(geo, dict) else None,
        primary_photo=_get(d, "primary_listing_photo", "image", "uri"),
        creation_time=_ts(d.get("creation_time")),
        is_sold=bool(d.get("is_sold")),
        is_pending=bool(d.get("is_pending")),
    )


def parse_search(html: str) -> SearchPage:
    cards: dict[str, Card] = {}
    has_next = False
    feed_seen = False
    for d in _dicts(html):
        listing = d.get("listing")
        if isinstance(listing, dict) and (
            listing.get("__typename") == "GroupCommerceProductItem"
            or "marketplace_listing_title" in listing
        ):
            card = _card(listing)
            if card and card.source_id not in cards and not (
                card.is_sold or card.is_pending
            ):
                cards[card.source_id] = card
        feed = d.get("marketplace_feed_stories")
        if isinstance(feed, dict) and isinstance(feed.get("edges"), list):
            feed_seen = True
        page_info = d.get("page_info")
        if isinstance(page_info, dict) and page_info.get("has_next_page") is True:
            has_next = True
    # A healthy empty result still carries the feed payload (with "edges");
    # a login wall / redirect / stripped page does not. The login-form query
    # name alone says nothing: it is present on healthy pages too.
    blocked = not cards and not feed_seen
    return SearchPage(cards=list(cards.values()), has_next_page=has_next, blocked=blocked)


# ------------------------------------------------------------------ item page


def parse_item(html: str, source_id: str) -> Listing:
    merged: dict[str, Any] = {}
    found = False
    for d in _dicts(html):
        if str(d.get("id")) != source_id:
            continue
        if d.get("__typename") != "GroupCommerceProductItem" and (
            "marketplace_listing_title" not in d
        ):
            continue
        found = True
        for key, value in d.items():
            if merged.get(key) is None:
                merged[key] = value
    if not found:
        raise ItemUnavailable(source_id)

    title = merged.get("marketplace_listing_title") or ""
    year_m = _YEAR_RE.match(title)

    mileage = None
    odo = merged.get("vehicle_odometer_data")
    if isinstance(odo, dict):
        try:
            value = float(odo["value"])
            if str(odo.get("unit", "")).upper().startswith("KILOMETER"):
                value *= _KM_TO_MILES
            mileage = round(value)
        except (KeyError, TypeError, ValueError):
            mileage = None

    photos: list[str] = []
    raw_photos = merged.get("listing_photos")
    for photo in raw_photos if isinstance(raw_photos, list) else []:
        uri = _get(photo, "image", "uri")
        if isinstance(uri, str) and uri and uri not in photos:
            photos.append(uri)

    price = _price(merged)
    return Listing(
        source="fb",
        source_id=source_id,
        url=ITEM_URL.format(source_id),
        title=title,
        price=price if price is not None else 0,
        year=int(year_m.group(1)) if year_m else None,
        make=merged.get("vehicle_make_display_name"),
        model=merged.get("vehicle_model_display_name"),
        trim=merged.get("vehicle_trim_display_name"),
        mileage=mileage,
        transmission=merged.get("vehicle_transmission_type"),
        title_status=merged.get("vehicle_title_status"),
        location=_get(merged, "location_text", "text"),
        description=_get(merged, "redacted_description", "text"),
        photos=photos,
        seller_type=merged.get("vehicle_seller_type"),
        published_at=_ts(merged.get("creation_time")),
    )


# -------------------------------------------------------------------- fetching

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}


@dataclass
class FetchResult:
    status: int
    final_url: str
    text: str

    @property
    def blocked(self) -> bool:
        return "/login" in self.final_url


class Fetcher:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            headers=_HEADERS, timeout=30.0, follow_redirects=True
        )

    def get(self, url: str) -> FetchResult:
        resp = self.client.get(url)
        return FetchResult(
            status=resp.status_code, final_url=str(resp.url), text=resp.text
        )


@dataclass
class FbBatch:
    cards: list[Card] = field(default_factory=list)
    listings: list[Listing] = field(default_factory=list)
    blocked: bool = False
    errors: list[str] = field(default_factory=list)


def fetch_new(
    fetcher: Fetcher,
    settings: Settings,
    known_ids: set[str],
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> FbBatch:
    rng = rng or random.Random()
    batch = FbBatch()
    try:
        res = fetcher.get(search_url(settings.search))
    except httpx.HTTPError as exc:
        batch.errors.append(f"search: {exc!r}")
        return batch
    if res.blocked:
        batch.blocked = True
        return batch
    page = parse_search(res.text)
    batch.cards = page.cards
    batch.blocked = page.blocked
    if page.blocked:
        return batch

    cards = sorted(
        (c for c in page.cards if c.source_id not in known_ids),
        key=lambda c: c.creation_time or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[: settings.poll.max_details_per_run]
    lo, hi = settings.poll.detail_delay_s
    for i, card in enumerate(cards):
        if i:
            sleep(rng.uniform(lo, hi))
        try:
            item = fetcher.get(card.url)
            if item.blocked:
                batch.errors.append(f"{card.source_id}: redirected to login")
                continue
            batch.listings.append(parse_item(item.text, card.source_id))
        except Exception as exc:  # noqa: BLE001 - one bad item must not lose the batch
            batch.errors.append(f"{card.source_id}: {exc!r}")
    return batch
