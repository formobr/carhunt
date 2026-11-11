import os
from datetime import UTC, datetime

import pytest

from carhunt.config import load_env, load_settings
from carhunt.models import Listing
from carhunt.store import Store

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 2, tzinfo=UTC)


def mk(source_id="a1", price=5000, **kw):
    base = {
        "source": "fb",
        "source_id": source_id,
        "url": "http://x/" + source_id,
        "title": "Car",
        "price": price,
        "year": 2010,
        "make": "Honda",
        "model": "Civic",
        "mileage": 120400,
        "location": "Pittsburgh, PA",
    }
    base.update(kw)
    return Listing(**base)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "sub" / "t.db")


def test_new_then_known_listing_is_not_duplicated(store):
    r1 = store.upsert_listing(mk(), T0)
    r2 = store.upsert_listing(mk(), T1)
    assert r1.is_new and not r2.is_new
    assert r1.listing_id == r2.listing_id
    assert r2.old_price is None
    n = store.db.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert n == 1
    assert store.db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1


def test_price_change_recorded_with_old_price(store):
    r1 = store.upsert_listing(mk(price=5000), T0)
    r2 = store.upsert_listing(mk(price=4500), T1)
    assert r2.old_price == 5000
    prices = [
        r[0] for r in store.db.execute(
            "SELECT price FROM price_history WHERE listing_id=? ORDER BY id",
            (r1.listing_id,),
        )
    ]
    assert prices == [5000, 4500]
    assert store.similar_prices("honda", "civic", 2011) == [4500]


def test_repost_detected_by_fingerprint(store):
    first = store.upsert_listing(mk("a1"), T0)
    repost = store.upsert_listing(mk("b2", mileage=120200), T1)
    assert repost.is_new
    assert repost.repost_of == first.listing_id
    other = store.upsert_listing(mk("c3", year=2015), T1)
    assert other.repost_of is None


def test_pending_analysis_and_status(store):
    a = store.upsert_listing(mk("a1"), T0).listing_id
    b = store.upsert_listing(mk("b1", year=2001), T0).listing_id
    assert store.pending_analysis() == [a, b]
    store.set_status(a, "analyzed")
    assert store.pending_analysis() == [b]
    store.record_analysis(a, "quick", "m", "buy", 7, 8, {"k": 1}, 1.5, "new")
    assert store.latest_analysis(a)["json"] == {"k": 1}
    assert store.latest_analysis(b) is None


def test_was_notified(store):
    a = store.upsert_listing(mk(), T0).listing_id
    assert not store.was_notified(a)
    store.record_notification(a, 42)
    assert store.was_notified(a)


def test_runs(store):
    rid = store.start_run("fb")
    store.finish_run(rid, cards=3, new=1, errors=0, note="ok")
    runs = store.recent_runs(5)
    assert runs[0]["cards"] == 3 and runs[0]["note"] == "ok"


def test_unknown_config_key_refused(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("search:\n  max_pricee: 1\n")
    with pytest.raises(ValueError, match="max_pricee"):
        load_settings(p)
    p.write_text("bogus: {}\n")
    with pytest.raises(ValueError, match="bogus"):
        load_settings(p)


def test_missing_config_keys_take_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("search:\n  max_price: 7000\n")
    s = load_settings(p)
    assert s.search.max_price == 7000
    assert s.search.min_price == 300
    assert s.poll.detail_delay_s == (2, 5)
    assert s.health.fail_runs_before_alert == 2


def test_config_env_overrides_apply_with_coercion(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("notify:\n  min_value: 50\n")
    env = {
        "CARHUNT__NOTIFY__MIN_VALUE": "65",
        "carhunt__poll__detail_delay_s": "[1, 3]",
        "CARHUNT__AI__JUDGE_MODEL": "sonnet",
        "UNRELATED": "x",
    }
    s = load_settings(p, env)
    assert s.notify.min_value == 65
    assert s.poll.detail_delay_s == (1, 3)
    assert s.ai.judge_model == "sonnet"
    assert load_settings(p, {}).notify.min_value == 50


def test_config_env_reads_os_environ_by_default(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("")
    monkeypatch.setenv("CARHUNT__SEARCH__MAX_PRICE", "9000")
    assert load_settings(p).search.max_price == 9000


@pytest.mark.parametrize(
    "name",
    [
        "CARHUNT__NOTIFY__BOGUS",
        "CARHUNT__BOGUS__MIN_VALUE",
        "CARHUNT__NOTIFY",
        "CARHUNT__NOTIFY__MIN_VALUE__X",
    ],
)
def test_config_env_unknown_override_refused(tmp_path, name):
    p = tmp_path / "c.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="unknown config key"):
        load_settings(p, {name: "1"})


def test_env_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("CH_TEST_SET", "orig")
    monkeypatch.delenv("CH_TEST_NEW", raising=False)
    p = tmp_path / ".env"
    p.write_text('# c\n\nCH_TEST_SET=other\nCH_TEST_NEW="val"\n')
    parsed = load_env(p)
    assert parsed == {"CH_TEST_SET": "other", "CH_TEST_NEW": "val"}
    assert os.environ["CH_TEST_SET"] == "orig"
    assert os.environ["CH_TEST_NEW"] == "val"
    monkeypatch.delenv("CH_TEST_NEW")
