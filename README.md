# carhunt

Used-car alerts from Facebook Marketplace, rated by Claude.

Every 15 minutes carhunt reads the newest car listings of one Facebook Marketplace city **without logging in**,
asks Claude to rate each new one from its text and photos, and sends only the good ones to your Telegram. Every
listing, verdict and price change is kept in a local SQLite journal, and a self-contained HTML page shows it all.

- **No Anthropic API key.** Claude runs through the [Claude Code](https://claude.com/claude-code) CLI
  (`claude -p`) on your own Claude subscription: a fast model (`haiku`) triages every listing, a strong one
  (`opus`, with web search) re-checks the candidates.
- **No Facebook account.** Public Marketplace pages are fetched with plain HTTP at a polite pace.
- **No database server.** One SQLite file, `data/carhunt.db`, created on the first run.

## What you get in Telegram

One alert per listing that passes your bar, with photo and an «Open listing» button:

```
2005 Mercury Grand Marquis GS Sedan 4D — $1200
95 923 миль · AUTOMATIC · Pittsburgh, PA
Надёжность 76/100 · Выгодность 80/100
<summary: engine/gearbox pairing, what the photos show, price vs market>
Почему:
• Атмосферный V8 4.6 без турбины…
Риски:
• …
Рынок: $2300–3000 (KBB private party)
Торг: до $1000
Найдено 10:42 (опубл. 10:31)
```

The text is Russian by default; change the last line of `prompts/analyze.md` to get another language. A daily
summary (found / analyzed / sent, detection latency, AI calls) arrives at 09:00 in `report.timezone`, and an alert fires
when Facebook returns nothing two runs in a row.

An alert is sent only when the verdict is «worth seeing» **and** reliability ≥ `notify.min_reliability` **and**
value ≥ `notify.min_value`. A price drop of at least $200 / 5 % triggers a re-rating and, if it now passes, a new
alert.

## Requirements

- Linux with systemd (user timers) — macOS works for manual runs (`carhunt poll`) without the timers.
- Python 3.12+.
- [Claude Code](https://claude.com/claude-code) installed and logged in with your subscription (`claude` on `PATH`).
- A Telegram bot (free, from [@BotFather](https://t.me/BotFather)).

## Install

```bash
git clone https://github.com/formobr/carhunt.git
cd carhunt
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

1. **Telegram.** Create a bot with @BotFather, put its token into `.env` as `TELEGRAM_BOT_TOKEN`. Send `/start`
   to the bot, then get your chat id:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[-0-9]*'
   ```
   and put it into `.env` as `TELEGRAM_CHAT_ID`.
2. **Your search.** Edit `config.yaml` (city, price range) — or override any key from `.env`, see below.
3. **Your taste.** `prompts/criteria.md` is what the AI looks for (engine/gearbox preferences, AWD, red flags).
   Rewrite it in plain words for your needs.
4. **Try one run by hand:**
   ```bash
   .venv/bin/carhunt poll
   ```
   The first run rates the backlog (up to `poll.max_details_per_run` listings, ~1 min each).
5. **Run it every 15 minutes:**
   ```bash
   bash deploy/install.sh
   ```
   Stop and remove the timers with `bash deploy/uninstall.sh`.

## Settings

`config.yaml` holds the defaults. Any key can be overridden in `.env` (or the environment) as
`CARHUNT__<SECTION>__<KEY>=<value>`; the value is parsed as YAML, so numbers, `true/false` and `[lists]` work.
An unknown section or key is an error, not a silent no-op.

| setting | default | meaning |
|---|---|---|
| `search.source_city` | `pittsburgh` | Facebook Marketplace city slug (the part after `/marketplace/` in the URL) |
| `search.min_price` / `max_price` | 300 / 6000 | price range, USD |
| `search.region` | `Pittsburgh, PA` | the area named in the AI prompts |
| `report.timezone` | `America/New_York` | card times and the daily summary (re-run `deploy/install.sh` after changing it) |
| `poll.interval_min` | 15 | timer interval (re-run `deploy/install.sh` after changing it) |
| `poll.max_details_per_run` | 30 | listings opened and rated per run |
| `poll.detail_delay_s` | `[2, 5]` | random pause between listing pages, seconds |
| `notify.min_reliability` / `min_value` | 60 / 60 | alert bar, 0–100 |
| `notify.price_drop_min_usd` / `_pct` | 200 / 5 | what counts as a price drop |
| `ai.triage_model` / `judge_model` | `haiku` / `opus` | Claude models for the two stages |
| `ai.max_photos` | 8 | photos the AI reads per listing |
| `ai.timeout_s` | 240 | per-call deadline |
| `health.fail_runs_before_alert` | 2 | bad runs in a row before the «Facebook returns nothing» alert |

Example: `CARHUNT__SEARCH__MAX_PRICE=8000` and `CARHUNT__NOTIFY__MIN_VALUE=70`.

## Another city

Everything city-specific is in settings (or `.env` overrides):

1. `search.source_city` — open Facebook Marketplace in a browser, pick your city, and copy the part after
   `/marketplace/` from the URL (e.g. `nyc`, `chicago`, `seattle`, `austin`). Facebook searches its standard area
   around that city.
2. `search.region` — how the AI should call your area, e.g. `Austin, TX`. The AI uses it for the local market and
   climate (for example, whether AWD matters there).
3. `report.timezone` — your IANA time zone, e.g. `America/Chicago`, `America/Los_Angeles`; then re-run
   `bash deploy/install.sh` so the daily summary fires at 09:00 your time.
4. `search.min_price` / `max_price` — your budget.
5. `prompts/criteria.md` — what you want in a car, in plain words (it is the buyer profile the AI judges by).

```bash
# .env
CARHUNT__SEARCH__SOURCE_CITY=austin
CARHUNT__SEARCH__REGION=Austin, TX
CARHUNT__REPORT__TIMEZONE=America/Chicago
CARHUNT__SEARCH__MAX_PRICE=8000
```

## Journal

```bash
.venv/bin/carhunt log --limit 30      # newest listings with verdict and scores
.venv/bin/carhunt show <id>           # everything about one listing, full AI answer
.venv/bin/carhunt stats --days 7      # counts, latency, AI usage
.venv/bin/carhunt web --out data/web/index.html   # the HTML journal page
```
**See it in the browser (local, no server needed).** After every run the timer rebuilds the page at
`data/web/index.html` (one self-contained HTML file: filters, sorting, photos, full analysis). Open it:

```bash
python3 -m http.server 8000 --bind 127.0.0.1 -d data/web
```

then go to http://localhost:8000 — reload to see new runs. (Opening `data/web/index.html` directly as a file
works too.)

**Optional: your own web server.** To have the page on a domain after every run, set `CARHUNT_WEB_SSH` (an ssh host
from your `~/.ssh/config`) and `CARHUNT_WEB_DIR` (a directory it serves) in `.env`, and protect that directory with
basic auth, e.g. for nginx:

```nginx
location ^~ /carhunt/ {
    auth_basic "carhunt";
    auth_basic_user_file /etc/nginx/.htpasswd-carhunt;
    try_files $uri $uri/index.html =404;
}
```

Leave `CARHUNT_WEB_SSH` empty (the default) and the page is only written locally.

## Costs and limits

- Each listing costs one `haiku` call; candidates add one `opus` call. That is billed against your Claude
  subscription's usage limits, not money per call. Lower `poll.max_details_per_run` or raise the bar to use less.
- Facebook may change its page format or start asking for login; carhunt then sends a «returns nothing» alert.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

Tests never touch the network or `claude`: HTTP, the Claude runner and Telegram are injected and faked, and the
parsers are tested against saved pages in `tests/fixtures/` (personal data replaced with test values).

Use it for your own search, at a polite pace, and in line with the terms of the sites you read.
