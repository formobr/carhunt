"""Telegram delivery: credentials, card formatting, and the notifier."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from typing import Any

import httpx

from carhunt.analyze import Verdict
from carhunt.models import Listing

API = "https://api.telegram.org"
TEXT_LIMIT = 4096

_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")


class NotifyError(Exception):
    pass


class NotifyUnavailable(NotifyError):
    pass


def redact(text: str) -> str:
    return _TOKEN_RE.sub("bot<token>", text)


def telegram_creds(env: dict[str, str] | None = None) -> tuple[str, str]:
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        return token, chat
    raise NotifyUnavailable("telegram credentials not configured")


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(value: object, n: int) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def _hhmm(dt: datetime, tz: tzinfo) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%H:%M")


def format_header(
    listing: Listing,
    verdict: Verdict,
    reason: str,
    old_price: int | None = None,
) -> str:
    """Short top block: price drop, name — price, meta, scores."""
    lines: list[str] = []
    if reason == "price_drop" and old_price is not None:
        lines.append(f"📉 Цена снижена: ${old_price} → ${listing.price}")
    name = " ".join(
        str(p) for p in (listing.year, listing.make, listing.model, listing.trim) if p
    ) or listing.title
    lines.append(f"<b>{_esc(_clip(name, 80))}</b> — ${listing.price}")
    meta = []
    if listing.mileage is not None:
        meta.append(f"{listing.mileage:,} миль".replace(",", " "))
    if listing.transmission:
        meta.append(_esc(listing.transmission))
    if listing.location:
        meta.append(_esc(listing.location))
    if meta:
        lines.append(" · ".join(meta))
    lines.append(f"Надёжность {verdict.reliability}/100 · Выгодность {verdict.value}/100")
    return "\n".join(lines)


def _bullets(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    return [title, *(f"• {_esc(i)}" for i in items)]


def format_card(
    listing: Listing,
    verdict: Verdict,
    detected_at: datetime,
    reason: str,
    old_price: int | None = None,
    tz: tzinfo = UTC,
) -> str:
    """Full alert text; nothing is clipped except the name. Times are shown in `tz`."""
    lines = [format_header(listing, verdict, reason, old_price)]
    if verdict.summary:
        lines.append(_esc(verdict.summary))
    lines += _bullets("Почему:", verdict.reasons_for)
    lines += _bullets("Риски:", verdict.risks)
    if verdict.expected_costs:
        lines.append(f"Расходы: {_esc(verdict.expected_costs)}")
    mp = verdict.market_price
    if mp:
        lines.append(f"Рынок: ${mp.low}–${mp.high} ({_esc(mp.basis)})")
    if verdict.negotiation_target is not None:
        lines.append(f"Торг: до ${verdict.negotiation_target}")
    title = "Спросить у продавца:"
    if verdict.needs_clarification_urgent:
        title = "⚡ " + title
    lines += _bullets(title, verdict.questions_for_seller)
    pub = f" (опубл. {_hhmm(listing.published_at, tz)})" if listing.published_at else ""
    lines.append(f"Найдено {_hhmm(detected_at, tz)}{pub}")
    return "\n".join(lines)


def _cut_line(line: str, limit: int) -> list[str]:
    """Last resort for one line longer than limit: cut at a space, never inside &entity;."""
    parts: list[str] = []
    while len(line) > limit:
        cut = line.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
            amp = line.rfind("&", 0, cut)
            if amp > 0 and ";" not in line[amp:cut]:
                cut = amp
        parts.append(line[:cut])
        line = line[cut:]
    parts.append(line)
    return parts


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split on line boundaries into chunks of at most `limit` chars."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        for piece in _cut_line(line, limit):
            if cur and len(cur) + 1 + len(piece) > limit:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n{piece}" if cur else piece
    chunks.append(cur)
    return chunks


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        tz: tzinfo = UTC,
    ) -> None:
        self._tz = tz
        self._token = token
        self._chat_id = chat_id
        self._client = client
        self._sleep = sleep

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{API}/bot{self._token}/{method}"
        for attempt in (0, 1):
            try:
                resp = self._client.post(url, json=payload)
            except httpx.HTTPError as exc:
                raise NotifyError(redact(f"{method} failed: {exc}")) from None
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 429:
                if attempt == 1:
                    raise NotifyError(f"{method}: rate limited (429)")
                retry = (body.get("parameters") or {}).get("retry_after", 1)
                self._sleep(float(retry))
                continue
            return body if isinstance(body, dict) else {}
        raise NotifyError(f"{method}: rate limited (429)")  # pragma: no cover

    def _message_id(self, method: str, body: dict[str, Any]) -> int:
        if not body.get("ok"):
            raise NotifyError(redact(f"{method}: {body.get('description', 'not ok')}"))
        return int(body["result"]["message_id"])

    def send_card(
        self,
        listing: Listing,
        verdict: Verdict,
        detected_at: datetime,
        reason: str,
        old_price: int | None = None,
    ) -> int:
        keyboard = {"inline_keyboard": [[{"text": "Открыть объявление", "url": listing.url}]]}
        if listing.photos:
            # The photo is decoration: its failure must not stop the text.
            try:
                self._post(
                    "sendPhoto",
                    {
                        "chat_id": self._chat_id,
                        "photo": listing.photos[0],
                        "caption": format_header(listing, verdict, reason, old_price),
                        "parse_mode": "HTML",
                    },
                )
            except NotifyError:
                pass
        chunks = split_text(format_card(listing, verdict, detected_at, reason, old_price, self._tz))
        message_id = 0
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": self._chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            if i == len(chunks) - 1:
                payload["reply_markup"] = keyboard
            message_id = self._message_id("sendMessage", self._post("sendMessage", payload))
        return message_id

    def send_text(self, text: str) -> int:
        body = self._post(
            "sendMessage", {"chat_id": self._chat_id, "text": text[:TEXT_LIMIT]}
        )
        return self._message_id("sendMessage", body)
