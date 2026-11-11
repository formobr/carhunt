import json
import stat
from datetime import UTC, datetime

import httpx
import pytest

from carhunt.analyze import (
    AnalyzeError,
    ClaudeRunner,
    RunResult,
    Verdict,
    analyze,
    build_prompt,
    download_photos,
)
from carhunt.config import SearchSettings, Settings
from carhunt.models import Listing
from carhunt.store import Store


def canned(verdict="skip", urgent=False, **over):
    d = {
        "verdict": verdict, "reliability": 50, "value": 40, "summary": "s",
        "reasons_for": [], "risks": ["r"], "expected_costs": "unknown",
        "market_price": None, "negotiation_target": None,
        "questions_for_seller": [], "needs_clarification_urgent": urgent,
        "facts_unknown": [],
    }
    d.update(over)
    return d


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def run(self, prompt, model, schema, cwd, allow_web, timeout_s):
        self.calls.append({"prompt": prompt, "model": model, "schema": schema,
                           "allow_web": allow_web})
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return RunResult(out, 1.5, {model: {"inputTokens": 1}})


class Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class FakeHttp:
    def get(self, url):
        if "bad" in url:
            raise httpx.ConnectError("boom")
        return Resp(b"jpgdata")


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "t.db")
    l = Listing("fb", "1", "http://x", "2010 Toyota Camry", 3000, year=2010,
                make="Toyota", model="Camry",
                photos=["http://p/1", "http://bad/2", "http://p/3"])
    store.upsert_listing(l, datetime(2026, 1, 1, tzinfo=UTC))
    return store, l


def status(store):
    return "pending_analysis" if 1 in store.pending_analysis() else "analyzed"


def test_skip_never_reaches_judge(env):
    store, l = env
    r = FakeRunner([canned("skip")])
    v = analyze(l, store, r, FakeHttp(), Settings(), "new")
    assert v.verdict == "skip"
    assert [c["model"] for c in r.calls] == ["haiku"]
    assert status(store) == "analyzed"


@pytest.mark.parametrize("first,urgent", [("worth_seeing", False), ("doubtful", False), ("skip", True)])
def test_candidate_is_judged_and_judge_verdict_is_final(env, first, urgent):
    store, l = env
    r = FakeRunner([canned(first, urgent), canned("doubtful", reliability=70)])
    v = analyze(l, store, r, FakeHttp(), Settings(), "new")
    assert [c["model"] for c in r.calls] == ["haiku", "opus"]
    assert r.calls[1]["allow_web"] is True
    assert "web search" in r.calls[1]["prompt"]
    assert v.verdict == "doubtful" and v.reliability == 70
    last = store.latest_analysis(1)
    assert last["stage"] == "judge" and last["model"] == "opus"
    assert last["duration_s"] == 1.5 and last["reason"] == "new"


def test_malformed_reply_is_not_recorded(env):
    store, l = env
    bad = canned("worth_seeing")
    del bad["summary"]
    with pytest.raises(AnalyzeError):
        analyze(l, store, FakeRunner([bad]), FakeHttp(), Settings(), "new")
    assert store.latest_analysis(1) is None
    assert status(store) == "pending_analysis"
    with pytest.raises(AnalyzeError):
        analyze(l, store, FakeRunner([canned(reliability=500)]), FakeHttp(), Settings(), "new")
    assert store.latest_analysis(1) is None


def test_runner_error_leaves_pending(env):
    store, l = env
    with pytest.raises(AnalyzeError):
        analyze(l, store, FakeRunner([AnalyzeError("x")]), FakeHttp(), Settings(), "new")
    assert status(store) == "pending_analysis"


def test_schema_strict_and_prompt_contents(env):
    schema = Verdict.model_json_schema()
    assert schema["additionalProperties"] is False
    _, l = env
    p = build_prompt(l, [2500, 3100], "triage", ["photo_01.jpg"])
    assert "photo_01.jpg" in p and "2500, 3100" in p and "2010 Toyota Camry" in p
    assert "torque-converter" in p and "web search" not in p


def test_prompt_names_the_configured_region_only(env):
    _, l = env
    p = build_prompt(l, [], "triage", [], "Austin, TX")
    assert "Austin, TX" in p and "{region}" not in p
    assert "Pittsburgh" not in p and "Питтсбург" not in p
    default = build_prompt(l, [], "triage", [])
    assert "Pittsburgh, PA" in default


def test_analyze_passes_settings_region_to_prompt(env):
    store, l = env
    s = Settings(search=SearchSettings(region="Austin, TX"))
    r = FakeRunner([canned("skip")])
    analyze(l, store, r, FakeHttp(), s, "new")
    assert "Austin, TX" in r.calls[0]["prompt"]


def test_download_photos_skips_failures(tmp_path):
    names = download_photos(["http://p/1", "http://bad/2", "http://p/3"], tmp_path, FakeHttp(), 8)
    assert names == ["photo_01.jpg", "photo_02.jpg"]
    assert all((tmp_path / n).read_bytes() == b"jpgdata" for n in names)
    assert download_photos(["http://p/1", "http://p/2"], tmp_path, FakeHttp(), 1) == ["photo_01.jpg"]


def script(tmp_path, body):
    p = tmp_path / "fakeclaude"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def test_runner_argv_and_parsing(tmp_path):
    out = tmp_path / "argv.json"
    photo_dir = tmp_path / "photos"
    photo_dir.mkdir()
    body = (
        'import json,os,sys\n'
        f'json.dump([sys.argv[1:], os.getcwd(), sys.stdin.read()], open("{out}", "w"))\n'
    )
    shell = (
        f"python3 -c '{body}' \"$@\"\n"
        'echo \'{"is_error":false,"structured_output":{"a":1},"modelUsage":{"m":{}}}\'\n'
    )
    r = ClaudeRunner(script(tmp_path, shell)).run(
        "PROMPT", "haiku", {"type": "object"}, photo_dir, False, 10
    )
    assert r.output == {"a": 1} and r.model_usage == {"m": {}}
    argv, cwd, stdin = json.loads(out.read_text())
    assert cwd == str(photo_dir.resolve()) and stdin == ""
    assert argv == [
        "-p", "--model", "haiku", "--setting-sources", "project",
        "--strict-mcp-config", "--no-session-persistence", "--allowedTools", "Read",
        "--output-format", "json", "--json-schema", '{"type": "object"}', "PROMPT",
    ]


@pytest.mark.parametrize("body", [
    "exit 1\n",
    "echo not json\n",
    'echo \'{"is_error":true,"structured_output":{"a":1}}\'\n',
    'echo \'{"is_error":false}\'\n',
])
def test_runner_error_paths(tmp_path, body):
    with pytest.raises(AnalyzeError) as e:
        ClaudeRunner(script(tmp_path, body)).run("SECRETPROMPT", "haiku", {}, tmp_path, True, 10)
    assert "SECRETPROMPT" not in str(e.value)


def test_runner_timeout_kills_process_group(tmp_path):
    # The child would touch the marker after 30 s if only the shell were killed:
    # then the open stdout pipe also makes run() block until the child exits on
    # its own and the marker exists. A group kill leaves no marker and returns at once.
    survived = tmp_path / "survived"
    body = f'( sleep 30; touch "{survived}" ) &\nwait\n'
    with pytest.raises(AnalyzeError):
        ClaudeRunner(script(tmp_path, body)).run("p", "haiku", {}, tmp_path, False, 0.2)
    assert not survived.exists()
