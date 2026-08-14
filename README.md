# MentatNews

A self-hosted RSS reader that builds you a **front page** — not an endless river.

Feedly moved on to enterprise threat intelligence. This is the personal reader it
stopped making: your sources, in your order, on your server, with an editor that
decides what actually matters today.

> The interface and code comments are in Spanish. Everything else translates
> cleanly — the strings live in `static/index.html` and `static/app.js`.

---

## What makes it different

**The Front Page.** For each topic folder, Claude reads what you haven't read and
lays out a front page: one lead story, two or three secondary ones, and the rest as
briefs. It **only selects and ranks real articles** — every choice is validated
against the IDs it was given, so it cannot invent a headline. Each pick comes with
one sentence on why it earned that spot.

Front pages are per topic, never mixed, and are rebuilt in the background before
they go stale, so opening one takes milliseconds rather than waiting on a model.

**Noise removal.** Feeds bury metadata in the summary — `Article URL: … Points: 7 #
Comments: 0`. On the author's install that was 39% of every list row. `limpieza.py`
strips it, along with WordPress's *"The post X appeared first on Y"*, Reddit's
`submitted by /u/…`, tracking pixels and summaries that merely repeat the headline.

**Deduplication.** The same story from seven outlets collapses into one row marked
`+6`. Pure text similarity, no AI call: 3,000 articles in 0.28 s. Two articles from
the *same* feed never merge — that's an update, not a duplicate.

**Full text in the pane.** Most feeds send a teaser. When one does, the reader
fetches and extracts the whole article into the reading pane, so you never leave for
a tab full of ads.

**Reading that respects you.** The layout breathes: the list uses the full width
until you open something, then it steps aside and the article sits in a 68-character
column. Three densities, day separators, `⌘K` palette, and Feedly-style keyboard
shortcuts that do **not** hijack `Ctrl+R` or `Cmd+S`. Font scale from 90% to 145%
recomposes the whole page. Works on a phone.

**An API for your own software.** Create a key in Settings → API and read your feed
programmatically: `GET /api/articles?unread=1` with an `X-API-Key` header.

---

## ⚠️ Read this before exposing it

**MentatNews has no login of its own.** It is designed to sit behind a reverse proxy
that handles authentication. If you expose it directly to the internet, anyone can
read, add and delete your feeds.

Two supported setups:

1. **Bind to localhost** (the default) and reach it over SSH or a VPN like Tailscale.
2. **Put it behind a proxy** that authenticates, and forward only `/api/*` requests
   carrying a valid `X-API-Key` (the app validates those itself). There's a Caddy
   example in `deploy/Caddyfile.example`.

Outbound requests are protected against SSRF: every redirect hop is resolved and
anything landing on a private or loopback address is refused (`netguard.py`). That
guards your internal network — it is not a substitute for authentication.

---

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/fcarral/mentatnews.git
cd mentatnews
pip install -r requirements.txt
python3 main.py            # http://127.0.0.1:9160
```

The database is created on first run under `data/`.

### Optional: the Front Page and feed suggestions

Both call the Claude API. Without a key everything else works; the Front Page falls
back to reverse-chronological order.

```bash
cp .env.example .env       # then edit it
export ANTHROPIC_API_KEY=sk-ant-...
export MENTATNEWS_PERFIL="a developer who follows AI and markets"   # optional
```

`MENTATNEWS_PERFIL` describes the reader so the editor knows what to highlight.

### Getting started

Open the app and hit **+ Fuente**. Three ways in:

- Paste any site URL — it discovers the feed for you.
- Import your OPML from Feedly, Inoreader or wherever you're leaving.
- Pick from the bundled catalog: **81 verified live feeds** across 9 topics
  (`static/catalog.json`). Each one was checked to return a real feed *with recent
  items* — a feed that answers 200 can still have been dead since 2022.

---

## How it fits together

| File | What it does |
|---|---|
| `main.py` | FastAPI app, scheduler, article endpoints |
| `db.py` | SQLite schema, migrations, counters kept by triggers |
| `fetcher.py` | Feed download and parsing, with an allowlist HTML sanitiser |
| `extractor.py` | Full-text extraction (trafilatura, with an lxml fallback) |
| `limpieza.py` | Noise removal and featured-image detection |
| `dedup.py` | Groups the same story across outlets (inverted index + union-find) |
| `netguard.py` | SSRF protection: validates every redirect hop |
| `portada.py` | The Front Page editor prompt and its validation |
| `endpoints_portada.py` | Front Page cache, background renewal and endpoints |
| `opml.py` | OPML import/export and feed autodiscovery |
| `ai.py` | Natural-language feed suggestions |
| `static/` | The whole interface — vanilla JS, no build step, no CDN |

No framework, no bundler, no node_modules. Self-hosted fonts. It runs on a small VPS
and the database is one SQLite file you can copy.

### Performance

Measured on the author's install with 5,200 articles across 84 feeds: any list page
in 0.03 s, `/api/stats` in 0.01 s, switching Front Page topics in 76 ms. Sort order
is a materialised column with covering indexes, so nothing sorts in memory.

---

## Deployment

`deploy/` has a systemd unit and a Caddy config to copy. The short version: run it
with a process manager, keep it on localhost, and put your own auth in front.

## Contributing

Issues and pull requests welcome. The interface strings are in Spanish; a clean
i18n extraction would be a genuinely useful first contribution.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it, whatever helps.

The MIT grant covers the code. The MENTAT name and logo are the author's marks and
aren't part of it — see [TRADEMARK.md](TRADEMARK.md). Fork it, rename it, ship it.
