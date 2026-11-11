import argparse
import json
import sys
from datetime import UTC, datetime, timedelta


def _poll() -> int:
    import httpx

    from .analyze import ClaudeRunner, analyze
    from .config import load_settings
    from .notify import TelegramNotifier, telegram_creds
    from .pipeline import poll
    from .sources.fb import Fetcher
    from .store import Store

    settings = load_settings()
    store = Store("data/carhunt.db")
    token, chat = telegram_creds()
    with httpx.Client(timeout=30.0, follow_redirects=True) as http, httpx.Client(
        timeout=30.0
    ) as tg:
        runner = ClaudeRunner()
        summary = poll(
            settings,
            store,
            Fetcher(),
            lambda listing, reason: analyze(listing, store, runner, http, settings, reason),
            TelegramNotifier(token, chat, tg),
        )
    print(
        f"carhunt: cards={summary.cards} new={summary.new} analyzed={summary.analyzed} "
        f"notified={summary.notified} errors={summary.errors}"
        + (" blocked" if summary.blocked else "")
    )
    return 0


DB_PATH = "data/carhunt.db"


def _interval_min() -> float:
    from .config import PollSettings, load_settings

    try:
        return load_settings().poll.interval_min
    except (OSError, ValueError, TypeError):
        return PollSettings().interval_min


def _local(value: str | None) -> str:
    from .stats import parse_ts

    dt = parse_ts(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def _log(store, args, now: datetime) -> int:
    sql = """SELECT l.*, (SELECT verdict FROM analyses a WHERE a.listing_id=l.id
                 AND a.verdict IS NOT NULL ORDER BY a.id DESC LIMIT 1) AS v,
             (SELECT reliability FROM analyses a WHERE a.listing_id=l.id
                 AND a.verdict IS NOT NULL ORDER BY a.id DESC LIMIT 1) AS rel,
             (SELECT value FROM analyses a WHERE a.listing_id=l.id
                 AND a.verdict IS NOT NULL ORDER BY a.id DESC LIMIT 1) AS val
             FROM listings l ORDER BY l.detected_at DESC, l.id DESC"""
    since = now - timedelta(days=args.days) if args.days is not None else None
    from .stats import parse_ts

    shown = 0
    for r in store.db.execute(sql):
        if since is not None and parse_ts(r["detected_at"]) < since:
            continue
        if args.verdict and r["v"] != args.verdict:
            continue
        if args.limit is not None and shown >= args.limit:
            break
        if shown == 0:
            print("detected\tverdict\trel/val\tprice\tcar\tmileage\turl")
        shown += 1
        rv = "-" if r["rel"] is None else f"{r['rel']:g}/{r['val']:g}"
        car = " ".join(str(p) for p in (r["year"], r["make"], r["model"]) if p) or (
            r["title"] or "-"
        )
        price = "-" if r["price"] is None else f"${r['price']}"
        miles = "-" if r["mileage"] is None else str(r["mileage"])
        print(
            f"{_local(r['detected_at'])}\t{r['v'] or '-'}\t{rv}\t{price}\t{car}\t{miles}\t{r['url']}"
        )
    if shown == 0:
        print("no listings")
    return 0


def _show(store, args) -> int:
    key = args.id
    row = None
    if key.isdigit():
        row = store.db.execute("SELECT * FROM listings WHERE id=?", (int(key),)).fetchone()
    if row is None:
        row = store.db.execute("SELECT * FROM listings WHERE source_id=?", (key,)).fetchone()
    if row is None:
        print(f"carhunt: no listing {key!r}", file=sys.stderr)
        return 1
    for k in row.keys():  # noqa: SIM118 - sqlite3.Row iterates values, not keys
        if k == "raw":
            continue
        print(f"{k}: {row[k]}")
    print("price history:")
    for p in store.db.execute(
        "SELECT * FROM price_history WHERE listing_id=? ORDER BY id", (row["id"],)
    ):
        print(f"  {p['seen_at']}  ${p['price']}")
    print("analyses:")
    for a in store.db.execute(
        "SELECT * FROM analyses WHERE listing_id=? ORDER BY id", (row["id"],)
    ):
        print(
            f"  [{a['stage']}] model={a['model']} verdict={a['verdict']} "
            f"rel={a['reliability']} val={a['value']} duration={a['duration_s']}s "
            f"reason={a['reason']} at {a['created_at']}"
        )
        if a["json"]:
            print(json.dumps(json.loads(a["json"]), indent=2, ensure_ascii=False))
    print("notifications:")
    for n in store.db.execute(
        "SELECT * FROM notifications WHERE listing_id=? ORDER BY id", (row["id"],)
    ):
        print(f"  {n['sent_at']}  message_id={n['telegram_message_id']}")
    return 0


def _make_notifier(http):
    from .notify import TelegramNotifier, telegram_creds

    token, chat = telegram_creds()
    return TelegramNotifier(token, chat, http)


def _summary(store, now: datetime) -> int:
    import httpx

    from .stats import compute, format_stats

    text = format_stats(compute(store, now, 1, _interval_min()))
    with httpx.Client(timeout=30.0) as http:
        _make_notifier(http).send_text(text)
    print("carhunt: summary sent")
    return 0


def _web(store, out: str, now: datetime) -> int:
    import os
    import tempfile
    from pathlib import Path

    from .web import render_page

    html = render_page(store, now)
    dest = Path(out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"carhunt: wrote {dest}")
    return 0


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carhunt")
    parser.add_argument("--db", default=DB_PATH)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("poll")
    p = sub.add_parser("log")
    p.add_argument("--verdict", choices=["worth_seeing", "doubtful", "skip"])
    p.add_argument("--days", type=float)
    p.add_argument("--limit", type=int)
    p = sub.add_parser("show")
    p.add_argument("id")
    p = sub.add_parser("stats")
    p.add_argument("--days", type=float, default=7)
    sub.add_parser("summary")
    p = sub.add_parser("web")
    p.add_argument("--out", default="data/web/index.html")
    args = parser.parse_args(argv)
    from .config import load_env

    load_env()
    now = now or datetime.now(UTC)
    if args.command == "poll":
        try:
            return _poll()
        except Exception as exc:  # noqa: BLE001 - crash -> exit 1
            print(f"carhunt: crashed: {exc!r}", file=sys.stderr)
            return 1
    if args.command is None:
        parser.print_usage(sys.stderr)
        return 1
    from .store import Store

    store = Store(args.db)
    try:
        if args.command == "log":
            return _log(store, args, now)
        if args.command == "show":
            return _show(store, args)
        if args.command == "web":
            return _web(store, args.out, now)
        if args.command == "stats":
            from .stats import compute, format_stats

            print(format_stats(compute(store, now, args.days, _interval_min())))
            return 0
        try:
            return _summary(store, now)
        except Exception as exc:  # noqa: BLE001
            print(f"carhunt: summary failed: {exc!r}", file=sys.stderr)
            return 1
    finally:
        store.close()
