# WA Council Works Radar

Pre-tender capital-works intelligence for WA civil contractors. The radar reads public council documents (budgets, capital works programs, project plans, council agendas and minutes), extracts projects with their values, stages and trades, and links each fact back to its source. It publishes facts, short quotes (300 characters or less) and source links only, never full council documents. Output is a weekly digest (HTML and PDF) and a filterable local web UI.

## Status

- Milestones M0 to M16 are done (see PLAN.md §10). Stretch items are listed there too.
- Six councils are configured: Joondalup, Wanneroo, Stirling, Melville, Cockburn, Rockingham.
- The live demo used Joondalup, Melville and Cockburn.

## Install

Requires Python 3.11 or later (3.13 is used in development).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,pdf]'          # or: pip install -r requirements.txt
```

WeasyPrint (the `pdf` extra) is optional. Without it, the PDF digest falls back to PyMuPDF Story.

## How to run

Check the robots.txt decisions first:

```bash
python -m radar robots-check
```

Run the full pipeline for the demo councils:

```bash
python -m radar run --councils joondalup,melville,cockburn
```

`run` goes through list, select, fetch, extract, link, changes, digest and UI. It takes a few minutes because each host is rate-limited to one request every 5 seconds or more.

Flags:

- `--offline`: no network. Replays stored documents from `.cache/raw`.
- `--allow-large`: allow documents over the 40 MB threshold (the hard cap is 80 MB).
- `--no-pdf`: write the digest as HTML only.
- `--enable blackspot`: optional Black Spot cross-check. Disabled by default; its licence is unverified.
- `--max-meeting-docs N`: override the per-council meeting document cap (default 2).

View the UI:

```bash
python -m radar serve                # then open http://127.0.0.1:8000/ui/
```

You can also open `out/ui/index.html` directly from a `file://` URL.

Individual stages are available as subcommands: `list`, `extract`, `link`, `digest`, `export`, `trades-report`, `prune`.

Tests:

```bash
pytest -q                            # offline, with a network guard
RADAR_LIVE=1 pytest -q -m live       # optional live checks
```

## Sample output

The sample from a live baseline run on 2026-11-22 (ISO week 2026-W47) is committed in `out/`.

- `out/digest-2026-W47.html` and `out/digest-2026-W47.pdf` (14 pages, WeasyPrint). Baseline digest with the top projects per trade by value, council by trade counts, a needs-review list, a source health table and an attribution footer. Every item links to the council source with page number and retrieval date.
- `out/ui/` (`index.html`, `app.js`, `style.css`, `data.js` at about 450 KB). Filter by council, trade (including implied trades), stage, financial year, value, confidence, recent change, flags and free text. Rows expand to show all sources and change history. CSV download. Filter state is kept in the URL hash.
- `out/run-summary-2026-W47.json`. Per council: documents listed and selected, outcome per document, skipped documents with reasons, mentions, projects, events, errors and paused hosts.

Results from that run:

| Council | Documents used (mentions extracted) | Projects |
|---|---|---|
| Joondalup | Budget 2026-27 PDF (202); 27 Oct 2026 Council minutes (18). 86 documents listed | 218 |
| Melville | Capital Works Program 2026-27 HTML page | 23 |
| Cockburn | Project Plans 2026-2027 PDF (49); 13 Oct 2026 OCM minutes (10) | 58 |

Total: 299 projects, all recorded as `new` events (this was the baseline run).

Skipped by policy, and recorded in the run summary:

- Joondalup 17 Nov 2026 agenda (57 MB) and Cockburn 10 Nov 2026 minutes (44 MB). Both exceed the 40 MB threshold; use `--allow-large` to include them.
- Meetings older than 60 days.
- More than 2 meeting documents per council per run.

Note on the live run: in the first attempt the Cockburn Project Plans PDF timed out (the server stalled for more than 60 seconds before sending headers). The fetcher now retries once after 10 seconds, and the committed run fetched the file successfully.

## Compliance

- Honest User-Agent that identifies the bot as a research bot. No browser user agent.
- robots.txt is checked for every host, and again after redirects.
- At least 5 seconds between requests to the same host. Conditional GET (ETag and Last-Modified).
- 80 MB hard size cap. Documents over 40 MB are opt-in.
- A host is paused for 24 hours on a 403, a 429 or a challenge page.
- A compliance blocklist is never contacted: Canning CDN, Swan `/v1/`, Tenders WA, Gosnells and search.joondalup.
- No council documents are committed. Only extracted facts, quotes of 300 characters or less, and links are stored or published.
- Licensing: Melville and Cockburn publish under non-commercial terms, and Rockingham requires permission. This is a private pilot.

## Layout

```
config/           councils.yaml (sources, blocklist), trades.yaml (trade taxonomy)
radar/            package: cli.py, pipeline.py, dedupe.py, digest.py, export.py,
                  settings.py, blackspot.py
  adapters/       council listing adapters
  extract/        document extractors
  templates/      digest HTML templates (Jinja2)
  ui/             static web UI (index.html, app.js, style.css)
tests/            offline tests and fixtures (tests/fixtures/)
data/             gitignored: SQLite database, fetch log, listings
.cache/           gitignored: raw downloads, robots cache, extracted text
out/              committed sample output (digest, UI, run summary)
```

## Pointers

- [PLAN.md](PLAN.md): design, compliance rules (§9), milestones (§10), demo (§12).
- [RESEARCH.md](RESEARCH.md): source research for each council.
