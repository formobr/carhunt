import itertools
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from carhunt.config import PollSettings, SearchSettings, Settings
from carhunt.sources.fb import (
    FetchResult,
    ItemUnavailable,
    fetch_new,
    parse_item,
    parse_search,
    price_bands,
    search_url,
)

FIX = Path(__file__).parent / "fixtures"
ITEM_ID = "1888659702303322"


@pytest.fixture(scope="module")
def search_html():
    return (FIX / "fb_search_pittsburgh.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def item_html():
    return (FIX / "fb_item.html").read_text(encoding="utf-8")


def test_search_url():
    url = search_url(SearchSettings(source_city="pittsburgh", min_price=300, max_price=6000))
    assert url == (
        "https://www.facebook.com/marketplace/pittsburgh/vehicles"
        "?minPrice=300&maxPrice=6000&sortBy=creation_time_descend&exact=false"
    )


def test_parse_search_fixture(search_html):
    page = parse_search(search_html)
    assert not page.blocked
    assert page.has_next_page
    assert len(page.cards) >= 20
    ids = [c.source_id for c in page.cards]
    assert len(ids) == len(set(ids))
    for c in page.cards:
        assert c.source_id and c.price is not None
        assert c.url == f"https://www.facebook.com/marketplace/item/{c.source_id}/"
        assert not c.is_sold and not c.is_pending
    first = next(c for c in page.cards if c.source_id == ITEM_ID)
    assert first.title == "2014 Buick LaCrosse · Leather Sedan 4D"
    assert first.price == 2500
    assert first.mileage_text == "164K miles"
    assert first.state == "PA"
    assert first.primary_photo.startswith("https://")
    assert first.creation_time == datetime.fromtimestamp(1794321665, tz=UTC)


def test_parse_item_fixture(item_html):
    item = parse_item(item_html, ITEM_ID)
    assert item.source == "fb" and item.source_id == ITEM_ID
    assert item.url == f"https://www.facebook.com/marketplace/item/{ITEM_ID}/"
    assert item.price == 2500
    assert item.year == 2014
    assert (item.make, item.model, item.trim) == ("Buick", "LaCrosse", "Leather Sedan 4D")
    assert item.mileage == 164000
    assert item.transmission == "AUTOMATIC"
    assert item.title_status is None
    assert item.seller_type == "PRIVATE_SELLER"
    assert item.location == "Pittsburgh, PA"
    assert item.description.startswith("2014 Buick Lacrosse all-wheel-drive")
    assert item.published_at == datetime.fromtimestamp(1794321665, tz=UTC)
    assert item.photos
    assert len(item.photos) == len(set(item.photos))
    assert all(p.startswith("https://") for p in item.photos)


def _page(payload: dict) -> str:
    return f'<html><script type="application/json">{json.dumps(payload)}</script></html>'


def test_item_kilometers_and_missing_fields():
    html = _page(
        {
            "x": {
                "__typename": "GroupCommerceProductItem",
                "id": "7",
                "marketplace_listing_title": "no year here",
                "vehicle_odometer_data": {"unit": "KILOMETERS", "value": 100000},
            }
        }
    )
    item = parse_item(html, "7")
    assert item.mileage == 62137
    assert item.year is None and item.make is None and item.photos == []
    assert item.description is None and item.published_at is None


def test_item_unavailable():
    with pytest.raises(ItemUnavailable):
        parse_item(_page({"other": {"id": "1"}}), "7")


def test_blocked_pages():
    login = _page({"preloader": "useCometLogInFormQuery", "a": {"marketplace_feed_stories": {}}})
    assert parse_search(login).blocked
    assert parse_search("<html>redirecting</html>").blocked
    page = parse_search(login)
    assert page.cards == []


def test_healthy_empty_page_not_blocked():
    html = _page({"a": {"marketplace_feed_stories": {"edges": []}}})
    page = parse_search(html)
    assert not page.blocked and page.cards == []


def test_sold_and_pending_skipped():
    def listing(i, **kw):
        return {
            "listing": {
                "__typename": "GroupCommerceProductItem",
                "id": i,
                "marketplace_listing_title": "t",
                "listing_price": {"amount": "100.00"},
                **kw,
            }
        }

    html = _page(
        {"l": [listing("1"), listing("1"), listing("2", is_sold=True), listing("3", is_pending=True)]}
    )
    assert [c.source_id for c in parse_search(html).cards] == ["1"]


class FakeFetcher:
    def __init__(self, search_text, items, search_url_final="https://www.facebook.com/marketplace/x"):
        self.search_text = search_text
        self.items = items
        self.final = search_url_final
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if "/vehicles" in url:
            return FetchResult(200, self.final, self.search_text)
        sid = url.rstrip("/").rsplit("/", 1)[1]
        text = self.items.get(sid)
        if text is None:
            raise RuntimeError("boom")
        return FetchResult(200, url, text)


def _search_with(ids):
    nodes = [
        {
            "listing": {
                "__typename": "GroupCommerceProductItem",
                "id": i,
                "marketplace_listing_title": f"2010 Car {i}",
                "listing_price": {"amount": "500.00"},
                "creation_time": 1000 + n,
            }
        }
        for n, i in enumerate(ids)
    ]
    return _page({"marketplace_feed_stories": {"edges": nodes}})


def _item(i):
    return _page(
        {"t": {"__typename": "GroupCommerceProductItem", "id": i,
               "marketplace_listing_title": f"2010 Car {i}",
               "listing_price": {"amount": "500.00"}}}
    )


def test_fetch_new_cap_known_and_errors():
    ids = ["1", "2", "3", "4", "5"]
    fetcher = FakeFetcher(_search_with(ids), {i: _item(i) for i in ["5", "4", "2"]})
    settings = Settings(poll=PollSettings(max_details_per_run=3, detail_delay_s=(2, 5)))
    sleeps = []
    batch = fetch_new(fetcher, settings, {"3"}, sleep=sleeps.append, rng=random.Random(0))

    assert len(batch.cards) == 5
    # newest first (5, 4, then 2), known "3" skipped, capped at 3 detail fetches
    item_calls = [u for u in fetcher.calls if "/item/" in u]
    assert [u.split("/")[-2] for u in item_calls] == ["5", "4", "2"]
    assert [x.source_id for x in batch.listings] == ["5", "4", "2"]
    assert len(sleeps) == 2 and all(2 <= s <= 5 for s in sleeps)
    assert not batch.blocked and not batch.errors


def test_fetch_new_item_error_recorded():
    fetcher = FakeFetcher(_search_with(["1", "2"]), {"2": _item("2")})
    batch = fetch_new(fetcher, Settings(), set(), sleep=lambda s: None)
    assert [x.source_id for x in batch.listings] == ["2"]
    assert len(batch.errors) == 1 and "1" in batch.errors[0]
    assert {c.source_id for c in batch.cards} == {"1", "2"}


def test_fetch_new_login_redirect_blocked():
    fetcher = FakeFetcher("", {}, "https://www.facebook.com/login/?next=x")
    batch = fetch_new(fetcher, Settings(), set(), sleep=lambda s: None)
    assert batch.blocked and batch.cards == [] and len(fetcher.calls) == 1


def test_price_bands_contiguous_and_cover_range():
    s = SearchSettings(min_price=2000, max_price=7000, price_bands=5)
    assert price_bands(s) == [(2000, 3000), (3000, 4000), (4000, 5000), (5000, 6000), (6000, 7000)]
    odd = price_bands(SearchSettings(min_price=300, max_price=1000, price_bands=3))
    assert odd[0][0] == 300 and odd[-1][1] == 1000
    assert all(a[1] == b[0] for a, b in itertools.pairwise(odd))
    assert all(lo < hi for lo, hi in odd)
    assert price_bands(SearchSettings(min_price=2000, max_price=7000)) == [(2000, 7000)]


def test_price_bands_must_be_positive():
    with pytest.raises(ValueError, match="price_bands"):
        SearchSettings(price_bands=0)


def test_search_url_band_override():
    s = SearchSettings(min_price=2000, max_price=7000)
    assert "minPrice=3000&maxPrice=4000" in search_url(s, 3000, 4000)
    assert "minPrice=2000&maxPrice=7000" in search_url(s)


class BandFetcher:
    """Search pages keyed by the band's minPrice: html, an exception, or a final url."""

    def __init__(self, bands):
        self.bands = bands
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        lo = int(url.split("minPrice=")[1].split("&")[0])
        res = self.bands[lo]
        if isinstance(res, Exception):
            raise res
        if res.startswith("https://"):
            return FetchResult(200, res, "")
        return FetchResult(200, url, res)


def _bands_settings(n):
    return Settings(
        search=SearchSettings(min_price=2000, max_price=2000 + 1000 * n, price_bands=n),
        poll=PollSettings(max_details_per_run=0),
    )


def test_fetch_new_merges_bands_without_duplicates():
    fetcher = BandFetcher(
        {2000: _search_with(["1", "2"]), 3000: _search_with(["2", "3"]), 4000: _search_with(["4"])}
    )
    sleeps = []
    batch = fetch_new(fetcher, _bands_settings(3), set(), sleep=sleeps.append, rng=random.Random(0))
    assert len(fetcher.calls) == 3
    ids = [c.source_id for c in batch.cards]
    assert len(ids) == 4 and set(ids) == {"1", "2", "3", "4"}
    assert len(sleeps) == 2 and all(2 <= s <= 5 for s in sleeps)
    assert not batch.blocked and not batch.errors


def test_fetch_new_stops_after_blocked_band():
    fetcher = BandFetcher(
        {
            2000: _search_with(["1"]),
            3000: "https://www.facebook.com/login/?next=x",
            4000: _search_with(["4"]),
        }
    )
    batch = fetch_new(fetcher, _bands_settings(3), set(), sleep=lambda s: None)
    assert batch.blocked
    assert len(fetcher.calls) == 2


def test_fetch_new_band_http_error_does_not_stop_others():
    fetcher = BandFetcher(
        {2000: httpx.ConnectError("x"), 3000: _search_with(["3"]), 4000: _search_with(["4"])}
    )
    batch = fetch_new(fetcher, _bands_settings(3), set(), sleep=lambda s: None)
    assert len(fetcher.calls) == 3
    assert {c.source_id for c in batch.cards} == {"3", "4"}
    assert len(batch.errors) == 1 and not batch.blocked


def test_fetch_new_all_bands_failing_returns_no_cards():
    fetcher = BandFetcher({2000: httpx.ConnectError("a"), 3000: httpx.ConnectError("b")})
    batch = fetch_new(fetcher, _bands_settings(2), set(), sleep=lambda s: None)
    assert batch.cards == [] and batch.listings == [] and len(batch.errors) == 2
