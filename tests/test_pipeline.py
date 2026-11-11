from datetime import UTC, datetime

import pytest

from carhunt import pipeline
from carhunt.analyze import AnalyzeError, Verdict
from carhunt.config import Settings
from carhunt.models import Listing
from carhunt.sources.fb import Card, FbBatch
from carhunt.store import Store

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
CARS = {"1": "Civic", "2": "Corolla", "3": "Focus"}


def verdict(kind="worth_seeing", urgent=False, reliability=80, value=80):
    return Verdict(
        verdict=kind, reliability=reliability, value=value, summary="s", reasons_for=[],
        risks=[], expected_costs="$0", questions_for_seller=[],
        needs_clarification_urgent=urgent, facts_unknown=[],
    )


def listing(sid, price=3000, model=None, mileage=100000):
    return Listing(
        source="fb", source_id=sid, url=f"https://fb/{sid}", title=f"car {sid}",
        price=price, year=2008, make="Honda", model=model or CARS.get(sid, "X"),
        mileage=mileage, location="Pittsburgh, PA",
    )


def card(sid, price=3000):
    return Card(source_id=sid, url=f"https://fb/{sid}", title="t", price=price)


class FakeFetcher:
    """Each poll consumes one scripted batch; detailed listings are only
    returned for ids the pipeline said it already knows nothing about."""

    def __init__(self):
        self.script = []
        self.known_seen = []

    def push(self, cards=(), listings=(), blocked=False):
        self.script.append(FbBatch(list(cards), list(listings), blocked))


class FakeNotifier:
    def __init__(self):
        self.cards = []
        self.texts = []

    def send_card(self, lst, v, detected, reason, old_price=None):
        self.cards.append((lst.source_id, lst.price, reason, old_price))
        return len(self.cards)

    def send_text(self, text):
        self.texts.append(text)
        return 1


class FakeAnalyzer:
    def __init__(self, store):
        self.store = store
        self.verdicts = {}  # source_id -> Verdict
        self.fail = set()  # source_ids that raise
        self.calls = []

    def __call__(self, lst, reason):
        self.calls.append((lst.source_id, reason))
        lid = self.store.db.execute(
            "SELECT id FROM listings WHERE source_id=?", (lst.source_id,)
        ).fetchone()["id"]
        if lst.source_id in self.fail:
            self.store.set_status(lid, "pending_analysis")
            raise AnalyzeError("boom")
        self.store.set_status(lid, "analyzed")
        return self.verdicts.get(lst.source_id, verdict("skip"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    fetcher = FakeFetcher()

    def fake_fetch(f, settings, known, sleep):
        f.known_seen.append(set(known))
        return f.script.pop(0)

    monkeypatch.setattr(pipeline, "fetch_new", fake_fetch)
    e = type("Env", (), {})()
    e.store, e.fetcher = store, fetcher
    e.analyzer, e.notifier = FakeAnalyzer(store), FakeNotifier()
    e.settings = Settings()
    e.poll = lambda: pipeline.poll(
        e.settings, store, fetcher, e.analyzer, e.notifier,
        now=lambda: NOW, sleep=lambda s: None,
    )
    return e


def first_run(env):
    env.analyzer.verdicts["1"] = verdict("worth_seeing")
    env.fetcher.push(
        [card(s) for s in "123"], [listing(s) for s in "123"]
    )
    return env.poll()


def test_new_cards_one_notified_two_silent(env):
    s = first_run(env)
    assert (s.cards, s.new, s.analyzed, s.notified) == (3, 3, 3, 1)
    assert [c[0] for c in env.notifier.cards] == ["1"]
    assert env.store.pending_analysis() == []


@pytest.mark.parametrize(
    ("v", "sent"),
    [
        (verdict("worth_seeing", reliability=80, value=80), True),
        (verdict("worth_seeing", reliability=70, value=70), True),
        (verdict("worth_seeing", reliability=80, value=60), False),
        (verdict("worth_seeing", reliability=60, value=90), False),
        (verdict("doubtful", reliability=90, value=90), False),
        (verdict("skip", urgent=True), False),
        (verdict("worth_seeing", urgent=True, reliability=60, value=60), False),
        (verdict("worth_seeing", urgent=True, reliability=80, value=80), True),
    ],
)
def test_new_listing_notified_only_when_worth_seeing_and_scores_high(env, v, sent):
    env.analyzer.verdicts["1"] = v
    env.fetcher.push([card("1")], [listing("1")])
    env.poll()
    assert [c[0] for c in env.notifier.cards] == (["1"] if sent else [])


@pytest.mark.parametrize(("rel", "val", "sent"), [(75, 75, True), (75, 50, False)])
def test_price_drop_reanalysis_applies_same_thresholds(env, rel, val, sent):
    first_run(env)
    env.analyzer.verdicts["1"] = verdict("worth_seeing", reliability=rel, value=val)
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert (len(env.notifier.cards) == 2) is sent


def test_thresholds_come_from_settings(env):
    from dataclasses import replace

    env.settings = replace(
        env.settings,
        notify=replace(env.settings.notify, min_reliability=50, min_value=50),
    )
    env.analyzer.verdicts["1"] = verdict("worth_seeing", reliability=60, value=60)
    env.fetcher.push([card("1")], [listing("1")])
    env.poll()
    assert [c[0] for c in env.notifier.cards] == ["1"]


def test_same_cards_next_run_no_analysis_no_alert(env):
    first_run(env)
    env.analyzer.calls.clear()
    env.fetcher.push([card(s) for s in "123"])
    s = env.poll()
    assert env.analyzer.calls == []
    assert len(env.notifier.cards) == 1
    assert (s.new, s.notified) == (0, 0)
    assert env.fetcher.known_seen[1] == {"1", "2", "3"}


def test_price_drop_reanalyzes_and_alerts_again(env):
    first_run(env)
    env.fetcher.push([card("1", 2600), card("2"), card("3")])
    env.poll()
    assert env.analyzer.calls[-1] == ("1", "price_drop")
    assert env.notifier.cards[-1] == ("1", 2600, "price_drop", 3000)
    row = env.store.db.execute("SELECT price FROM listings WHERE source_id='1'").fetchone()
    assert row["price"] == 2600


def test_small_drop_does_nothing(env):
    first_run(env)
    env.analyzer.calls.clear()
    env.fetcher.push([card("1", 2950), card("2"), card("3")])
    env.poll()
    assert env.analyzer.calls == []
    assert len(env.notifier.cards) == 1


def test_price_drop_to_skip_verdict_is_not_notified(env):
    first_run(env)
    env.analyzer.verdicts["1"] = verdict("skip")
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert len(env.notifier.cards) == 1


def test_failed_analysis_stays_pending_then_succeeds(env):
    env.analyzer.fail.add("1")
    env.analyzer.verdicts["1"] = verdict("worth_seeing")
    env.fetcher.push([card("1")], [listing("1")])
    s = env.poll()
    assert s.errors == 1 and env.notifier.cards == []
    assert env.store.pending_analysis() != []

    env.analyzer.fail.clear()
    env.fetcher.push([card("1")])
    env.poll()
    assert env.store.pending_analysis() == []
    assert [c[0] for c in env.notifier.cards] == ["1"]
    assert env.fetcher.known_seen[1] == {"1"}  # pending is not re-fetched


def test_pending_capped_by_max_details_per_run(env):
    from dataclasses import replace

    env.settings = replace(env.settings, poll=replace(env.settings.poll, max_details_per_run=2))
    env.fetcher.push([card(s) for s in "123"], [listing(s) for s in "123"])
    env.poll()
    assert [c[0] for c in env.analyzer.calls] == ["1", "2"]
    env.fetcher.push([card(s) for s in "123"])
    env.poll()
    assert env.analyzer.calls[-1] == ("3", "new")


def test_failed_price_drop_reanalysis_is_retried(env):
    first_run(env)
    env.analyzer.fail.add("1")
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert len(env.notifier.cards) == 1
    env.analyzer.fail.clear()
    env.fetcher.push([card("1", 2600)])
    env.poll()
    assert env.notifier.cards[-1] == ("1", 2600, "price_drop", 3000)


def test_blocked_runs_alert_exactly_once_per_streak(env):
    def blocked():
        env.fetcher.push(blocked=True)
        return env.poll()

    blocked()
    assert env.notifier.texts == []
    s = blocked()
    assert s.blocked and len(env.notifier.texts) == 1
    assert "2 запусков" in env.notifier.texts[0]
    blocked()
    assert len(env.notifier.texts) == 1

    env.fetcher.push([card("1")], [listing("1")])
    env.poll()  # healthy
    blocked()
    blocked()
    assert len(env.notifier.texts) == 2
    runs = env.store.recent_runs(1)
    assert runs[0]["note"] == "blocked" and runs[0]["errors"] == 1


def test_zero_card_runs_count_as_bad(env):
    env.fetcher.push()
    env.poll()
    env.fetcher.push()
    env.poll()
    assert len(env.notifier.texts) == 1


def test_repost_same_price_is_silent(env):
    first_run(env)
    env.analyzer.calls.clear()
    env.fetcher.push([card("1"), card("9")], [listing("9", model="Civic")])
    s = env.poll()
    assert env.analyzer.calls == []
    assert len(env.notifier.cards) == 1
    assert s.new == 1
    assert env.store.pending_analysis() == []


def test_repost_with_price_drop_is_analyzed_and_alerted(env):
    first_run(env)
    env.analyzer.verdicts["9"] = verdict("worth_seeing")
    env.fetcher.push([card("1"), card("9", 2600)], [listing("9", price=2600, model="Civic")])
    env.poll()
    assert env.analyzer.calls[-1] == ("9", "price_drop")
    assert env.notifier.cards[-1] == ("9", 2600, "price_drop", 3000)
    assert env.store.pending_analysis() == []
