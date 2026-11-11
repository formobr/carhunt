from datetime import UTC, datetime, timedelta

from carhunt import cli, stats
from carhunt.store import Store

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def iso(dt):
    return dt.isoformat()


def add_listing(store, n, detected, published=None, status="analyzed", price=1000):
    cur = store.db.execute(
        """INSERT INTO listings (source, source_id, url, title, price, year, make, model,
        mileage, published_at, detected_at, fingerprint, status)
        VALUES ('fb', ?, ?, 't', ?, 2010, 'Honda', 'Civic', 90000, ?, ?, ?, ?)""",
        (f"fb{n}", f"https://fb/{n}", price, iso(published) if published else None,
         iso(detected), f"fp{n}", status),
    )
    store.db.commit()
    return cur.lastrowid


def add_analysis(store, lid, verdict, model="haiku", stage="triage", dur=10.0, at=NOW):
    store.db.execute(
        """INSERT INTO analyses (listing_id, stage, model, verdict, reliability, value,
        json, duration_s, reason, created_at) VALUES (?,?,?,?,70,60,'{"a": 1}',?,'new',?)""",
        (lid, stage, model, verdict, dur, iso(at)),
    )
    store.db.commit()


def run_cli(tmp_path, capsys, *argv):
    code = cli.main(["--db", str(tmp_path / "t.db"), *argv], now=NOW)
    return code, capsys.readouterr().out


def test_percentile_nearest_rank_hand_computed():
    vals = [float(m * 60) for m in range(1, 11)]  # 1..10 minutes
    assert stats.percentile(vals, 50) == 300.0
    assert stats.percentile(vals, 90) == 540.0
    assert stats.percentile([7.0], 90) == 7.0
    assert stats.percentile([], 50) is None


def test_gaps_only_beyond_twice_interval():
    t0 = NOW - timedelta(hours=3)
    starts = [t0, t0 + timedelta(minutes=30), t0 + timedelta(minutes=61)]
    # exactly 2x (30) is not a gap; 31 min is
    assert stats.find_gaps(starts, 15) == [(starts[1], starts[2])]


def test_stats_counts_latency_and_gap(tmp_path, capsys):
    store = Store(tmp_path / "t.db")
    for i, (start, note, err) in enumerate(
        [(NOW - timedelta(hours=2), None, 0),
         (NOW - timedelta(hours=1, minutes=55), "blocked", 0),
         (NOW - timedelta(hours=1), None, 2)]
    ):
        store.db.execute(
            "INSERT INTO runs (started_at, source, errors, note) VALUES (?, 'fb', ?, ?)",
            (iso(start), err, note),
        )
    # latencies 60s,120s,...,600s
    for m in range(1, 11):
        det = NOW - timedelta(hours=1)
        add_listing(store, m, det, published=det - timedelta(minutes=m))
    add_listing(store, 99, NOW - timedelta(hours=1), status="pending_analysis")
    add_analysis(store, 1, "worth_seeing")
    add_analysis(store, 2, "skip")
    add_analysis(store, 2, "doubtful", model="opus", stage="judge", dur=30.0)
    store.record_notification(1, 5)
    store.db.commit()
    store.close()

    code, out = run_cli(tmp_path, capsys, "stats", "--days", "1")
    assert code == 0
    assert "runs: 3 (blocked 1, with errors 1)" in out
    assert "gaps > 2x interval: 1" in out
    assert "listings found: 11, analyzed: 10, pending: 1, notified: 1" in out
    assert "worth_seeing 1, doubtful 1, skip 0" in out  # latest verdict wins
    assert "p50 5.0 min, p90 9.0 min" in out
    assert "AI haiku: 2 calls, mean 10.0s" in out
    assert "AI opus: 1 calls, mean 30.0s" in out


def test_log_filters_do_not_hide_rows(tmp_path, capsys):
    store = Store(tmp_path / "t.db")
    a = add_listing(store, 1, NOW - timedelta(hours=1))
    b = add_listing(store, 2, NOW - timedelta(hours=2))
    c = add_listing(store, 3, NOW - timedelta(days=5))
    d = add_listing(store, 4, NOW - timedelta(hours=3), status="new")  # no analysis
    add_analysis(store, a, "worth_seeing")
    add_analysis(store, b, "skip")
    add_analysis(store, c, "worth_seeing")
    store.close()

    _, out = run_cli(tmp_path, capsys, "log")
    assert all(f"https://fb/{n}" in out for n in (1, 2, 3, 4))  # unanalyzed shown too
    _, out = run_cli(tmp_path, capsys, "log", "--verdict", "worth_seeing")
    assert "https://fb/1" in out and "https://fb/3" in out
    assert "https://fb/2" not in out and "https://fb/4" not in out
    _, out = run_cli(tmp_path, capsys, "log", "--days", "1")
    assert "https://fb/3" not in out and "https://fb/1" in out
    _, out = run_cli(tmp_path, capsys, "log", "--limit", "2")
    assert out.count("https://fb/") == 2
    assert "https://fb/1" in out and "https://fb/2" in out  # newest first
    assert (c, d) == (3, 4)


def test_show_by_id_and_fb_id(tmp_path, capsys):
    store = Store(tmp_path / "t.db")
    lid = add_listing(store, 7, NOW)
    add_analysis(store, lid, "doubtful")
    store.db.execute(
        "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,1000,?)",
        (lid, iso(NOW)),
    )
    store.record_notification(lid, 42)
    store.close()
    for key in (str(lid), "fb7"):
        code, out = run_cli(tmp_path, capsys, "show", key)
        assert code == 0
        assert "source_id: fb7" in out and "$1000" in out
        assert '"a": 1' in out and "message_id=42" in out
    code, _ = run_cli(tmp_path, capsys, "show", "nope")
    assert code == 1


def test_summary_sends_one_text(tmp_path, capsys, monkeypatch):
    Store(tmp_path / "t.db").close()
    sent = []

    class Fake:
        def send_text(self, text):
            sent.append(text)
            return 1

    monkeypatch.setattr(cli, "_make_notifier", lambda http: Fake())
    monkeypatch.setattr(cli, "_interval_min", lambda: 15)
    code, _ = run_cli(tmp_path, capsys, "summary")
    assert code == 0 and len(sent) == 1 and "last 24 h" in sent[0]
