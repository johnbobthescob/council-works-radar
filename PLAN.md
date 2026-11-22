# WA Council Works Radar: v1 Plan

**Status:** plan only. No application code exists yet.
**Inputs:** `RESEARCH.md` (all live observations retrieved 2026-11-22). Section references like "R§1.2.3" point into RESEARCH.md.
**Plan date:** 2026-11-22.

---

## 1. Goals and non-goals

### 1.1 Goals (v1)

1. **Ingest real documents** from at least 3 Perth-metro councils. The target is 6: Joondalup, Wanneroo, Stirling, Melville, Cockburn, Rockingham (R§6.1). The demo must pull live documents from at least 3 of them.
2. **Extract project facts:** project name, location (road / from / to / suburb / ward), work type, stated value and value basis, dates, funding references, and stage. Every fact carries `doc_id`, `source_url`, page, and `retrieved_at`.
3. **Dedupe** mentions into projects across documents, and **track changes** (new project, value changed, stage advanced, deferred, source added).
4. **Match projects to trades with rules** (R§5.5 taxonomy). Each label records which keyword or code prefix fired. No LLM in v1's critical path.
5. **Weekly digest** as HTML in `out/`, plus PDF via WeasyPrint (PyMuPDF `Story` as fallback). It includes source links, uncertainty flags, and council copyright attribution.
6. **Local, filterable web UI:** static HTML + vanilla JS over a generated JSON data file, served with `python -m http.server`.
7. **Compliance enforced in code:** robots.txt checks, per-host rate limiting, honest User-Agent, conditional GET, size caps, fetch log.

### 1.2 Non-goals (v1)

- **Tenders WA**: not fetched at all (robots disallows everything but the homepage; terms forbid commercial reuse, R§2.1).
- **Council tender portals** (TenderLink, eTenderBox, SL360, Rock Port): out of scope.
- **Blocked or deferred councils:** Canning (CDN `Disallow: /`), Swan (`/v1/` disallowed), Gosnells (Akamai 403), Armadale (JS-only). Fremantle stays in reserve. Perth is deferred until its meetings page is confirmed fresh (R§1.2.7). Perth's *budget PDF* may be added later as a stretch.
- **Full agenda NLP.** v1 only segments agenda/minutes items whose titles match civil keywords. The rest of each document is ignored.
- **OCR.** Detect image-only pages and flag them (`ocr_needed`). Tesseract runs only behind an opt-in flag.
- **LLM classification/summaries.** There is an interface hook (§6.4), but no model is used in v1.
- **Users, accounts, email delivery, hosting, scheduling infrastructure.** v1 is a CLI you run weekly by hand (or cron).
- **Gazetteer / geocoding.** Suburb lists only. No maps.
- **Commercial launch.** v1 is a private pilot (R§3.2): facts and links only, no republished documents.

---

## 2. Architecture

### 2.1 Components

```
            +-------------------------- config/ (councils.yaml, trades.yaml) ---------------------+
            |                                                                                      |
 [1] Listing adapters  ->  [2] Polite fetcher  ->  [3] Text layer  ->  [4] Extractors  ->  [5] Normaliser
   (per council)          (robots, rate limit,     (PyMuPDF words,     (per doc template)    (money, roads,
   produce DocRef[]        cond. GET, size cap,     HTML->text,                                suburbs, FY)
                           fetch log, .cache/)      page texts)                                    |
                                                                                                   v
 [9] UI export  <-  [8] Digest builder  <-  [7] Change tracker  <-  [6] Dedupe/linker  <-  Trade matcher (rules)
   out/ui/data.js     out/digest-*.html/pdf    change_event rows      project rows            labels + evidence
                                   \_____________ all state in data/radar.sqlite ____________/
```

1. **Listing adapters** (`radar/adapters/`): one module per listing flavour, each yielding `DocRef` records (url, title, doc_type, meeting_type, meeting_date, financial_year, listing_url). No document bytes are fetched here.
2. **Polite fetcher** (`radar/fetch.py`): the only module allowed to touch the network. It enforces robots, rate limits, User-Agent, conditional GET, HEAD-before-GET for large files, and size caps. It writes the fetch log and the `.cache/` files.
3. **Text layer** (`radar/text.py`): PDF → per-page text plus word boxes (PyMuPDF). HTML → cleaned text blocks (BeautifulSoup + lxml). `pdftotext -layout` is a fallback. Image-only pages (<50 chars) are flagged.
4. **Extractors** (`radar/extract/`): one per document template (Joondalup budget schedule, Melville CWP HTML, Cockburn project plans, agenda/minutes items, Black Spot list). Each emits `project_mention` rows with provenance.
5. **Normaliser** (`radar/normalise.py`): money parsing, road-name normalisation and abbreviation expansion, direction qualifiers, suburb lookup, financial-year parsing, GST basis.
6. **Trade matcher** (`radar/trades.py`): rules from `config/trades.yaml`. Positive keywords, negative keywords, and code-prefix priors.
7. **Dedupe/linker** (`radar/dedupe.py`): assigns mentions to projects with the key ladder from R§5.4.
8. **Change tracker** (`radar/changes.py`): compares each project's state before and after a run and emits `change_event` rows.
9. **Digest builder** (`radar/digest.py`): Jinja2 templates → HTML, then WeasyPrint → PDF.
10. **UI export** (`radar/export.py`): writes `out/ui/data.js` and copies the static `index.html`, `app.js` and `style.css`.
11. **CLI** (`radar/cli.py`, run as `python -m radar ...`): `list`, `fetch`, `extract`, `link`, `digest`, `ui`, `run` (all stages), `robots-check`.

### 2.2 Data flow per run

1. For each enabled council in `councils.yaml`, run its adapters and collect `DocRef`s. Save the listing snapshot to `data/listings/<council>/<date>.json` (metadata only; gitignored).
2. Filter `DocRef`s by the MVP document policy (§3.3): doc types, date window, and per-run caps.
3. Fetch each selected doc through the polite fetcher. `304 Not Modified` → reuse the cached text. New bytes → new `source_document` version.
4. Extract text, then mentions; normalise; trade-match.
5. Link mentions to projects; compute change events against the previous run.
6. Build the digest from this week's change events plus a "current pipeline" summary. Export the UI data.

Stages are idempotent and keyed on `doc_id` + `extractor_version`, so you can re-run `extract` on cached files without network access (replay mode).

### 2.3 On-disk layout

```
council-works-radar/
  PLAN.md  RESEARCH.md  README.md  pyproject.toml  requirements.txt  .gitignore
  config/
    councils.yaml        # per-council endpoints, adapter type, SearchParam headers, enabled flag, doc policy
    trades.yaml          # taxonomy, keywords, negatives, code prefixes
    suburbs/<council>.txt
  radar/
    __init__.py  __main__.py  cli.py  settings.py  models.py  db.py
    fetch.py  robots.py  ratelimit.py  text.py  normalise.py  trades.py  dedupe.py  changes.py
    digest.py  export.py
    adapters/  kentico_ksearch.py  joondalup_htmlresult.py  infocouncil.py  cockburn_html.py  static_links.py
    extract/   joondalup_budget.py  melville_cwp.py  cockburn_plans.py  agenda_items.py  blackspot.py  common.py
    templates/ digest.html.j2  digest.css
    ui/        index.html  app.js  style.css
  tests/
    fixtures/  <council>/listing_*.json|html, small text/HTML excerpts, tiny synthetic PDFs
    test_*.py
  data/                  # gitignored
    radar.sqlite
    listings/<council>/<YYYY-MM-DD>.json
    fetch_log.jsonl
  .cache/                # gitignored: raw downloads + extracted page text
    raw/<host>/<sha256>.<ext>
    text/<doc_id>.jsonl
    robots/<host>.txt
  out/                   # generated outputs; sample digest committed for the demo (no PDFs of council docs)
    digest-2026-W47.html
    digest-2026-W47.pdf
    ui/index.html  ui/app.js  ui/style.css  ui/data.js
    run-summary-2026-W47.json
```

`out/` holds only *our* generated artefacts: extracted facts, short quotes (≤1 sentence), and links. The committed sample digest is fine under that rule. Council PDFs never go into git.

### 2.4 Storage choice

**SQLite (stdlib `sqlite3`)** is the store. It needs no service, gives transactions for idempotent re-runs, and the queries for dedupe and change tracking are simple SQL. JSON export is only for the UI. Tables: `source_document`, `fetch_log` (also mirrored to JSONL), `project_mention`, `project`, `project_mention_link`, `change_event`, `run`.

---

## 3. Data sources and adapters

### 3.1 Adapter types (mapped to R§1.3 platform clusters)

| Adapter | Councils | Mechanism (from RESEARCH) | Notes |
|---|---|---|---|
| `kentico_ksearch` | Stirling, Melville, Rockingham | `POST` JSON listing with `SearchParam` / `SearchFilter` headers. Stirling: `/ksearchmna/ksearchresult`, body `{"take":12,"skip":0,"page":1,"pageSize":12}`, returns `Data[]` with `Agenda`, `MinutesFile`, `Attachments[]`. Melville and Rockingham: `/ksearch/mnaresult`, return `MNAFirstFileUrl` / `MNAFileUrl`, `MNADocumentType`, `MNAPublicationDate` | One adapter, per-site config: path, headers, field mapping. Response-shape contract test per site |
| `joondalup_htmlresult` | Joondalup | `POST /search/htmlresult` with the `SI:"CouncilMeeting"` JSON body (R§1.2.1). Returns an HTML fragment in `htmlResult`. Results are oldest-first: read `recordCountValue`, then jump to the last page(s) | Values must be objects (`[{"value":...}]`); plain strings give 400. Filenames are free text, so always take URLs from the listing |
| `infocouncil` | Wanneroo | GET `https://wanneroo.infocouncil.biz/`, a server-rendered table. Parse `RedirectToDoc.aspx?URL=Open/YYYY/MM/<MTG>_<DDMMYYYY>_<AGN|MIN>_AT...` | **Prefer the `.HTM` paper** (`..._AGN_AT.HTM`; skip the `_WEB.htm` frameset and `_BMK`). Meeting codes: `OC` first; others optional |
| `cockburn_html` | Cockburn | GET the Agendas-and-Minutes page with `?page=1&keyword=OCM|Ordinary%20Council%20Meeting&titleonly=true&years=2026`. Direct `getattachment/<GUID>/ECM_<id>_v<n>_...-pdf.aspx` links with sizes | Parse `ECM_<id>` and `v<n>` for versioning |
| `static_links` | All (budget / CWP / project plans), Main Roads Black Spot | Known URLs in `councils.yaml`, plus an optional "discover from page" mode that parses one index page for links matching a regex (e.g. the Cockburn Strategies-and-Plans page, the Melville budget page) | The simplest adapter. Covers the high-value annual documents |

Sitemap discovery (`sitemap.xml`) is a **fallback** for when a JSON endpoint breaks (R§6.3 risk 3). Melville's sitemap lists every meeting page. It is implemented in milestone M12 only if time allows.

### 3.2 Per-council source table (v1)

| Council | Priority | Listing | MVP documents | Extractor | Expected yield |
|---|---|---|---|---|---|
| **Joondalup** | 1 (demo) | `static_links` for budget; `joondalup_htmlresult` for meetings | **Budget-2026-27.pdf** (3.16 MB, capital works schedule); latest Council minutes (28 MB+, within cap) | `joondalup_budget` (word-box table parser); `agenda_items` | High: hundreds of coded projects with suburb, ward and funding split |
| **Melville** | 2 (demo) | `static_links` for the CWP HTML page; `kentico_ksearch` for meetings | **Capital Works Program HTML page**; latest OMC minutes (agendas ~51 MB, so minutes first) | `melville_cwp` (regex `Name ($value) description`); `agenda_items` | Medium-high: named projects, values, Black Spot flags |
| **Cockburn** | 3 (demo) | `static_links` for project plans; `cockburn_html` for meetings | **Project Plans 2026-2027 PDF** (8.7 MB); latest OCM minutes (31 MB) | `cockburn_plans` (labelled fields, road-segment bullets); `agenda_items` | Medium-high: project IDs, budgets, dates, named resurfacing segments |
| **Stirling** | 4 | `kentico_ksearch` | Latest Council minutes (8.6 MB, contains tender reports); Community and Resources Committee papers | `agenda_items` (tender report template) | Medium: tender awards, named projects |
| **Wanneroo** | 5 | `infocouncil` | Latest OC agenda **HTML paper** (~6 MB HTML) and minutes HTML | `agenda_items` (HTML variant; item IDs like `AS01-11/26`) | Medium: tender items, capital works reports |
| **Rockingham** | 6 | `kentico_ksearch` | Latest Ordinary Council agenda (4.9 MB); Planning and Asset Services Committee agenda | `agenda_items` | Medium: engineering and road reports |
| Main Roads Black Spot | optional | `static_links` | 2026-27 State Black Spot (local roads) PDF, 418 KB | `blackspot` | Cross-check + `funding_refs` for intersection projects in target councils |
| Perth | deferred | `static_links` | Budget PDF only, if added | `agenda_items` style keyword scan | Low priority |

Statutory budgets for Stirling, Wanneroo and Rockingham give asset-class totals only (R§1.2). v1 skips them for project extraction. If time allows, a later milestone records their program totals ("Road Renewal Program $20.4M") as council-level context in the digest header, but not as projects.

**Main Roads licence caution:** the licence isn't verified (`/copyright/` 404, R§2.2). In v1, Black Spot rows are used **only** to cross-check and tag `funding_refs` on council-sourced projects, and to show a "Black Spot approved" badge with a link. They are not published as standalone digest items unless a council source also exists. It's disabled by default (`enabled: false`), and the digest footer states the caution when it is on.

### 3.3 Document selection policy (per run)

- **Annual docs** (budget schedule, CWP page, project plans): fetch once; then conditional GET weekly. They are cheap to re-check with `304`.
- **Meetings:** take Ordinary Council (plus the named infrastructure committee) agendas and minutes **from the last 60 days**, at most **2 meeting documents per council per run**. Prefer minutes over agendas when both exist (minutes confirm decisions). Prefer HTML (Infocouncil) or attachments-excluded variants.
- **Size cap:** 80 MB hard cap (R§3.3). Docs above 40 MB are fetched only with `--allow-large` (first runs). Others are skipped with a logged `skipped_size` reason. The demo set (§12) stays under 35 MB per file.
- `doc_type` comes from the adapter (`MNADocumentType`, filename tokens `AGN`/`MIN`, titles).

### 3.4 `councils.yaml` sketch

```yaml
defaults:
  min_delay_s: 5          # stricter than the 2 s floor; R§3.3 recommends 5 s
  max_bytes: 83886080     # 80 MB
  large_threshold_bytes: 41943040
  meeting_window_days: 60
  max_meeting_docs_per_run: 2
councils:
  joondalup:
    name: City of Joondalup
    enabled: true
    hosts: [www.joondalup.wa.gov.au, api.joondalup.wa.gov.au]
    blocked_hosts: [search.joondalup.wa.gov.au]   # 401 on robots: never fetch
    terms_url: https://www.joondalup.wa.gov.au/disclaimer
    adapters:
      - type: static_links
        docs:
          - {url: "https://www.joondalup.wa.gov.au/awcontent/Web/Documents/Budget,%20Fees%20and%20Financial%20Reports/Budget-2026-27.pdf",
             doc_type: budget, financial_year: "2026-27", extractor: joondalup_budget}
      - type: joondalup_htmlresult
        endpoint: https://www.joondalup.wa.gov.au/search/htmlresult
        listing_url: https://www.joondalup.wa.gov.au/city-and-council/council-and-committee-meetings/council-agenda-and-minutes
        page_size: 24
  # ... melville, cockburn, stirling, wanneroo, rockingham as per §3.2
global_blocklist:          # enforced in fetch.py regardless of robots result
  - host: cdn.canning.wa.gov.au
  - host: www.swan.wa.gov.au
    path_prefix: /v1/
  - host: www.tenders.wa.gov.au
  - host: www.gosnells.wa.gov.au
  - host: search.joondalup.wa.gov.au
```

---

## 4. Extraction schema

The schema follows R§5.2, with these justified changes:

- **`doc_id` = sha256 of bytes**, plus a separate stable **`doc_key`** = canonical URL (with `?v=1` and `language=en` stripped). That way "same URL, new bytes" is a new version of the same `doc_key` (R§5.4) without a separate lookup table.
- **`work_type` → `trade_labels`** as a list of `{code, method, evidence}` objects, so each label keeps the keyword that fired (R§5.5 requirement). `work_type_method` moves into each label.
- **`char_span`/`bbox`:** store `page` (always), `bbox` for PDF word-box extractors, and `char_start/char_end` into the page text for regex extractors. At least one of these is required.
- **`quote`:** a short excerpt of at most 300 characters (≤1 sentence, R§3.3 rule 8). This is what the digest shows. Full page text stays in `.cache/` only.
- **`date_start` / `date_end`** (ISO, nullable) are parsed out of `date_stated` when possible (Cockburn start/end dates).
- **`tender_no`** is added as an explicit field (dedupe key 4 in R§5.4).
- **`stage`** values as R§5.2; `deleted/deferred` is split into `deferred` and `deleted`.
- **Work types requested in the brief** map to taxonomy codes: road resurfacing → `ROAD_RESURF`; drainage → `DRAINAGE`; footpaths → `FOOTPATH`; kerbing → `KERBING`; line marking → `LINE_MARK`; Other/Unknown → `OTHER_CIVIL` / `UNKNOWN`. The other R§5.5 codes (`ROAD_CONST`, `INTERSECTION`, `CARPARK`, `BRIDGE`, `STREETSCAPE`, `NON_CIVIL`) are kept because they come free with the same rules.

### 4.1 Tables

**`source_document`**: `doc_id` PK (sha256 bytes), `doc_key` (canonical URL), `version_no`, `council_id`, `doc_type` (budget | capital_program | project_plans | agenda | minutes | attachments | tender_register | blackspot_list | other), `title`, `meeting_type`, `meeting_date`, `financial_year`, `source_url`, `listing_url`, `retrieved_at`, `http_etag`, `http_last_modified`, `content_type`, `bytes`, `page_count`, `text_sha256`, `extractor_version`, `extract_status` (ok | partial | failed | ocr_needed | skipped_size), `superseded_by`.

**`project_mention`** (the system of record; one row per fact sighting):

| field | type | notes |
|---|---|---|
| `mention_id` | text (uuid5 of doc_id + extractor + anchor) | deterministic, so re-runs are idempotent |
| `doc_id`, `page`, `bbox`, `char_start`, `char_end` | provenance | page required; one of bbox/char span required |
| `council_id` | text | |
| `council_project_code` | text? | `RPR3542`, `4C.18` |
| `cost_code` | text? | Joondalup `W5033` |
| `tender_no` | text? | |
| `project_name_raw` | text | verbatim |
| `location_raw` | text? | |
| `road_name`, `from_road`, `to_road`, `direction`, `suburb`, `ward` | text? | normalised |
| `slk_from`, `slk_to` | real? | Black Spot only |
| `trade_labels` | json | `[{"code":"ROAD_RESURF","method":"rule","evidence":"kw:resurfac"}]` |
| `value_amount` | real? | AUD |
| `value_basis` | text | budget_total \| current_year \| municipal \| grant \| contract_award \| estimate |
| `value_gst` | text | ex \| inc \| unknown |
| `funding_refs` | json | Municipal, Reserve, Grant, BlackSpot_State, BlackSpot_Federal, R2R, MRRG, Loan, Contribution, CarryForward |
| `funding_split` | json? | Joondalup columns `{municipal, reserve, trust, grants_new, grants_cfwd, contribution, brought_fwd, loan}` |
| `stage` | text | proposed_budget \| adopted_budget \| budget_review \| tender_advertised \| tender_awarded \| in_progress \| completed \| deferred \| deleted |
| `date_stated`, `date_start`, `date_end` | text / iso? | |
| `financial_year` | text | "2026-27" |
| `resolution_ref` | text? | |
| `supplier_name` | text? | |
| `quote` | text | ≤300 chars |
| `confidence` | real 0–1 | from rules (§4.2) |
| `uncertainty_flags` | json | value_ambiguous_column, wrapped_text_join, name_only_no_location, multi_year, carry_forward, possible_duplicate, ocr_used, ocr_needed, value_missing, gst_unknown, agenda_not_resolution |
| `extractor`, `extractor_version`, `extracted_at` | | |

**`project`**: `project_id`, `council_id`, `dedupe_key` + `dedupe_key_type`, `canonical_name`, `road_name`, `from_road`, `to_road`, `suburb`, `trade_codes` (union), `latest_value`, `latest_value_basis`, `latest_stage`, `financial_year`, `first_seen`, `last_seen`, `confidence` (max of mentions), `flags` (union), `review_needed` (bool).

**`project_mention_link`**: `project_id`, `mention_id`, `link_method` (code | segment | intersection | tender | fuzzy | singleton), `link_score`.

**`change_event`**: `event_id`, `run_id`, `project_id`, `event_type` (new | value_changed | stage_advanced | deferred | deleted | location_refined | source_added), `old`, `new` (json), `mention_id` (the evidence), `created_at`.

**`run`**: `run_id`, `started_at`, `finished_at`, `iso_week`, `councils`, counts, `errors` (json).

### 4.2 Confidence scoring (rules, transparent)

Start at 0.5, then:
- +0.2 if the row came from a structured template (budget table row, labelled project-plan field, CWP paragraph)
- +0.1 if there's a project code or tender number
- +0.1 if both a location and a value were parsed
- −0.2 per serious flag (`value_ambiguous_column`, `wrapped_text_join`, `ocr_used`)
- −0.1 for `name_only_no_location` or `agenda_not_resolution`

Clamp to [0, 1]. The digest shows **High ≥ 0.75, Medium 0.5–0.75, Low < 0.5**.

### 4.3 Extractor designs

- **`joondalup_budget`** (hardest; R§5.1): use PyMuPDF `page.get_text("words")`. Find the header row by matching tokens `Project Number`, `Cost Code`, ..., `Expenditure`, and derive column x-ranges from the header word positions. Rows are anchored by `^[A-Z]{3}\d{4}$` in the project-number column with `W\d{4}` next to it. Description and name words within the column's x-range, sitting vertically between the previous and next anchors, are joined (flag `wrapped_text_join` when lines are gathered from above the anchor). Numeric columns: "-" = 0, parentheses = negative. `value_basis = budget_total` (Expenditure column), and `funding_split` gets the other columns. Code-prefix priors: `RPR` → ROAD_RESURF, `RDC` → ROAD_CONST, `PEP` → NON_CIVIL (the full prefix table lives in trades.yaml, filled in from the fixture). Fallback: if header detection fails on a page, use `pdftotext -layout` with the anchor regex and flag `value_ambiguous_column`.
- **`melville_cwp`**: parse the HTML page. One block (p/li/heading group) per project. Regex `^(?P<name>.+?)\s*\(\$(?P<value>[\d,\.]+\s*(?:million|m)?)\)\s*(?P<desc>.*)$`. Funding refs come from keywords ("Black Spot", "Roads to Recovery", "grant", "external funding"). `stage = adopted_budget`, `financial_year` from the page heading.
- **`cockburn_plans`**: page-level. Find `ID:` (`\d+[A-Z]\.\d+`), `Directorate:`, the project title (first heading line), the budget table ("Total Budget", current-year budget), and start/end dates. For programs like "Roads Resurfacing Program", parse bullet lines with the road-segment regex (R§5.3) into **one mention per segment**, with the parent ID as `council_project_code` plus a segment suffix (`4C.18#elderberry-dr|south-lake|berrigan-dr`). Segment values are usually absent, so flag `value_missing` and show the program total on the parent only.
- **`agenda_items`** (shared by Stirling, Rockingham, Wanneroo, Joondalup, Cockburn and Melville minutes): (1) split the text into items with a per-council item-number regex from `councils.yaml` (Stirling `\d+\.\d+/[A-Z]+\d+`, Wanneroo `[A-Z]{2}\d{2}-\d{2}/\d{2}`, generic `^\d{1,2}\.\d{1,2}(\.\d+)?\s+[A-Z]`); (2) keep items whose title or first 500 characters match civil keywords (tender, resurfac, road, footpath, drainage, black spot, roundabout, capital works, budget amendment, MRRG, Roads to Recovery, kerb, line marking); (3) inside each kept item, run the tender regexes (`Tender No\.?\s*(\d+)`, `tender from (.+?) (?:for|be accepted)`, `\$[\d,]+(?:\.\d+)?\s*\((?:ex|excl)`) and money/road regexes; (4) set `stage` by heuristics: an award recommendation → `tender_awarded` (minutes) or `tender_advertised`/`proposed` (agenda, flagged `agenda_not_resolution`); budget amendment → `budget_review`. Dedupe the officer recommendation against the council resolution within a document by keeping one mention per (item, tender_no/value). Infocouncil HTML uses the same logic on heading-delimited blocks.
- **`blackspot`**: line pattern `^(?P<loc>.+?)(?: SLK (?P<a>[\d.]+)\s*-\s*(?P<b>[\d.]+))? \((?P<council>City|Town|Shire) of (?P<name>[^)]+)\)`, then a description and an amount. Keep only target councils.

---

## 5. Dedupe and change tracking

### 5.1 Document level

- `doc_key` = canonical URL: lower-case host; strip `v`, `language` and other cache-buster params listed per host; decode `%20` consistently.
- On fetch: send `If-None-Match` / `If-Modified-Since` from the last version. `304` → no new version, `last_checked_at` updated. `200` with the same sha256 → no new version. A new sha256 → `version_no + 1`, old row `superseded_by` = new `doc_id`, and a re-extract.
- Explicit version hints are parsed into `source_document.version_hint`: Cockburn `ECM_<id>_v<n>` (same ECM id, higher v = new version even if the URL GUID changed), Infocouncil `_SUP`.

### 5.2 Mention → project linking (R§5.4 key ladder, strongest first)

1. `code:` `(council_id, council_project_code)` (plus segment suffix for Cockburn segments).
2. `segment:` `(council_id, norm(road), norm(from), norm(to))`. Work type is *not* part of the key (labels differ by source wording). Instead, linking is refused if the trade sets don't intersect, which stops a resurfacing and a drainage job on the same segment from merging.
3. `intersection:` `(council_id, sorted(norm(roadA), norm(roadB)))` for roundabouts and Black Spot items.
4. `tender:` `(council_id, tender_no)`.
5. `fuzzy:` rapidfuzz `token_set_ratio` ≥ 90 on the normalised name, same council and same or adjacent FY. **Never auto-merges.** It links with `link_method=fuzzy`, sets `possible_duplicate` and `review_needed`, and the digest shows it as "possible match".
6. Otherwise a new `singleton` project.

Cross-key joins: if a mention matches project A by tender and project B by segment, attach it to the higher-ranked key and record a `possible_duplicate` between A and B (no automatic merge in v1).

**Normalisation** (`normalise.py`): lower-case; expand `Rd/St/Dr/Ave/Blvd/Pde/Cres/Hwy/Pl/Ct/Tce/Cl` (whole tokens only); strip punctuation; pull `(westbound|eastbound|northbound|southbound|nb|sb|eb|wb)` into `direction`; drop FY suffixes (`26/27`, `2026-27`); collapse whitespace; normalise en dash/hyphen.

### 5.3 Project state and change events

After linking, each project's derived state comes from its mentions:
- `latest_value`: the value from the most recent document (by `meeting_date` or `retrieved_at`), with a basis preference of contract_award > budget_total > current_year when documents are on the same date.
- `latest_stage`: the most advanced stage, ordered proposed_budget < adopted_budget < budget_review < tender_advertised < tender_awarded < in_progress < completed. `deferred`/`deleted` override when their evidence is newer.

Before and after each run, snapshot `(project_id → state)` and emit:
- `new`: the project didn't exist before.
- `value_changed`: |Δ| ≥ max($10k, 5%). Store old, new and delta.
- `stage_advanced`: the stage moved forward.
- `deferred` / `deleted`: mid-year review or resolution language ("deferred", "removed from", "carry forward" → also `carry_forward` flag).
- `location_refined`: a road, from/to or suburb field went from null to a value.
- `source_added`: a new document mentions an existing project with no other change.

On the **first run**, every project is `new`. The digest handles this with a "baseline run" banner and groups by council and trade instead of listing hundreds of "new" items (§7.1).

---

## 6. Trade-matching rules

### 6.1 Taxonomy (R§5.5)

`ROAD_RESURF`, `ROAD_CONST`, `INTERSECTION`, `DRAINAGE`, `FOOTPATH`, `KERBING`, `LINE_MARK`, `CARPARK`, `BRIDGE`, `STREETSCAPE`, `OTHER_CIVIL`, `NON_CIVIL`, and `UNKNOWN` (no rule fired). The brief's "Other/Unknown" bucket is `OTHER_CIVIL` + `UNKNOWN`.

### 6.2 `config/trades.yaml` shape

```yaml
trades:
  ROAD_RESURF:
    label: Road resurfacing / preservation
    keywords: ['\bresurfac\w*', '\breseal\w*', 'asphalt overlay', 'mill(ing)? and fill', 'road preservation',
               'pavement renewal', 'spray seal', 'road (?:rehabilitation|renewal)', 'rehabilitation.{0,30}\broad']
    negative: ['tennis', 'netball', 'basketball', 'court', 'synthetic (?:turf|surface)', 'bowling green', 'cricket (?:pitch|wicket)', 'playground']
    on_negative: NON_CIVIL
  FOOTPATH:
    keywords: ['\bfootpath', '\bshared path', '\bPSP\b', 'dual use path', '\bcycleway', 'bike lane', 'pedestrian (?:path|crossing)']
    negative: ['bush ?walk', 'nature trail']
  KERBING:   {keywords: ['\bkerb\w*'], negative: []}
  LINE_MARK: {keywords: ['line ?marking', 'pavement marking', '\bRRPM', 'raised pavement marker']}
  DRAINAGE:  {keywords: ['drainage', 'stormwater', '\bsump', 'soak ?well', '\bgull(y|ies)', 'culvert', 'outfall', '\bswale', 'drain(?:age)? pipe', 'basin']}
  # ... ROAD_CONST, INTERSECTION, CARPARK, BRIDGE, STREETSCAPE, NON_CIVIL per R§5.5
code_prefixes:
  joondalup: {RPR: ROAD_RESURF, RDC: ROAD_CONST, PEP: NON_CIVIL}   # extend from fixture
context_required:
  pipe: [drain, stormwater]      # bare "pipe" only counts with drainage context
  path: [foot, shared, cycle]    # bare "path" is too broad
```

### 6.3 Algorithm

1. Build the text to match = `project_name_raw + description + location_raw` (not the full agenda item, which avoids topic bleed).
2. **Code prefix prior** (if any) gives a label with evidence `prefix:RPR`.
3. For each trade, positive regexes on the text. If a negative regex also matches within 60 characters, emit `on_negative` (default `NON_CIVIL`) instead.
4. Multi-label is allowed. Composite rule: `ROAD_CONST` with "upgrade|duplication|extension" adds `KERBING`, `DRAINAGE` and `LINE_MARK` as **implied** labels (`method=rule_implied`, shown lighter in the UI).
5. No label → `UNKNOWN`. Any civil label present → drop `NON_CIVIL`/`OTHER_CIVIL` unless it came from a negative override.
6. Each label stores `evidence` (`kw:<pattern>`, `prefix:<code>`, `implied:<from>`).

### 6.4 LLM hook (not used in v1)

`trades.classify_fallback(text) -> list[Label]` returns `[]` in v1. A later version may plug in a **local** model (llama.cpp/Ollama) for `UNKNOWN` items only, storing `method=llm` as a suggestion that never overwrites rule labels or provenance. Wanneroo text is excluded if Infocouncil signals `ai-input=no` (R§6.3 risk 11).

### 6.5 Tuning loop

`python -m radar trades-report` prints the top `UNKNOWN` project names and per-trade counts, so keywords can be tuned against real data. A **gold file** `tests/fixtures/trade_gold.csv` (~60 hand-labelled names taken from the real documents) is checked in CI: per-label precision and recall ≥ 0.85 on that set.

---

## 7. Digest and UI

### 7.1 Weekly digest (`out/digest-<YYYY>-W<ww>.html` + `.pdf`)

Single self-contained HTML file (inline CSS, no JS, so it prints cleanly to PDF).

1. **Header:** "WA Council Works Radar: Week 47, 2026 (16–22 Nov)". Run date. Councils covered, each with document counts and last-retrieved time. Baseline banner on the first run.
2. **This week's signals** (from `change_event`), grouped by trade, then by council. Each item is a card:
   - **Project name** · council · suburb/ward · trade chips (with implied labels shown lighter)
   - Value: `$406,364 (budget total, FY2026-27, GST unknown)`; funding: `Municipal / Grant (Black Spot)`
   - Stage badge plus the change: `NEW` / `VALUE ▲ $50,000 (+14%)` / `STAGE adopted_budget → tender_awarded`
   - Dates if stated
   - ≤1-sentence quote in a muted style
   - **Source:** "City of Joondalup, Budget 2026-27, p. 41, retrieved 2026-11-22" (linked to the council URL, with `#page=41` for PDFs)
   - **Confidence** badge (High/Medium/Low) and uncertainty flags as small labelled pills ("wrapped text", "value column ambiguous", "possible duplicate", "agenda, not yet resolved")
3. **Upcoming / pipeline snapshot:** a table of the top 25 civil projects per trade by value with stage < tender_awarded (the pre-tender sweet spot).
4. **Needs review:** possible duplicates, low-confidence items, documents that failed extraction or need OCR.
5. **Coverage & health:** per council, listings fetched, docs new/unchanged (304)/skipped (size)/failed, robots status. Any host paused (403/429) is shown in red.
6. **Footer (attribution and caution):** "Council material © the respective City. Facts are extracted from public documents and link to the official source; documents are not reproduced. Verify against the source before relying on any figure. Values are as stated in source documents; GST basis may be unknown." Plus the Main Roads licence caution when Black Spot is enabled, and the R§3.2 non-commercial note.

On the baseline run, section 2 becomes "Baseline pipeline": the top 15 per trade by value, with counts per council × trade. The full list is in the UI.

**PDF:** WeasyPrint renders the same HTML with a print stylesheet (A4, page numbers, links kept). If WeasyPrint import or rendering fails, fall back to `pymupdf.Story` (already installed, no system deps). If both fail, log it and still ship the HTML. The PDF contains only our digest, never council pages.

### 7.2 Filterable web UI (`out/ui/`)

- Static files: `index.html`, `app.js` (vanilla JS, no build step), `style.css`, and `data.js` (`window.RADAR_DATA = {...}`). Using a `.js` data file rather than `fetch('data.json')` means the UI also opens straight from `file://`. `python -m radar serve` (a wrapper around `http.server`, bound to 127.0.0.1:8000) is the documented way.
- **Layout:** a filter sidebar plus a results table with expandable rows.
- **Filters:** council (multi), trade (multi, with "include implied" toggle), stage, FY, value range (min/max), confidence (≥ level), "changed in last N weeks", "has flags", free-text search over name/road/suburb.
- **Table columns:** project, council, suburb, trades, value, basis, stage, last change, confidence, # sources. Sortable by clicking a header.
- **Expanded row:** every mention (document title, date, page, quote, link, extractor, flags) and the change history timeline.
- **Header counters:** projects shown / total, total value shown. A "Download CSV of current filter" button (client-side).
- Filter state lives in the URL hash so you can share a view.
- `data.js` contains facts, ≤300-character quotes and links only. Its size is kept under ~5 MB.

---

## 8. Stack and dependencies

All free and open source. Python **3.13** (installed here; supports ≥3.11).

| Package | Version pin | Why |
|---|---|---|
| `requests` | `==2.34.2` | HTTP. Already installed, synchronous, simple. (httpx isn't needed: no async or HTTP/2 required with one connection per host) |
| `beautifulsoup4` | `==4.15.0` | HTML listing and CWP parsing |
| `lxml` | `==6.1.3` | Fast parser backend for bs4 |
| `pymupdf` | `==2.1` (import as `pymupdf`, not `fitz`, which is deprecated) | PDF words with coordinates; page count; Story PDF fallback. AGPL, which is fine for a private/open tool. Note it if commercialised |
| `pypdf` | `==6.21.4` | Lightweight fallback text extraction and metadata. Already installed |
| `pyyaml` | `==6.0.3` | Config |
| `jinja2` | `==3.1.6` | Digest templates |
| `rapidfuzz` | `==3.14.6` | Fuzzy name matching (MIT) |
| `weasyprint` | `==70.0` | HTML → PDF. Needs pango/harfbuzz, which are present in this environment. **Optional** extra (`.[pdf]`) |
| `pytest` | `==9.2.0` | Tests (dev extra) |
| stdlib | — | `sqlite3`, `urllib.robotparser`, `hashlib`, `json`, `argparse`, `http.server`, `logging`, `uuid`, `datetime`, `zoneinfo` (Australia/Perth) |
| system | `pdftotext` (poppler), `tesseract` | Optional fallbacks only; both already present. Not required for tests |

Packaging: a `pyproject.toml` with `[project.optional-dependencies] pdf = [weasyprint]`, `dev = [pytest]`, and a pinned `requirements.txt`. No network services, API keys or paid tiers. Versions are pinned to what `pip index` reports today. The build may relax them to `~=` if a pin fails to install.

---

## 9. Compliance

1. **User-Agent:** `CouncilWorksRadar/0.1 (+https://github.com/johnbobthescob/council-works-radar; pre-tender research bot; respects robots.txt)`. Configurable in `settings.py`. No personal email in the UA. Never a browser UA.
2. **robots.txt before fetch:** `robots.py` fetches `/robots.txt` per host once per run (cached in `.cache/robots/`, TTL 24 h), parsed with `urllib.robotparser`, and checks `can_fetch(UA, url)` **before every request**, including listing POSTs (check the endpoint path) and redirects (re-check the final host/path). Robots responses: 404 → allowed; **401/403 → host treated as disallowed** (Joondalup search host, Gosnells); 5xx/timeout → disallowed for this run. A `global_blocklist` in `councils.yaml` (§3.4) is enforced as well, as defence in depth. `python -m radar robots-check` prints a status table for all configured hosts.
3. **Rate limits:** a per-host token: ≥ **5 s** between requests to the same host (the brief's floor is 2 s; R§3.3 recommends 5 s), one connection per host, no concurrency across hosts in v1 (simpler; the run is small). Honour `Crawl-delay` if it's larger. Exponential backoff on 5xx (3 tries: 10 s, 30 s, 90 s).
4. **Stop conditions:** 403, 429, or a detected challenge page (Cloudflare/Sucuri/Akamai markers in the body, or `cf-mitigated` header) → pause that host for 24 h (`data/host_pause.json`), record it in the run summary and the digest health section, and carry on with other hosts. Never retry with different headers.
5. **Conditional GET:** send `If-None-Match` and `If-Modified-Since` from the last version. HEAD first when the listing doesn't give a size or the last known size is > 10 MB. Read `Content-Length` and skip above the cap. Streaming download with a running byte counter aborts at the cap even without `Content-Length`.
6. **Size caps:** 80 MB hard, 40 MB "large" threshold (opt-in). Listing and HTML responses are capped at 10 MB.
7. **No logins, CAPTCHAs, paywalls or bypassing.** Cookies are not persisted between runs. No JS execution.
8. **Fetch log:** every request (method, URL, status, bytes, ETag, Last-Modified, elapsed, robots decision, cache hit) goes to the `fetch_log` table and `data/fetch_log.jsonl`.
9. **Caches gitignored:** `.cache/`, `data/`, `*.sqlite`, `*.pdf` outside `out/`, `.venv/`, `__pycache__/`. `.cache/raw/` is pruned after 30 days (`python -m radar prune`); extracted text and hashes are kept.
10. **Attribution:** every item links to the council URL with document title, page and retrieval date. The digest footer carries the copyright notice (§7.1). The UI footer has the same text. Quotes are capped at 300 characters / 1 sentence.
11. **No third-party AI APIs** on council text (R§3.3 rule 9).
12. **Tests never hit the network** unless `RADAR_LIVE=1` (§11). A pytest fixture monkeypatches `requests.Session.send` to raise if network access is attempted without the flag.

---

## 10. Milestones

Each milestone is one or a few commits, runnable and tested on its own. "Done-when" is the acceptance check. The test listed is the minimum to add.

| # | Milestone | Scope | Done-when | Suggested automated test |
|---|---|---|---|---|
| **M0** | Skeleton & config | `pyproject.toml`, `requirements.txt`, `.gitignore`, package layout, `settings.py`, `config/councils.yaml` (6 councils + blocklist), `config/trades.yaml` (full taxonomy), `python -m radar --help` | `pip install -e .[dev]` works; CLI prints its subcommands; config loads | `test_config.py`: YAML loads; every enabled council has ≥1 adapter; blocklist contains Canning CDN, Swan `/v1/`, Tenders WA |
| **M1** | Data model & SQLite | `models.py` dataclasses, `db.py` schema + migrations (just `CREATE TABLE IF NOT EXISTS` + `schema_version`), upsert helpers | Fresh DB is created; mentions round-trip | `test_db.py`: insert/read source_document and mention; idempotent upsert by `mention_id` |
| **M2** | Polite fetcher + robots | `robots.py`, `ratelimit.py`, `fetch.py` (UA, robots, blocklist, rate limit, conditional GET, HEAD, streaming size cap, pause on 403/429/challenge, fetch log, `.cache/raw`), `robots-check` CLI | `robots-check` shows allowed for the 6 councils and blocked for Canning CDN, Swan `/v1/`, Tenders WA (live, manual); unit tests pass offline | `test_fetch.py` with a local `http.server` fixture: robots disallow is respected; 304 path reuses cache; size cap aborts; 2 requests to the same host are ≥ delay apart (fake clock); 403 pauses host; network guard |
| **M3** | Text layer | `text.py`: PDF → per-page text + words (PyMuPDF), HTML → blocks, image-only page detection, `pdftotext` fallback, text cache in `.cache/text/` | Extracts a tiny synthetic PDF and an HTML fixture | `test_text.py`: synthetic PDF (generated in-test with PyMuPDF) gives the expected words/bboxes; empty page flagged `ocr_needed` |
| **M4** | Normaliser | money (`$4.3 million`, `(1,234)`, `-`), road normalisation and abbreviations, direction, road-segment regex, intersection pair, FY parsing, GST basis, suburb lookup | All normaliser tests green | `test_normalise.py`: table-driven, ~40 cases taken from RESEARCH examples |
| **M5** | Trade matcher | `trades.py` per §6.3, `trades-report` CLI, gold CSV (initial ~30 names from RESEARCH; extend after M8) | Gold set precision and recall ≥ 0.85 per label present | `test_trades.py`: "tennis court resurfacing" → NON_CIVIL; `RPR3542` → ROAD_RESURF via prefix; road upgrade gives implied kerbing/drainage; gold-set metric test |
| **M6** | Listing adapters A: static, Cockburn, Infocouncil | `static_links` (incl. discover-from-page), `cockburn_html`, `infocouncil` → `DocRef`s; `python -m radar list --council X` | Live `list` prints recent docs for Cockburn and Wanneroo (manual check); fixtures captured into `tests/fixtures/` (small HTML snippets only) | `test_adapters_html.py`: fixture HTML gives the expected DocRefs (URL, doc_type, date, ECM version, Infocouncil `.HTM` preferred over PDF) |
| **M7** | Listing adapters B: Kentico ksearch + Joondalup | `kentico_ksearch` with per-site config (Stirling, Melville, Rockingham), `joondalup_htmlresult` (last-page logic) | Live `list` works for all 4; recorded JSON fixtures (trimmed to ~5 records) | `test_adapters_json.py`: contract tests on recorded JSON (required keys present, mapping correct); request builder produces the exact headers/body from R§1.2 |
| **M8** | Extractor: Melville CWP (HTML) | `melville_cwp` + end-to-end `fetch → extract` for one council; `extract` CLI | Live run produces ≥10 Melville mentions with values and Black Spot refs | `test_extract_melville.py`: fixture HTML snippet → expected names/values/funding; provenance fields set |
| **M9** | Extractor: Joondalup budget schedule | `joondalup_budget` word-box table parser + `pdftotext` fallback | Live run gives ≥100 coded projects; spot-check 10 rows against the PDF by hand (recorded in the commit message) | `test_extract_joondalup.py`: **synthetic PDF** built in-test that reproduces the column layout with wrapped descriptions, plus a small text-layer fixture of real rows (≤10 rows of facts, not the PDF) → exact codes, suburbs, totals; `RPR3542` total 406,364 |
| **M10** | Extractor: Cockburn project plans | `cockburn_plans` labelled fields + segment bullets | Live run gives project IDs with budgets and ≥5 named resurfacing segments | `test_extract_cockburn.py`: page-text fixture → ID `4C.18`-style, budgets, segments "Elderberry Dr – South Lake to Berrigan Dr" parsed into road/from/to |
| **M11** | Extractor: agenda/minutes items | `agenda_items`: segmentation by council regex, civil keyword filter, tender regexes, stage heuristics, in-doc recommendation/resolution dedupe; PDF and Infocouncil HTML variants | Live run on Stirling 17 Nov 2026 minutes finds the Kardan tender (non-civil, so NON_CIVIL, still extracted) and ≥1 civil item from Wanneroo or Rockingham | `test_extract_agenda.py`: text fixtures per council style → items split correctly; tender no/supplier/value parsed; duplicate recommendation collapsed |
| **M12** | Dedupe/linker | `dedupe.py` key ladder, fuzzy flagging, project state derivation | Re-running `link` is idempotent; known duplicates (same code in two docs) merge | `test_dedupe.py`: code match, segment match with trade-intersection guard, order-independent intersection, tender key, fuzzy → flagged not merged |
| **M13** | Change tracking | `changes.py` snapshot/diff, `run` table, events | Second run with a modified fixture produces `value_changed` and `stage_advanced` | `test_changes.py`: two synthetic runs → expected event types and deltas; threshold behaviour; baseline run all `new` |
| **M14** | Digest HTML + PDF | Jinja2 templates, grouping, baseline mode, health section, attribution footer, WeasyPrint with Story fallback | `python -m radar digest` writes `out/digest-YYYY-Www.html` (+ `.pdf`) from the DB | `test_digest.py`: golden-structure test on a fixture DB (has source links for every item, footer text, flags rendered); PDF test skipped if weasyprint is missing |
| **M15** | Web UI | `export.py` → `out/ui/data.js`; static `index.html/app.js/style.css`; `serve` command | Opening `out/ui/index.html` shows the table; filters work (manual check) | `test_export.py`: data.js parses as JSON after stripping the prefix; every project has ≥1 source with URL and retrieved_at; size < 5 MB. Optional: tiny JS-free smoke (HTML contains the expected element IDs) |
| **M16** | `run` orchestration + live demo | `python -m radar run --councils joondalup,melville,cockburn[,stirling,wanneroo,rockingham]`, run summary JSON, README "how to run", commit sample `out/` | Live run completes for ≥3 councils; `out/` has the digest HTML (+PDF), UI, run summary; README updated | `test_cli_smoke.py`: full pipeline on fixtures with network guard on → produces all `out/` files. Plus the gated live test (§11.3) |
| **M17** *(stretch)* | Black Spot cross-check | `blackspot` extractor; funding-ref tagging of matching council projects; licence caution in footer; disabled by default | With `--enable blackspot`, ≥1 target-council project gets `BlackSpot_State` | `test_extract_blackspot.py`: line fixtures → road, SLK, council, amount |
| **M18** *(stretch)* | Sitemap fallback + Stirling/Rockingham committee docs + statutory budget totals | sitemap discovery adapter; council program totals in the digest header | Adapters fall back to the sitemap when the JSON contract test fails | `test_sitemap.py` on a fixture sitemap |

The demo-critical path is **M0 → M8 → M9 → M10 → M12 → M13 → M14 → M15 → M16**, with M6/M7 needed for meeting docs. If time is short, M11 can ship limited to Stirling + Wanneroo, since three councils (Joondalup, Melville, Cockburn) already meet the "≥3 councils" demo bar through annual docs alone.

---

## 11. Test strategy

### 11.1 Unit tests (offline, fast)

- **Normaliser, trade rules, dedupe, change tracking:** pure functions, table-driven.
- **Extractors:** run against **small fixtures** of extracted facts or text: short HTML snippets, page-text excerpts (≤ a few rows/paragraphs), and **synthetic PDFs generated inside tests** with PyMuPDF to reproduce layouts (column x-positions, wrapped lines). No council PDFs are committed (copyright, R§3.2). Text excerpts are kept minimal and factual.
- **Fetcher:** a local `http.server` thread fixture serving robots.txt, ETag/304 responses, oversized bodies, 403 and challenge pages. A fake clock checks the rate limiter.

### 11.2 Contract / smoke tests (offline, fixtures)

- **Listing contract tests:** recorded, trimmed listing responses (JSON with ~5 records; HTML fragments) for each adapter. They assert the fields the adapter depends on exist (`Data[].Agenda`, `MNAFileUrl`, `htmlResult`, `recordCountValue`, Infocouncil link pattern). When a live endpoint changes, re-recording a fixture with `python -m radar record-fixture --council X` and seeing the contract test fail pinpoints the break.
- **Pipeline smoke test:** fixture listings + synthetic docs served by the local HTTP fixture → full `run` → asserts the `out/` files exist and every digest item has a source link and retrieval date.
- **Network guard:** an autouse fixture blocks real sockets unless `RADAR_LIVE=1`.

### 11.3 Live integration test (opt-in)

`tests/test_live.py`, marked `@pytest.mark.live` and skipped unless `RADAR_LIVE=1`:
- `robots-check` for all configured hosts (asserts blocked hosts stay blocked).
- One listing call per adapter type (5 requests total, rate-limited).
- One small document fetch (Melville CWP HTML page) with extraction yielding ≥1 mention.
- Never runs in CI by default. Documented in the README.

### 11.4 Quality gates

`pytest -q` runs in < 30 s offline. The trade gold-set metric (≥ 0.85) and the Joondalup row-level expectations act as regression guards for rule tuning.

---

## 12. v1 demo

### 12.1 Demo document set (all robots-allowed, all ≤ 35 MB)

| Council | Document | Size | Extractor |
|---|---|---|---|
| Joondalup | Budget-2026-27.pdf | 3.2 MB | joondalup_budget |
| Melville | Capital Works Program HTML page | small | melville_cwp |
| Cockburn | Project Plans 2026-2027 PDF | 8.7 MB | cockburn_plans |
| Stirling | Council Minutes 17 Nov 2026 | 8.6 MB | agenda_items |
| Wanneroo | OC 24 Nov 2026 agenda HTML paper | ~6 MB | agenda_items (HTML) |
| Rockingham | Nov 2026 Ordinary Council agenda | 4.9 MB | agenda_items |

Plus listing calls for each council. The total is about 35 documents' worth of requests (≈ 32 MB), which at ≥5 s/host spread across hosts finishes in a few minutes.

### 12.2 How to run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,pdf]'          # or: pip install -r requirements.txt
python -m radar robots-check          # confirm allow/block table
python -m radar run --councils joondalup,melville,cockburn,stirling,wanneroo,rockingham
python -m radar serve                 # http://127.0.0.1:8000/ui/
pytest -q                             # offline tests
RADAR_LIVE=1 pytest -q -m live        # optional live checks
```

`run` = list → fetch → extract → link → changes → digest → ui. Each stage is also available on its own; `--offline` replays from `.cache/`.

### 12.3 Expected files in `out/` (committed as the sample)

```
out/
  digest-2026-W47.html        # baseline digest, ≥3 councils, source links + flags
  digest-2026-W47.pdf         # same, via WeasyPrint (or Story fallback)
  run-summary-2026-W47.json   # councils, docs fetched/304/skipped, mentions, projects, events, errors
  ui/index.html  ui/app.js  ui/style.css  ui/data.js
```

**Demo acceptance:** the digest shows projects from at least 3 councils, each with a working council source link, page number and retrieval date. At least 100 projects in total (Joondalup alone should give this). At least one item in each of ROAD_RESURF, DRAINAGE and FOOTPATH. The uncertainty flags appear where expected. The UI filters by council and trade.

---

## 13. Risks and fallbacks

| Risk | Likelihood | Fallback |
|---|---|---|
| **Listing endpoint changes** (Kentico ksearch, Joondalup htmlresult are undocumented) | Medium | Contract tests detect it; `static_links` entries for known doc URLs keep annual docs working; sitemap discovery (M18); the adapter failure is shown in the digest health section, and the run continues for other councils |
| **Large PDFs** (25–67 MB agendas) | High | Prefer minutes / HTML (Infocouncil) / `_ATT_EXCLUDED`; 40 MB opt-in threshold and 80 MB hard cap; conditional GET so large files are downloaded at most once; streaming abort; PyMuPDF page-by-page processing to limit memory |
| **Sparse budgets** (Stirling, Wanneroo, Rockingham give only totals) | Certain | Project detail from agenda items (M11); program totals as context only; the demo bar is met by Joondalup/Melville/Cockburn annual docs |
| **PDF extraction failures** (layout drift, wrapped columns, image-only pages) | Medium | Word-box parser → `pdftotext -layout` fallback → flag `value_ambiguous_column`; image-only pages flagged `ocr_needed` with optional Tesseract (`--ocr`); `extract_status=partial/failed` surfaced in "Needs review"; the extractor never invents values (null + flag) |
| **Joondalup table layout differs from the research sample** | Medium | Header detection is driven by header text, not fixed coordinates; the M9 spot-check is part of done-when; the fallback still yields code + name + total |
| **WAF/challenge or 403** (Sucuri on Stirling, Cloudflare) | Low–medium | Pause host 24 h, report it, never bypass; the other councils continue |
| **Rule false positives** (e.g. "path" in parks, court resurfacing) | Medium | Negative keywords, context-required tokens, gold-set metric, `trades-report` tuning, confidence shown |
| **Over-merging in dedupe** | Medium | Fuzzy never auto-merges; trade-intersection guard on segment keys; `review_needed` list |
| **WeasyPrint install/render failure** | Low (pango present here) | PyMuPDF `Story` fallback; HTML is always produced |
| **Licensing / terms** (Cockburn, Melville non-commercial; Rockingham permission needed) | Business risk | v1 private pilot; facts + links + ≤1-sentence quotes only; no document mirroring; attribution footer; permission emails before any paid launch (R§3.2) |
| **Main Roads licence unverified** | — | Black Spot disabled by default, used only to tag council projects, caution in footer |
| **Stale data / meeting cadence** | Low | Digest shows "last retrieved" per council; Perth deferred until fresh |
| **Undated or mis-dated docs** | Medium | Date from listing metadata first, then filename, then title; `meeting_date` null → flagged and excluded from "this week" grouping |

---

## 14. Build delegation

Follow this table during the build. "opus" is reserved for design-heavy or layout-parsing work; "sonnet" for standard features; "haiku" for boilerplate, fixtures and docs.

| Milestone | Model | Rationale |
|---|---|---|
| M0 Skeleton & config (pyproject, .gitignore, councils.yaml, trades.yaml) | **haiku** | Boilerplate and config YAML transcribed directly from this plan and RESEARCH.md |
| M1 Data model & SQLite schema | **sonnet** | Straightforward schema from §4.1, but idempotent upserts and deterministic IDs need care |
| M2 Polite fetcher + robots + rate limit | **sonnet** | Standard HTTP work, but compliance-critical edge cases (redirect re-check, 401/403 robots, streaming cap) need a careful implementer |
| M2 tests (local HTTP server fixture, network guard) | **haiku** | Test scaffolding against a clearly specified behaviour list |
| M3 Text layer (PyMuPDF words, HTML blocks, OCR detection) | **sonnet** | Library integration with well-known APIs |
| M4 Normaliser (money, roads, FY, segments) | **sonnet** | Regex-heavy but well specified; table-driven tests define it |
| M4/M5 table-driven test cases and gold CSV seed | **haiku** | Writing fixture rows from RESEARCH examples is mechanical |
| M5 Trade matcher | **sonnet** | Rules engine from config with negatives, context and implied labels: standard logic |
| M6 Listing adapters A (static, Cockburn HTML, Infocouncil) | **sonnet** | HTML scraping of server-rendered lists |
| M7 Listing adapters B (Kentico ksearch, Joondalup htmlresult) | **sonnet** | Request shapes are fully documented in RESEARCH; per-site config mapping |
| M6/M7 fixture recording and contract tests | **haiku** | Trimming recorded responses and asserting key presence is boilerplate |
| M8 Melville CWP extractor | **sonnet** | Simple HTML + regex extraction |
| M9 Joondalup budget table parser | **opus** | Tricky PDF table parsing: word-box column inference, wrapped-description re-joining, fallback strategy |
| M10 Cockburn project plans extractor | **sonnet** | Labelled fields and segment regex; moderate layout variance. Escalate to opus only if the budget-table layout proves irregular |
| M11 Agenda/minutes item extractor | **opus** | Extraction design across varied council templates: segmentation, stage heuristics, in-document recommendation/resolution dedupe |
| M12 Dedupe/linker | **opus** | Architecture trade-offs in the key ladder, cross-key conflicts and merge safety; mistakes corrupt the system of record |
| M13 Change tracking | **sonnet** | Snapshot diff over a well-defined state model |
| M14 Digest HTML + PDF | **sonnet** | Jinja2 templating and WeasyPrint/Story fallback wiring |
| M15 Web UI (static, vanilla JS) + export | **sonnet** | Standard front-end filtering/table code with no build step |
| M16 `run` orchestration + live demo + sample `out/` | **sonnet** | Glue code plus a careful, rate-limited live run and result checking |
| M16 README "how to run", CLI help polish | **haiku** | Documentation polish |
| M17 Black Spot extractor (stretch) | **sonnet** | Simple line-pattern PDF extraction plus tagging |
| M18 Sitemap fallback + statutory totals (stretch) | **sonnet** | Standard XML parsing and a small budget-total extractor |
| Cross-cutting test review before demo | **haiku** | Filling coverage gaps and fixture tidy-up against the §11 checklist |
