import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from carhunt import cli
from carhunt.store import Store
from carhunt.web import render_page

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
HOSTILE = "</script><script>alert(1)</script> & <b>x</b>"
DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def add(store, n, title="t", price=5000, photos='["https://img/1.jpg"]'):
    cur = store.db.execute(
        """INSERT INTO listings (source, source_id, url, title, price, year, make, model,
        photos, detected_at, fingerprint, status)
        VALUES ('fb', ?, ?, ?, ?, 2010, 'Honda', 'Civic', ?, ?, ?, 'analyzed')""",
        (f"fb{n}", f"https://fb/{n}", title, price, photos, NOW.isoformat(), f"fp{n}"),
    )
    store.db.execute(
        "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,?,?)",
        (cur.lastrowid, price, NOW.isoformat()),
    )
    store.db.commit()
    return cur.lastrowid


def build(tmp_path, title="t"):
    store = Store(tmp_path / "t.db")
    a = add(store, 1, title)
    b = add(store, 2)
    c = add(store, 3, price=4000)
    store.db.execute(
        "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,3500,?)",
        (c, NOW.isoformat()),
    )
    store.db.execute("UPDATE listings SET price=3500 WHERE id=?", (c,))
    store.record_analysis(a, "triage", "haiku", "worth_seeing", 80, 70, {"x": 1}, 5, "new")
    store.record_analysis(
        a, "judge", "opus", "worth_seeing", 85, 75, {"summary": "хорошо"}, 30, "new"
    )
    store.record_notification(a, 9)
    store.record_analysis(b, "triage", "haiku", "skip", 20, 10, {"summary": "no"}, 4, "new")
    store.close()
    return Store(tmp_path / "t.db")


def data_of(html):
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.DOTALL)
    return json.loads(m.group(1))


def test_page_embeds_every_listing_with_state(tmp_path):
    store = build(tmp_path)
    data = data_of(render_page(store, NOW, ZoneInfo("America/New_York")))
    by_id = {x["id"]: x for x in data["listings"]}
    assert len(by_id) == 3
    assert by_id[1]["verdict"] == "worth_seeing" and by_id[1]["notified"] is True
    assert by_id[1]["analyses"]["judge"]["json"]["summary"] == "хорошо"
    assert by_id[1]["analyses"]["triage"]["model"] == "haiku"
    assert by_id[2]["verdict"] == "skip" and by_id[2]["notified"] is False
    assert "judge" not in by_id[2]["analyses"]
    assert by_id[3]["verdict"] is None
    assert [p["price"] for p in by_id[3]["price_history"]] == [4000, 3500]
    assert data["meta"]["found"] == 3 and data["meta"]["notified"] == 1
    assert by_id[1]["detected_local"] == "2026-11-10 07:00"


def test_last_run_skips_unfinished_run(tmp_path):
    store = Store(tmp_path / "t.db")
    store.start_run("fb")
    assert data_of(render_page(store, NOW))["meta"]["run"] is None
    store.finish_run(1, 24, 3, 0)
    store.start_run("fb")
    run = data_of(render_page(store, NOW))["meta"]["run"]
    assert run["cards"] == 24 and run["new"] == 3


def test_hostile_text_cannot_break_out_and_round_trips(tmp_path):
    store = build(tmp_path, title=HOSTILE)
    html = render_page(store, NOW)
    assert "alert(1)" in html  # present, but only inside the JSON, escaped
    assert "</script><script>" not in html
    assert html.count("</script>") == 2  # data block + code block
    assert "<b>x</b>" not in html
    titles = [x["title"] for x in data_of(html)["listings"]]
    assert HOSTILE in titles


def test_page_is_self_contained_and_never_uses_innerhtml(tmp_path):
    html = render_page(build(tmp_path), NOW)
    assert "<script src=" not in html
    assert not re.search(r'<link[^>]+rel=["\']?stylesheet', html)
    assert "innerHTML" not in html
    assert "prefers-color-scheme" in html


def test_cli_web_writes_file_atomically(tmp_path, capsys):
    build(tmp_path).close()
    out = tmp_path / "x" / "index.html"
    code = cli.main(["--db", str(tmp_path / "t.db"), "web", "--out", str(out)], now=NOW)
    assert code == 0
    assert len(data_of(out.read_text(encoding="utf-8"))["listings"]) == 3
    assert [p.name for p in out.parent.iterdir()] == ["index.html"]  # no temp left


def publish(tmp_path, ssh_exit, extra_env=None, dotenv=None):
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    shutil.copy(DEPLOY / "publish-web.sh", root / "deploy" / "publish-web.sh")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    carhunt = venv_bin / "carhunt"
    carhunt.write_text(
        '#!/bin/sh\n[ "$1" = web ] && [ "$2" = --out ] || exit 2\n'
        'mkdir -p "$(dirname "$3")"; printf "<html>PAGE</html>" > "$3"\n'
    )
    carhunt.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text(
        f'#!/bin/sh\necho "$@" > "{tmp_path}/argv"\ncat > "{tmp_path}/stdin"\nexit {ssh_exit}\n'
    )
    ssh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("CARHUNT_WEB_SSH", None)
    env.pop("CARHUNT_WEB_DIR", None)
    env.update(extra_env or {})
    if dotenv is not None:
        (root / ".env").write_text(dotenv)
    return subprocess.run(
        ["bash", str(root / "deploy" / "publish-web.sh")],
        check=False, env=env, capture_output=True, text=True,
    )


def test_publish_sends_page_over_ssh_from_env(tmp_path):
    proc = publish(
        tmp_path, 0, {"CARHUNT_WEB_SSH": "me@host", "CARHUNT_WEB_DIR": "/srv/cars"}
    )
    assert proc.returncode == 0, proc.stderr
    argv = (tmp_path / "argv").read_text()
    assert "BatchMode=yes" in argv and "me@host" in argv and "/srv/cars" in argv
    assert (tmp_path / "stdin").read_text() == "<html>PAGE</html>"


def test_publish_without_host_skips_and_never_calls_ssh(tmp_path):
    proc = publish(tmp_path, 0)
    assert proc.returncode == 0
    assert "publish skipped: CARHUNT_WEB_SSH not set" in proc.stderr
    assert not (tmp_path / "argv").exists()
    assert (tmp_path / "repo" / "data" / "web" / "index.html").exists()


def test_publish_empty_host_in_dotenv_skips(tmp_path):
    proc = publish(tmp_path, 0, dotenv="CARHUNT_WEB_SSH=\nCARHUNT_WEB_DIR=\n")
    assert proc.returncode == 0
    assert "publish skipped" in proc.stderr
    assert not (tmp_path / "argv").exists()


def test_publish_reads_host_and_dir_from_dotenv_without_sourcing(tmp_path):
    marker = tmp_path / "sourced"
    dotenv = (
        f"# c\ntouch {marker}\n$(touch {marker})\n"
        'CARHUNT_WEB_SSH="dot@host"\nCARHUNT_WEB_DIR=/dot/dir\n'
    )
    proc = publish(tmp_path, 0, dotenv=dotenv)
    assert proc.returncode == 0, proc.stderr
    argv = (tmp_path / "argv").read_text()
    assert "dot@host" in argv and "/dot/dir" in argv
    assert not marker.exists()


def test_publish_environment_beats_dotenv(tmp_path):
    proc = publish(
        tmp_path,
        0,
        {"CARHUNT_WEB_SSH": "env@host", "CARHUNT_WEB_DIR": "/env/dir"},
        dotenv="CARHUNT_WEB_SSH=dot@host\nCARHUNT_WEB_DIR=/dot/dir\n",
    )
    assert proc.returncode == 0, proc.stderr
    argv = (tmp_path / "argv").read_text()
    assert "env@host" in argv and "/env/dir" in argv and "dot@host" not in argv


def test_publish_failure_never_fails_the_poll(tmp_path):
    proc = publish(
        tmp_path, 255, {"CARHUNT_WEB_SSH": "me@host", "CARHUNT_WEB_DIR": "/srv/cars"}
    )
    assert proc.returncode == 0
    assert "failed" in proc.stderr
