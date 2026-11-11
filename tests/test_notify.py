import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from carhunt.analyze import MarketPrice, Verdict
from carhunt.models import Listing
from carhunt.notify import (
    NotifyError,
    NotifyUnavailable,
    TelegramNotifier,
    format_card,
    telegram_creds,
)

TOKEN = "123456:SECRET-abc_DEF"
DETECTED = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)  # 10:30 New York


def make_listing(**kw):
    base = {
        "source": "fb",
        "source_id": "1",
        "url": "https://fb.example/item/1",
        "title": "Civic <b>&",
        "price": 4500,
        "year": 2008,
        "make": "Honda",
        "model": "Civic",
        "trim": "LX <&>",
        "mileage": 120000,
        "transmission": "auto",
        "location": "Pittsburgh, PA",
        "description": "a < b & c",
        "photos": ["https://img.example/1.jpg"],
        "published_at": datetime(2026, 11, 10, 14, 5, tzinfo=UTC),
    }
    base.update(kw)
    return Listing(**base)


def make_verdict(**kw):
    base = {
        "verdict": "worth_seeing",
        "reliability": 70,
        "value": 80,
        "summary": "s",
        "reasons_for": ["low miles <5k & clean", "second reason"],
        "risks": ["rust > bad", "timing belt"],
        "expected_costs": "$500",
        "market_price": MarketPrice(low=4000, high=5000, basis="KBB <x>"),
        "negotiation_target": 4200,
        "questions_for_seller": ["Был ли ДТП?"],
        "needs_clarification_urgent": True,
        "facts_unknown": [],
    }
    base.update(kw)
    return Verdict(**base)


def test_card_times_follow_report_timezone():
    ny, la = ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")
    args = (make_listing(), make_verdict(), DETECTED, "new")
    assert format_card(*args, tz=ny).splitlines()[-1] == "Найдено 10:30 (опубл. 09:05)"
    assert format_card(*args, tz=la).splitlines()[-1] == "Найдено 07:30 (опубл. 06:05)"


def test_notifier_card_uses_its_timezone():
    seen, handler = recorder()
    la = ZoneInfo("America/Los_Angeles")
    TelegramNotifier(TOKEN, "42", client_for(handler), tz=la).send_card(
        make_listing(photos=[]), make_verdict(), DETECTED, "new"
    )
    assert seen[0][1]["text"].splitlines()[-1] == "Найдено 07:30 (опубл. 06:05)"


def test_card_escapes_and_layout():
    card = format_card(make_listing(), make_verdict(), DETECTED, "new", tz=ZoneInfo("America/New_York"))
    assert "LX &lt;&amp;&gt;" in card
    assert "low miles &lt;5k &amp; clean" in card
    # only our own bold tags may contain raw angle brackets
    assert "<" not in card.replace("<b>", "").replace("</b>", "")
    assert ">" not in card.replace("<b>", "").replace("</b>", "")
    assert "⚡ Спросить у продавца:\n• Был ли ДТП?" in card
    assert "Надёжность 70/100 · Выгодность 80/100" in card
    assert card.splitlines()[-1] == "Найдено 10:30 (опубл. 09:05)"


def test_card_price_drop_prefix():
    card = format_card(make_listing(), make_verdict(), DETECTED, "price_drop", old_price=5200)
    assert card.splitlines()[0] == "📉 Цена снижена: $5200 → $4500"


def test_full_card_keeps_every_item_verbatim_even_when_long():
    def items(tag):
        return [f"{tag}{i} <&> " + "x" * 300 for i in range(3)]

    v = make_verdict(
        reasons_for=items("r"),
        risks=items("k"),
        questions_for_seller=items("q"),
        facts_unknown=items("u"),
        summary="sum <m>",
        expected_costs="$500 <c>",
    )
    card = format_card(make_listing(), v, DETECTED, "new")
    lines = card.splitlines()
    for group in (v.reasons_for, v.risks, v.questions_for_seller, v.facts_unknown):
        for item in group:
            esc = item.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            assert f"• {esc}" in lines
    for title in ("Почему:", "Риски:", "⚡ Спросить у продавца:", "Неизвестно:"):
        assert title in lines
    assert "sum &lt;m&gt;" in lines
    assert "Расходы: $500 &lt;c&gt;" in lines
    assert "Рынок: $4000–$5000 (KBB &lt;x&gt;)" in lines
    assert "Торг: до $4200" in lines


def test_card_omits_empty_sections():
    v = make_verdict(
        reasons_for=[], risks=[], questions_for_seller=[], facts_unknown=[],
        market_price=None, negotiation_target=None, expected_costs="", summary="",
    )
    card = format_card(make_listing(), v, DETECTED, "new")
    for title in ("Почему:", "Риски:", "Спросить", "Неизвестно:", "Рынок:", "Торг:", "Расходы:"):
        assert title not in card


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def recorder(status_by_method=None):
    seen = []

    def handler(req):
        method = req.url.path.rsplit("/", 1)[1]
        seen.append((method, json.loads(req.content)))
        if status_by_method and method in status_by_method:
            return httpx.Response(
                status_by_method[method], json={"ok": False, "description": "bad"}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(seen)}})

    return seen, handler


def test_photo_listing_sends_short_photo_then_full_text_with_keyboard():
    seen, handler = recorder()
    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    assert n.send_card(make_listing(), make_verdict(), DETECTED, "new") == 2
    assert [m for m, _ in seen] == ["sendPhoto", "sendMessage"]
    photo, text = seen[0][1], seen[1][1]
    assert photo["photo"] == "https://img.example/1.jpg"
    assert photo["parse_mode"] == "HTML"
    assert "Надёжность 70/100 · Выгодность 80/100" in photo["caption"]
    assert "reply_markup" not in photo
    assert "Почему:" not in photo["caption"]
    assert text["parse_mode"] == "HTML"
    assert "Риски:" in text["text"]
    assert text["reply_markup"] == {
        "inline_keyboard": [[{"text": "Открыть объявление", "url": "https://fb.example/item/1"}]]
    }


def test_listing_without_photo_sends_only_message():
    seen, handler = recorder()
    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    n.send_card(make_listing(photos=[]), make_verdict(), DETECTED, "new")
    assert [m for m, _ in seen] == ["sendMessage"]


def test_send_photo_rejected_still_sends_text_and_returns_its_id():
    seen, handler = recorder({"sendPhoto": 400})
    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    assert n.send_card(make_listing(), make_verdict(), DETECTED, "new") == 2
    assert [m for m, _ in seen] == ["sendPhoto", "sendMessage"]
    assert "reply_markup" in seen[1][1]


def test_long_card_is_split_on_line_boundaries_keyboard_on_last():
    risks = [f"risk{i} <&> " + "y" * 280 for i in range(30)]  # ~9000 chars
    v = make_verdict(risks=risks)
    seen, handler = recorder()
    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    assert n.send_card(make_listing(photos=[]), v, DETECTED, "new") == 3
    bodies = [b for _, b in seen]
    assert len(bodies) == 3
    for b in bodies:
        t = b["text"]
        assert len(t) <= 4096
        assert t.count("<b>") == t.count("</b>")
        stripped = t.replace("<b>", "").replace("</b>", "")
        assert "<" not in stripped and ">" not in stripped
        assert not re.search(r"&(?!amp;|lt;|gt;)", t)  # no broken entity
    assert ["reply_markup" in b for b in bodies] == [False, False, True]
    joined = "\n".join(b["text"] for b in bodies)
    assert joined == format_card(make_listing(photos=[]), v, DETECTED, "new")
    assert bodies[-1]["text"].splitlines()[-1].startswith("Найдено")


def test_single_line_over_limit_is_cut_without_breaking_entities():
    v = make_verdict(risks=["&" * 5000])
    seen, handler = recorder()
    TelegramNotifier(TOKEN, "42", client_for(handler)).send_card(
        make_listing(photos=[]), v, DETECTED, "new"
    )
    for _, b in seen:
        assert len(b["text"]) <= 4096
        assert not re.search(r"&(?!amp;|lt;|gt;)", b["text"])


def test_429_sleeps_retry_after_once_then_raises():
    sleeps = []
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}})

    n = TelegramNotifier(TOKEN, "42", client_for(handler), sleep=sleeps.append)
    with pytest.raises(NotifyError):
        n.send_text("hi")
    assert sleeps == [3.0]
    assert len(calls) == 2


def test_429_then_success():
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, json={"parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    n = TelegramNotifier(TOKEN, "42", client_for(handler), sleep=lambda s: None)
    assert n.send_text("hi") == 5


def test_token_never_leaks_in_errors():
    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    n = TelegramNotifier(TOKEN, "42", client_for(handler))
    with pytest.raises(NotifyError) as ei:
        n.send_text("hi")
    assert "SECRET" not in str(ei.value)
    assert "bot<token>" in str(ei.value)

    def handler2(req):
        return httpx.Response(401, json={"ok": False, "description": f"bad bot{TOKEN}"})

    n = TelegramNotifier(TOKEN, "42", client_for(handler2))
    with pytest.raises(NotifyError) as ei:
        n.send_text("hi")
    assert "SECRET" not in str(ei.value)


def test_send_text_truncates_to_4096():
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    TelegramNotifier(TOKEN, "42", client_for(handler)).send_text("x" * 5000)
    assert len(sent["text"]) == 4096


def test_creds_from_env_and_empty_is_unset():
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
    assert telegram_creds(env) == ("t", "c")
    env["TELEGRAM_CHAT_ID"] = ""
    with pytest.raises(NotifyUnavailable):
        telegram_creds(env)


def test_creds_ignore_owner_monty_file(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("MONTY_OWNER_TG_BOT_TOKEN=ft\nMONTY_OWNER_TG_CHAT_ID=fc\n")
    d = tmp_path / ".config" / "monty"
    d.mkdir(parents=True)
    (d / "env").write_text("MONTY_OWNER_TG_BOT_TOKEN=a\nMONTY_OWNER_TG_CHAT_ID=b\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(NotifyUnavailable):
        telegram_creds({"MONTY_OWNER_ENV_FILE": str(f)})
    with pytest.raises(NotifyUnavailable):
        telegram_creds({})
