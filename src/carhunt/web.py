"""Self-contained HTML journal of every listing (inline CSS + JS, data as JSON)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, tzinfo


def _local(value: str | None, tz: tzinfo) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _analysis(row) -> dict:
    return {
        "model": row["model"],
        "duration_s": row["duration_s"],
        "verdict": row["verdict"],
        "reliability": row["reliability"],
        "value": row["value"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "json": _loads(row["json"]),
    }


def collect(store, now: datetime, tz: tzinfo = UTC) -> dict:
    db = store.db
    history: dict[int, list[dict]] = {}
    for p in db.execute("SELECT * FROM price_history ORDER BY id"):
        history.setdefault(p["listing_id"], []).append(
            {"price": p["price"], "seen_at": p["seen_at"]}
        )
    stages: dict[int, dict[str, dict]] = {}
    latest: dict[int, dict] = {}
    for a in db.execute("SELECT * FROM analyses ORDER BY id"):
        stages.setdefault(a["listing_id"], {})[a["stage"]] = _analysis(a)
        if a["verdict"] is not None:
            latest[a["listing_id"]] = _analysis(a)
    notified = {r["listing_id"] for r in db.execute("SELECT listing_id FROM notifications")}

    listings = []
    for r in db.execute("SELECT * FROM listings ORDER BY detected_at DESC, id DESC"):
        photos = _loads(r["photos"]) or []
        lat = latest.get(r["id"])
        listings.append(
            {
                "id": r["id"],
                "url": r["url"],
                "photo": photos[0] if photos and isinstance(photos[0], str) else None,
                "year": r["year"],
                "make": r["make"],
                "model": r["model"],
                "trim": r["trim"],
                "title": r["title"],
                "price": r["price"],
                "price_history": history.get(r["id"], []),
                "mileage": r["mileage"],
                "transmission": r["transmission"],
                "title_status": r["title_status"],
                "location": r["location"],
                "seller_type": r["seller_type"],
                "detected_at": r["detected_at"],
                "detected_local": _local(r["detected_at"], tz),
                "published_at": r["published_at"],
                "published_local": _local(r["published_at"], tz),
                "verdict": lat["verdict"] if lat else None,
                "reliability": lat["reliability"] if lat else None,
                "value": lat["value"] if lat else None,
                "notified": r["id"] in notified,
                "analyses": stages.get(r["id"], {}),
            }
        )
    run = next((r for r in store.recent_runs(10) if r["finished_at"]), None)
    meta = {
        "found": len(listings),
        "analyzed": len(latest),
        "notified": len(notified),
        "rendered_local": _local(now.isoformat(), tz),
        "timezone": str(tz),
        "run": None
        if run is None
        else {
            "at": _local(run["finished_at"] or run["started_at"], tz),
            "cards": run["cards"],
            "new": run["new"],
            "errors": run["errors"],
        },
    }
    return {"meta": meta, "listings": listings}


def _embed(data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False)
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                    (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(ch, esc)
    return text


def render_page(store, now: datetime, tz: tzinfo = UTC) -> str:
    return _TEMPLATE.replace("__DATA__", _embed(collect(store, now, tz)))


_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Журнал объявлений</title>
<style>
:root{--bg:#f6f7f9;--fg:#1c1e21;--mut:#65676b;--card:#fff;--bd:#dddfe2;--ok:#1a7f37;--warn:#9a6700;--bad:#b42318;--acc:#0b57d0}
@media (prefers-color-scheme:dark){:root{--bg:#18191a;--fg:#e4e6eb;--mut:#9a9da3;--card:#242526;--bd:#3a3b3c;--ok:#4ac26b;--warn:#e3b341;--bad:#ff7b72;--acc:#7ab7ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:900px;margin:0 auto;padding:12px 16px}
#bar{color:var(--mut);font-size:13px;margin-bottom:10px;display:flex;flex-wrap:wrap;gap:4px 12px}
#bar span{white-space:nowrap}
#filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}
#filters input,#filters select{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:6px 8px;font:inherit}
#filters input[type=number]{width:90px}
#q{flex:1 1 160px;min-width:0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px;margin-bottom:12px;display:flex;gap:12px}
.card img{width:140px;height:105px;object-fit:cover;border-radius:6px;flex:none}
.body{min-width:0;flex:1}
.name{font-weight:700;font-size:16px;overflow-wrap:anywhere}
.meta{color:var(--mut);font-size:13px;overflow-wrap:anywhere}
.badge{display:inline-block;border-radius:999px;padding:1px 9px;font-size:12px;font-weight:600;border:1px solid currentColor;margin:4px 6px 4px 0}
.worth_seeing{color:var(--ok)}.doubtful{color:var(--warn)}.skip{color:var(--bad)}.none,.sent{color:var(--mut)}
.drop{color:var(--ok);font-size:13px}
a{color:var(--acc)}
details{margin-top:6px}
summary{cursor:pointer;color:var(--acc)}
.sec{margin:8px 0 2px;font-weight:600}
ul{margin:2px 0;padding-left:20px}
li,p{overflow-wrap:anywhere}
.empty{color:var(--mut);text-align:center;padding:24px}
@media (max-width:520px){.card{flex-direction:column}.card img{width:100%;height:180px}}
</style>
</head>
<body>
<main>
<div id="bar"></div>
<div id="filters">
<select id="verdict"><option value="">Все вердикты</option><option value="worth_seeing">Стоит смотреть</option><option value="doubtful">Сомнительно</option><option value="skip">Пропустить</option></select>
<label><input type="checkbox" id="sent"> только отправленные</label>
<input type="number" id="minrel" placeholder="Надёжность ≥" min="0" max="100">
<input type="number" id="minval" placeholder="Выгодность ≥" min="0" max="100">
<input type="search" id="q" placeholder="Поиск: авто, город">
<select id="sort"><option value="new">Сначала новые</option><option value="best">Лучшие (надёжность+выгодность)</option><option value="cheap">Сначала дешёвые</option></select>
</div>
<div id="list"></div>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
"use strict";
var D = JSON.parse(document.getElementById("data").textContent);
var LABEL = {worth_seeing: "Стоит смотреть", doubtful: "Сомнительно", skip: "Пропустить"};
function el(tag, text, cls) {
  var e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
function money(n) { return "$" + Number(n).toLocaleString("en-US"); }
function carName(l) {
  var s = [l.year, l.make, l.model, l.trim].filter(function (x) { return x; }).join(" ");
  return s || l.title || "Без названия";
}
function dropFrom(l) {
  var h = l.price_history || [];
  if (l.price === null || h.length < 2) return null;
  var best = null;
  for (var i = 0; i < h.length - 1; i++) {
    if (h[i].price > l.price && (best === null || h[i].price > best)) best = h[i].price;
  }
  return best;
}
function bar() {
  var m = D.meta, b = document.getElementById("bar");
  b.textContent = "";
  var parts = ["Найдено: " + m.found, "Разобрано: " + m.analyzed, "Отправлено: " + m.notified];
  if (m.run) parts.push("Последний запуск: " + m.run.at + " (карточек " + m.run.cards +
    ", новых " + m.run.new + ", ошибок " + m.run.errors + ")");
  else parts.push("Запусков ещё не было");
  parts.push("Страница: " + m.rendered_local + " (" + m.timezone + ")");
  parts.forEach(function (p) { b.appendChild(el("span", p)); });
}
function addList(box, title, items) {
  if (!items || !items.length) return;
  box.appendChild(el("div", title, "sec"));
  var ul = el("ul");
  items.forEach(function (x) { ul.appendChild(el("li", x)); });
  box.appendChild(ul);
}
function addText(box, title, text) {
  if (text === null || text === undefined || text === "") return;
  box.appendChild(el("div", title, "sec"));
  box.appendChild(el("p", text));
}
function analysis(l) {
  var a = l.analyses || {};
  var main = a.judge || a.triage;
  var box = el("div");
  if (!main) { box.appendChild(el("p", "Разбора пока нет")); return box; }
  var j = main.json || {};
  addText(box, "Итог", j.summary);
  addList(box, "Почему стоит", j.reasons_for);
  addList(box, "Риски", j.risks);
  addText(box, "Расходы", j.expected_costs);
  if (j.market_price) addText(box, "Рынок",
    money(j.market_price.low) + " – " + money(j.market_price.high) + ". " + (j.market_price.basis || ""));
  if (j.negotiation_target !== null && j.negotiation_target !== undefined)
    addText(box, "Торг до", money(j.negotiation_target));
  addList(box, "Вопросы продавцу", j.questions_for_seller);
  addList(box, "Неизвестно", j.facts_unknown);
  ["triage", "judge"].forEach(function (st) {
    if (a[st]) box.appendChild(el("div", st + ": модель " + (a[st].model || "?") +
      ", " + (a[st].duration_s === null ? "?" : Math.round(a[st].duration_s)) + " с", "meta"));
  });
  return box;
}
function card(l) {
  var c = el("div", null, "card");
  if (l.photo) {
    var img = el("img");
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.alt = "";
    img.addEventListener("error", function () { img.style.display = "none"; });
    img.src = l.photo;
    c.appendChild(img);
  }
  var b = el("div", null, "body");
  b.appendChild(el("div", carName(l) + (l.price !== null ? " — " + money(l.price) : ""), "name"));
  var was = dropFrom(l);
  if (was !== null) b.appendChild(el("div", "📉 было " + money(was), "drop"));
  var meta = [];
  if (l.mileage !== null) meta.push(Number(l.mileage).toLocaleString("en-US") + " mi");
  if (l.transmission) meta.push(l.transmission);
  if (l.title_status) meta.push(l.title_status);
  if (l.location) meta.push(l.location);
  if (l.seller_type) meta.push(l.seller_type);
  if (l.detected_local) meta.push("найдено " + l.detected_local);
  if (l.published_local) meta.push("опубликовано " + l.published_local);
  b.appendChild(el("div", meta.join(" · "), "meta"));
  var v = el("div");
  v.appendChild(el("span", l.verdict ? LABEL[l.verdict] || l.verdict : "Не разобрано",
    "badge " + (l.verdict || "none")));
  if (l.reliability !== null) v.appendChild(el("span",
    "Надёжность " + l.reliability + " · Выгодность " + l.value, "meta"));
  b.appendChild(v);
  if (l.notified) b.appendChild(el("span", "✈ отправлено", "badge sent"));
  var p = el("div");
  var a = el("a", "Открыть на Facebook");
  a.href = /^https?:\\/\\//.test(l.url) ? l.url : "#";
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  p.appendChild(a);
  b.appendChild(p);
  var d = el("details");
  d.appendChild(el("summary", "Полный разбор"));
  d.appendChild(analysis(l));
  b.appendChild(d);
  c.appendChild(b);
  return c;
}
function score(l) { return (l.reliability || 0) + (l.value || 0); }
function render() {
  var verdict = document.getElementById("verdict").value;
  var sent = document.getElementById("sent").checked;
  var minrel = parseFloat(document.getElementById("minrel").value);
  var minval = parseFloat(document.getElementById("minval").value);
  var q = document.getElementById("q").value.trim().toLowerCase();
  var sort = document.getElementById("sort").value;
  var rows = D.listings.filter(function (l) {
    if (verdict && l.verdict !== verdict) return false;
    if (sent && !l.notified) return false;
    if (!isNaN(minrel) && !(l.reliability >= minrel)) return false;
    if (!isNaN(minval) && !(l.value >= minval)) return false;
    if (q && (carName(l) + " " + (l.title || "") + " " + (l.location || "")).toLowerCase().indexOf(q) < 0) return false;
    return true;
  });
  rows.sort(function (x, y) {
    if (sort === "best") return score(y) - score(x) || y.id - x.id;
    if (sort === "cheap") {
      var px = x.price === null ? Infinity : x.price, py = y.price === null ? Infinity : y.price;
      return px - py || y.id - x.id;
    }
    return (y.detected_at < x.detected_at ? -1 : y.detected_at > x.detected_at ? 1 : 0) || y.id - x.id;
  });
  var list = document.getElementById("list");
  list.textContent = "";
  if (!rows.length) list.appendChild(el("div", "Ничего не найдено", "empty"));
  rows.forEach(function (l) { list.appendChild(card(l)); });
}
["verdict", "sent", "minrel", "minval", "q", "sort"].forEach(function (id) {
  document.getElementById(id).addEventListener("input", render);
});
bar();
render();
</script>
</body>
</html>
"""
