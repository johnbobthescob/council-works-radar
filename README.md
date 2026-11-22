# WA Council Works Radar

Pre-tender capital-works intelligence for WA civil contractors. The radar reads public council documents (budgets, capital works programs, project plans, council agendas and minutes), extracts projects with their values, stages and trades, and links each fact back to its source. It publishes facts, short quotes (300 characters or less) and source links only, never full council documents. Output is a weekly digest (HTML and PDF) and a filterable local web UI.

## Status

- Milestones M0 to M18 are done (see PLAN.md §10), including the stretch items: the Black Spot cross-check (M17, off by default) and the sitemap fallback plus extracted program totals (M18).
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

Individual stages are available as subcommands: `list`, `extract` (fetch + extract), `link`, `digest`, `export` (alias `ui`), `trades-report`, `prune`.

Tests:

```bash
pytest -q                            # offline, with a network guard
RADAR_LIVE=1 pytest -q -m live       # optional live checks
```

## Sample output

The sample from a live baseline run on 2026-11-22 (ISO week 2026-W47) is committed in `out/`.

- `out/digest-2026-W47.html` and `out/digest-2026-W47.pdf` (14 pages, WeasyPrint). Baseline digest with the top projects per trade by value, council by trade counts, a needs-review list, a source health table and an attribution footer. Every item links to the council source with page number and retrieval date.
- `out/ui/` (`index.html`, `app.js`, `style.css`, `data.js` at about 425 KB). The header shows the extracted program totals per council. Filter by council, trade (including implied trades), stage, financial year, value, confidence, recent change, flags and free text. Rows expand to show all sources and change history. CSV download. Filter state is kept in the URL hash.
- `out/run-summary-2026-W47.json`. Per council: documents listed and selected, outcome per document, skipped documents with reasons, mentions, projects, events, errors and paused hosts.

Results from that run (run `20261122T141724-845086`):

| Council | Documents used (mentions extracted) | Projects |
|---|---|---|
| Joondalup | Budget 2026-27 PDF (202); 27 Oct 2026 Council minutes (10). 86 documents listed | 212 |
| Melville | Capital Works Program 2026-27 HTML page (23); 20 Oct 2026 OMC minutes (0, see known issues) | 23 |
| Cockburn | Project Plans 2026-2027 PDF (49); 13 Oct 2026 OCM minutes (5) | 53 |

Total: 288 projects, all recorded as `new` events (this was the baseline run). The trades covered include ROAD_RESURF (99), ROAD_CONST (55), INTERSECTION (43), FOOTPATH (18), DRAINAGE (9), STREETSCAPE (9), CARPARK (7) and BRIDGE (4).

Skipped by policy, and recorded in the run summary:

- Joondalup 17 Nov 2026 agenda (57 MB), Cockburn 10 Nov 2026 minutes (44 MB) and Melville 17 Nov 2026 minutes (79 MB) are over the 40 MB threshold. Use `--allow-large` to include them.
- Meetings older than 60 days, and more than 2 meeting documents per council per run.
- Melville's meeting listing came from the sitemap fallback: its ksearch endpoint returned HTTP 500 (see known issues). The run summary records this as an adapter error, so Melville shows as `partial`.

## Known issues

- **Melville and Rockingham ksearch listings return HTTP 500.** Their request bodies were assumed from Stirling's (documented in `config/councils.yaml`), and the assumption is wrong. Stirling's ksearch works. Melville is covered by the sitemap fallback; Rockingham's fallback pattern is unverified.
- **The committed sample predates Melville's item pattern.** `agenda_items` now uses Melville's item numbering (`E26/106`). Re-extracting the 20 Oct 2026 minutes gives 1 civil item, a $78,500 budget amendment, which is not in the committed sample.
- **No real Black Spot tags yet.** With `--enable blackspot`, the 2026-27 Main Roads list gives 12 rows for the target councils. None of them matches a council project in the current data. The matcher only links on an intersection pair or on a single unambiguous road, and never guesses.
- **Program totals are sums of extracted values,** not the councils' statutory budget totals. Statutory budget PDFs are not fetched.
- `record-fixture` (PLAN §11.2) is not implemented. To re-record a listing fixture, save the adapter response by hand.

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
