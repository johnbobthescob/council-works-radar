# WA Council Works Radar: Research

**Retrieval date for all live observations:** 2026-11-22 (AWST). Unless a section says otherwise, every URL below was fetched on that date with plain HTTP (`curl`). I used a descriptive User-Agent (`CouncilWorksRadar-research/0.1; manual research`) and waited at least 2 s between requests. No logins, CAPTCHAs or bot walls were bypassed. Where a fetch failed, the error is recorded.

**Scope:** research only. No application code has been written.

---

## 0. Executive summary

* **Seven Perth-metro councils can be fetched with ordinary HTTP and robots.txt allows it:** Joondalup, Wanneroo, Stirling, Melville, Cockburn, Rockingham and Perth. All seven publish born-digital, text-extractable budget PDFs and direct-link agenda/minutes PDFs. The listings come in four flavours:
  - server-rendered HTML (Cockburn, Perth)
  - an Infocouncil portal (Wanneroo)
  - a public JSON endpoint that the council's own listing page calls (Stirling, Melville, Rockingham)
  - a JSON/HTML search endpoint (Joondalup)
* **Five of the suggested councils are blocked or should be deferred:**
  - **Canning:** every document is served from `cdn.canning.wa.gov.au`, whose robots.txt is `Disallow: /`.
  - **Swan:** the listing API sits under `/v1/`, which robots.txt disallows.
  - **Gosnells:** Akamai returns `403 Access Denied` to a plain HTTP client, even for robots.txt.
  - **Armadale:** a React/Contentful SPA. The 8 MB agendas page contains no agenda/minutes links in the HTML.
  - **Fremantle:** a WordPress JSON listing endpoint that needs parameters I did not reverse-engineer. Kept as a reserve.
* **Only some budgets list individual projects.**
  - **Joondalup's** 2026-27 budget PDF has a full **project-level capital works schedule**: project number, cost code, project name, description, suburb, ward, and funding split (Municipal / Reserve / Trust / Grants New / Grants C/fwd / Contribution / Brought Fwd / Loan / Total).
  - **Melville** publishes an **HTML Capital Works Program 2026-2027 page** with per-project dollar values and funding notes, such as "Black Spot".
  - **Cockburn** publishes a separate **"Project Plans 2026-2027" PDF** with per-project budgets and named road segments in its resurfacing program.
  - The other statutory budgets (Stirling, Wanneroo, Rockingham, Perth) give totals by asset class. For those councils, project detail has to come from agenda items: tender award reports, budget amendments and capital works reports.
* **Tenders WA is not usable as a scraping source, and it doesn't cover councils anyway.**
  - `tenders.wa.gov.au/robots.txt` disallows everything except `/watenders/index.do`.
  - The site's copyright terms forbid reuse "for any commercial purpose".
  - Its open-data substitute is the annual *"Tenders WA contract award details"* CSVs on data.wa.gov.au (CC BY 4.0). That covers **state agencies only**: zero local governments among the 5,817 rows for 2023-24. The latest year published is 2023-24.
  - Councils advertise tenders on their own portals: TenderLink (Joondalup, Melville, Canning), eTenderBox (Wanneroo), SL360 (Cockburn and apparently Stirling), and Rock Port (Rockingham, login needed).
* **Main Roads WA's Black Spot program PDFs are a strong free cross-reference.** They list project-level local-road works by council, with approved budgets. For example, the 2026-27 State Black Spot (local roads) list includes "Preston Point Road-Stock Road (City of Melville)" and "Hartman Drive SLK 3.52 - 3.84 (City of Wanneroo)".
* **Licensing is the main business risk, not the technology.**
  - Every council site I checked asserts "all rights reserved" copyright.
  - Cockburn, Melville and Perth explicitly limit reuse to personal or non-commercial use. Rockingham requires written permission for any reproduction.
  - Factual data points (project, location, value, date) with a link back are much lower risk than republishing documents, but a paid product should seek written permission. See §3.
* **Competitors:** Cordell Connect (Cotality, formerly CoreLogic) is the incumbent. Its Lite tier is $577.50/month for one state with a 12-month minimum. Tender aggregators such as TenderLink ($95–$875/month) and Australian Tenders ($67–$145/month) only catch *published tenders*. None of the products I could verify markets itself on **pre-tender signals from WA council budgets and minutes**, which is the niche here.

---

## 1. Councils and document portals

### 1.1 Candidate screening (all 12 suggested councils)

| Council | robots.txt (fetched) | Listing mechanism | Verdict |
|---|---|---|---|
| **Joondalup** | Allows all except `/admin/`, `/v1/`, Kentico admin paths. `api.joondalup.wa.gov.au/robots.txt` returns 404 (no restrictions) | Kentico site. Listing via `POST /search/htmlresult` (JSON in, HTML fragment out). PDFs on `api.joondalup.wa.gov.au/files/councilmeetings/YYYY/...` | **Include (rank 1)** |
| **Wanneroo** | Allows all except `/admin/`, `/cmshelp/`. `wanneroo.infocouncil.biz/robots.txt` is a Cloudflare "content signals" preamble with no Disallow rules | Page iframes **Infocouncil** (`wanneroo.infocouncil.biz`). Server-rendered list with PDF and HTML versions | **Include (rank 2)** |
| **Stirling** | Allows all except `/admin/`, `/v1/`, Kentico admin | Kentico + Kendo. Listing JSON via `POST /ksearchmna/ksearchresult` | **Include (rank 3)** |
| **Melville** | Allows all except `/admin/`, `/cmshelp/` | Kentico + Kendo. Listing JSON via `POST /ksearch/mnaresult`. HTML capital works page | **Include (rank 4)** |
| **Cockburn** | Allows all except `/admin/`, `/cmspages/`, `/cmsdesk/`, test areas | Server-rendered HTML document list with direct `getattachment` PDF links | **Include (rank 5)** |
| **Rockingham** | Allows all except `/admin/`, `/cmspages/`, `/cmsdesk/` | Kentico + Kendo. Listing JSON via `POST /ksearch/mnaresult`. Budget page has direct links | **Include (rank 6)** |
| **Perth** | Disallows `/upload/`, `/temp/`, `/sitecore*` and similar. Documents live under `/-/media/...` (allowed) | Sitecore. Static per-year HTML pages with direct PDF links | **Include (rank 7)**, with a freshness caveat (§1.2.7) |
| Canning | `www` robots allows all. **`cdn.canning.wa.gov.au/robots.txt` = `User-agent: * / Disallow: /`** | Umbraco. Listing JSON is embedded in the page, but every `urlPdf` points at the disallowed CDN | **Exclude from automation** until written permission is obtained |
| Swan | Disallows `/v1/` | Listing loads from `/v1/aapi/search/htmlresult`, **which robots.txt disallows**. No document links in the server HTML | **Exclude** (robots) |
| Gosnells | `https://www.gosnells.wa.gov.au/robots.txt` → **HTTP 403 "Access Denied"** (Akamai, ref `#18.7115d617.1795334896.610b1bba`). Homepage also 403 | n/a | **Exclude** (bot wall; not bypassed) |
| Armadale | `www.armadale.wa.gov.au` redirects to `my.armadale.wa.gov.au/service/`. robots allows all | React/Contentful SPA. `/service/agendas-and-minutes/` is 8.1 MB of HTML with no agenda/minutes PDF links. Only a couple of `assets.ctfassets.net` PDFs (rates booklet, tip fees) | **Defer** (JS-only listing) |
| Fremantle | WordPress/Yoast. Allows all except `/wp-admin/` and taxonomy paths | Listing via `POST /wp-json/aw/v1/minutes-and-agendas`. An empty-body POST returned `404 {"message":"No posts found "}`, so parameters are needed | **Reserve** (probably feasible; not verified) |

### 1.2 Per-council detail (selected seven)

All example URLs below were checked on 2026-11-22. A ranged GET returned `206` with `application/pdf` and a `%PDF-` magic header, or HTML where stated.

#### 1.2.1 City of Joondalup (rank 1)

* **Website:** https://www.joondalup.wa.gov.au/
* **Agendas/minutes page:** https://www.joondalup.wa.gov.au/city-and-council/council-and-committee-meetings/council-agenda-and-minutes (also `.../committee-agendas-and-minutes`)
* **Listing mechanism:** the page is a Kentico "PageTypeListing" widget (`data-api="/search/htmlresult"`, `data-searchindex="CouncilMeeting"`). A POST with JSON body:
  ```json
  {"SI":"CouncilMeeting","OB":"","Q":"","PS":24,"FS":null,"PG":1,
   "PR":[{"key":"widgetclassname","values":[{"value":"AWCouncilMeetingSmartSearchListing"}]},
         {"key":"widgettemplatename","values":[{"value":"AWCouncilMeetingSmartSearchListing"}]},
         {"key":"classname","values":[{"value":"AWPT.CouncilMeeting"}]},
         {"key":"nodealiaspath","values":[{"value":"/"}]}],
   "IncludeFirst":false}
  ```
  It returns `{"htmlResult": "...", "suggestion": ...}` with `recordCountValue=840`. Results come oldest first (2000 → 2026), and page 35 at size 24 held the 2026 items. My first attempt with plain-string `values` returned HTTP 400. No token or cookie was needed.
* **Document host:** `https://api.joondalup.wa.gov.au/files/councilmeetings/<YYYY>/<filename>.pdf?v=1`.
  - Older files follow `CJyyMMdd_{AGN|MIN|BRF}.pdf` (e.g. `2018/CJ181218_MIN.pdf`).
  - **2026 filenames are free text**, e.g. `27 Oct 2026 - Council Agenda Inc Attachments.pdf`, `8 Septembe 2026 - Briefing Session - Agenda inc att.pdf` (typo in original). So URLs must be discovered from the listing, not guessed.
* **Also on the page:** "Search Council Agendas and Minutes" iframes `https://search.joondalup.wa.gov.au/docs/embed.html`. That host's robots.txt returned `401 {"error":"Unauthorized","message":"Authentication required"}`, so **don't use it**. A "Council Resolution Register" links to `https://joondalup.docassembler.com.au/#resolutionregister` (not fetched).
* **Budget:** found through the publications search (`SI":"DocPublications"`, keyword `budget`).
  - **2026-27:** https://www.joondalup.wa.gov.au/awcontent/Web/Documents/Budget,%20Fees%20and%20Financial%20Reports/Budget-2026-27.pdf (3.16 MB, 69 pages, "Power PDF Create", text-extractable).
  - Earlier: `.../Budget-2024-25.pdf`, `.../Budget-2023-24.pdf`, `.../City-of-Joondalup-Budget-2022-23-Final.pdf`, and so on.
  - Budget news: https://www.joondalup.wa.gov.au/city-and-council/latest-news-updates/council-approves-balanced-and-forward-focused-2026-27-budget
* **Budget content (the main finding):** the capital works schedule has the header `Project Number | Cost Code | Team | Project Name | Description | Suburb | Ward | Municipal | Reserve | Trust | Grants New | Grants Cfwd | Contribution | Brought Fwd | Loan | Expenditure`. Example rows:
  - `RPR3542  W5033  621  Warwick Road (westbound)  ... Resurfacing and rehabilitation works ...  Warwick  South-East  135,455 ... 270,909 ... 406,364`
  - `RDC2032  W4530  621  Eddystone Ave - Joondalup to Honeybush  ... Joondalup  North  509,102 ... 750,000 ...`
  - The word "resurfac" appears 93 times. There are prefix families: `RPR` (road preservation/resurfacing), `RDC` (road construction), `PEP` (parks equipment), and others.
* **Example agenda:** https://api.joondalup.wa.gov.au/files/councilmeetings/2026/27%20Oct%202026%20-%20Council%20Agenda%20Inc%20Attachments.pdf?v=1 (28.3 MB, `Last-Modified: Fri, 16 Oct 2026`, ETag present, IIS)
* **Example minutes:** https://api.joondalup.wa.gov.au/files/councilmeetings/2026/27%20October%202026%20-%20Council%20-%20Minutes%20inc%20Attach.pdf?v=1
* **Formats:** searchable PDF. Agendas include attachments, so files are large (tens of MB).
* **Cadence:** a Briefing Session, then a Council meeting, roughly monthly. 2026 dates include 9 Jun Briefing, 28 Jul Council, 8 Sep Briefing, 27 Oct Council, and 8 Dec Council (in the sitemap).
* **Obstacles:** the listing needs a JSON POST, which is the same call the public page makes. Free-text filenames. Big files.

#### 1.2.2 City of Wanneroo (rank 2)

* **Website:** https://www.wanneroo.wa.gov.au/
* **Agendas/minutes page:** https://www.wanneroo.wa.gov.au/city-and-council/council,-committees-and-meetings/minutes-and-agendas, which iframes **Infocouncil**: https://wanneroo.infocouncil.biz/
* **Listing:** a server-rendered HTML table on `wanneroo.infocouncil.biz/` (206 KB) with links of the form `RedirectToDoc.aspx?URL=Open/<YYYY>/<MM>/<MEETING>_<DDMMYYYY>_<AGN|MIN>_AT[_SUP][_WEB.htm|.PDF]`. Meeting codes seen: `OC` (Ordinary Council), `BSS`, `PRCCC`, `CPRCC`. There are also `_ATT_EXCLUDED` variants (attachments split out).
* **Infocouncil HTML versions:** `..._AGN_AT_WEB.htm` is a frameset that points to `..._AGN_AT.HTM` (the paper) and `..._AGN_AT_BMK.HTM` (bookmarks/navigation). The paper HTML for OC 24 Nov 2026 was 6.2 MB and reduced to about 505 KB of clean text. Agenda item IDs look like `AS01-11/26` ("Tender No. 26173 The Supply ..."). **Parsing structured HTML is easier than parsing PDFs**, which is a real advantage.
* **Budget:** budget news page https://www.wanneroo.wa.gov.au/city-and-council/latest-news/budget-2026-27 links to:
  - Statutory budget: https://www.wanneroo.wa.gov.au/getContentAsset/3aeb5e3f-84a5-46b1-80a2-b53f979c4125/3ca954ad-3848-47c6-8f97-68cebe0b47a2/2026_2027_Statutory_Budget.PDF?language=en (1.23 MB, 35 pages, produced from Excel, text-extractable)
  - Long Term Financial Plan: `.../Long_Term_Financial_Plan202627to203536.PDF`
  - Council Plan: `.../Council-Plan.pdf`
* **Budget content:** asset-class totals only (e.g. Roads 91,055,501; Drainage 38,152,282). Few project names. Project detail has to come from agendas.
* **Example agenda:** https://wanneroo.infocouncil.biz/RedirectToDoc.aspx?URL=Open/2026/11/OC_24112026_AGN_AT.PDF (66.5 MB, Cloudflare, ETag and Last-Modified present). HTML version: https://wanneroo.infocouncil.biz/RedirectToDoc.aspx?URL=Open/2026/11/OC_24112026_AGN_AT_WEB.htm
* **Example minutes:** https://wanneroo.infocouncil.biz/RedirectToDoc.aspx?URL=Open/2026/10/OC_27102026_MIN_AT.PDF
* **Cadence:** monthly Ordinary Council (27 Oct, 24 Nov 2026) plus committees and briefings.
* **Tender portal:** https://wanneroo.eTenderBox.com.au/Supplier/Home.aspx (not fetched). The council site also has a historical per-tender page set under `/business-and-investment/tenders-and-contracts/tenders/<no>-<slug>`, visible in the sitemap.
* **Obstacles:** very large PDFs, so prefer the HTML. The Infocouncil robots.txt carries Cloudflare's content-signal legal preamble, which reserves rights for uses where a signal is set. No signals or Disallow lines were present, so use is neither granted nor restricted by signal.

#### 1.2.3 City of Stirling (rank 3)

* **Website:** https://www.stirling.wa.gov.au/ (Sucuri CloudProxy in front)
* **Agendas/minutes page:** https://www.stirling.wa.gov.au/city-and-council/about-council/council-and-committee-meetings/minutes-and-agendas. The page is a Kendo template (`#:AgendaName#`), so there are no links in the HTML.
* **Listing JSON:** `POST https://www.stirling.wa.gov.au/ksearchmna/ksearchresult`
  - Header `SearchParam: [{"key":"classname","values":["AWPT.MNA"],...},{"key":"searchindexes","values":["MNA"],...},{"key":"nodealiaspath","values":["/City-and-Council/About-Council/Council-and-committees/Minutes-and-agendas"],...}]`
  - Header `SearchFilter: [{"key":"Sort","values":["ArticleReleaseDate DESC"],"or":"true","operator":""}]`
  - Body `{"take":12,"skip":0,"page":1,"pageSize":12}`
  - Returns `{"Data":[...],"Total":279}`. Each record has `ArticleTitle`, `ArticleReleaseDate`, `Agenda`, `MinutesFile`, `NoticeOfMotion` and an `Attachments[]` array. This is the most structured listing of the seven.
  - Filters exposed in page markup: `ArticleReleaseYear` (2021–2026), `MeetingType` IDs (e.g. Community and Resources Committee = 1060), `MeetingCategories`.
* **Budget:** "Budget statements" section on https://www.stirling.wa.gov.au/city-and-council/about-council/governance-and-transparency/annual-reports-and-financial-statements
  - **Statutory Budget 2026-2027:** https://www.stirling.wa.gov.au/awcontent/Web/Documents/City%20and%20Council/Governance%20and%20transparency/Annual%20Reports%20and%20Financial%20Statements/City-of-Stirling-Statutory-Budget-2027.pdf (2.0 MB, 61 pages, Word-generated, text-extractable)
  - 2025-26: `.../2025-26-Statutory-Budget_FINAL_inc-fees-and-charges_11082025.pdf`
  - Budget newsletter page: https://www.stirling.wa.gov.au/city-and-council/document-and-publications/budget-newsletter-2026-27
* **Budget content:** program-level lines ("Road Renewal Program 19,502,936 / 884,566 / 20,387,502", "Footpaths Program 2,401,805", "Drainage Program 4,843,098") and a few named items ("Hutton Street Extension (3,800,000)"). No per-street list.
* **Example agenda:** https://www.stirling.wa.gov.au/getmedia/11cb4985-d185-435a-aa2c-af1d7e0ea512/Council-Agenda-17-November-2026.pdf
* **Example minutes:** https://www.stirling.wa.gov.au/getmedia/bc87f6d5-383c-46c3-b4c6-9ece60b0be18/Council-Minutes-17-November-2026.pdf (8.6 MB, 188 pages, Aspose.PDF, text-extractable)
  - Includes a full tender report ("That the tender from Kardan Australia Pty Ltd ... for 9 Leeway Court, Osborne Park Roof and HVAC ... $1,064,854 (ex GST)", "Tender Deadline: Monday 21 September 2026").
* **Cadence:** Council meets about every 2–3 weeks (20 Oct, 3 Nov, 17 Nov 2026). Committees sit in between: Community and Resources, Planning and Development, ARIC.
* **Other pages:** tender register https://www.stirling.wa.gov.au/business-and-investment/doing-business-with-the-city/tender-register. Current tenders page references an "SL360 Public Portal" (Simply Logical).
* **Obstacles:** Sucuri WAF (no challenge seen at a polite rate). Kendo JSON listing.

#### 1.2.4 City of Melville (rank 4)

* **Website:** https://www.melvillecity.com.au/ (Cloudflare)
* **Agendas/minutes page:** https://www.melvillecity.com.au/our-city/our-council/agendas-and-minutes. Every meeting also has its own HTML page, e.g. `/our-city/our-council/agendas-and-minutes/agenda-ordinary-meeting-of-council-17-november-2026` (listed in sitemap.xml). **The sitemap alone is a usable discovery feed.**
* **Listing JSON:** `POST https://www.melvillecity.com.au/ksearch/mnaresult` with header `SearchParam: [{"key":"classname","values":["AWPT.MNA"],...},{"key":"searchindexes","values":["MNA"],...}]`. Returns `MNAFirstFileName`, `MNAFirstFileUrl`, `MNAMeetingType`, `MNAFirstFileSize` and `PageUrl`.
* **Budget page:** https://www.melvillecity.com.au/our-city/city-management/financial-information/budget
  - **Budget Document 2026-2027:** https://www.melvillecity.com.au/getContentAsset/8c0a400e-cb96-4604-98e1-befd824a6285/3ca954ad-3848-47c6-8f97-68cebe0b47a2/26-130-Budget-Document-_-WEB-READY.pdf?language=en (11.8 MB, 76 pages, InDesign, text-extractable but design-heavy)
  - Back-years 2020-21 to 2025-26 are on the same page.
* **Capital Works Program page (HTML):** https://www.melvillecity.com.au/our-city/about-our-city/capital-works-program. It has per-project entries with value and funding source, for example:
  - "Ardross Street and Munro Road Roundabout Installation ($700,000) External funding secured under the Black Spot funding program"
  - "Norma Road and McCoy Street Roundabout ($550,000) ... Black Spot"
  - "Morris Buzzacott ... car park renewal ($700,000)"
* **Example agenda:** https://www.melvillecity.com.au/getContentAsset/4c1124ba-2322-45d8-8cc2-ebbbb8fedd46/3ca954ad-3848-47c6-8f97-68cebe0b47a2/Agenda-8-December-2026-OMC.pdf?language=en (51.5 MB)
* **Example minutes:** https://www.melvillecity.com.au/getContentAsset/21ed1f23-57da-4b53-82a9-79b75dcad9b2/3ca954ad-3848-47c6-8f97-68cebe0b47a2/Minutes-17-November-2026-OMC.pdf?language=en
* **Cadence:** monthly Ordinary Meeting of Council, plus Agenda Briefing Forum, DAU, and ARIC.
* **Tender portal:** TenderLink (`http://www.tenderlink.com/melville`, not fetched).
* **Terms:** the strictest wording among the targets (see §3).

#### 1.2.5 City of Cockburn (rank 5)

* **Website:** https://www.cockburn.wa.gov.au/ (Cloudflare, Kentico)
* **Agendas/minutes page:** https://www.cockburn.wa.gov.au/City-and-Council/About-Council-Meetings-and-Elections/Agendas-and-Minutes. **Server-rendered HTML** with direct links and file sizes. Query-string filtering works: `?page=1&keyword=OCM|Ordinary%20Council%20Meeting&titleonly=true&years=2026`.
* **Document URL pattern:** `/getattachment/<GUID>/ECM_<ecm-id>_v<n>_<Title>-pdf.aspx`. The ECM record ID and version number in the filename are useful for dedupe and versioning.
* **Budget:**
  - Strategies-and-Plans page: https://www.cockburn.wa.gov.au/Strategies-and-Plans
  - **Annual Budget 2026-2027:** https://www.cockburn.wa.gov.au/getattachment/eaa1f7d8-69b9-4c8f-b3b9-ac533c2ea170/ECM_12777460_v1_Annual-Budget-2026-2027-pdf.aspx (6.45 MB, 103 pages, Word-generated)
  - **Project Plans 2026-2027:** https://www.cockburn.wa.gov.au/getattachment/91e79b42-247f-4c86-a1bd-050ad2cf769f/ECM_12774885_v2_Project-Plans-2026-2027-pdf.aspx (8.7 MB, 100 pages). Per-project sheets with ID (e.g. `4C.18`), directorate, total budget, current-year budget, and start/end dates. The "Roads Resurfacing Program" lists named segments such as "Elderberry Dr – South Lake to Berrigan Dr", "Warton Rd – Jandakot Rd to Nicholson Rd" and "Beeliar Drive – Wentworth to Midgeegooroo Ave".
  - Also on that page: Service Plans 2026-2027 and Corporate Business Plan 2026-2030.
  - Budget highlights (HTML): https://www.cockburn.wa.gov.au/City-and-Council/Rates/Budget-Highlights-2026-27 ("Major projects scheduled for 2026-27" section)
* **Example agenda:** https://www.cockburn.wa.gov.au/getattachment/a5cf4eca-728c-4e5c-ae81-32e01d71c0fd/ECM_12928826_v1_Ordinary-Council-Meeting-Agenda-10-November-2026-pdf.aspx (46.2 MB)
* **Example minutes:** https://www.cockburn.wa.gov.au/getattachment/a9c882fe-03ab-4938-bc8d-53ceab8e719b/ECM_12915695_v3_Ordinary-Council-Meeting-Minutes-13-October-2026-pdf.aspx (31.3 MB)
* **Cadence:** monthly OCM (13 Oct, 10 Nov 2026), plus ARIC and special meetings.
* **Tender portal:** `https://public.sl360.au/request/open/cockburnprod` (not fetched).
* **Obstacles:** none technical. Agendas are very large (40+ MB).

#### 1.2.6 City of Rockingham (rank 6)

* **Website:** https://rockingham.wa.gov.au/
* **Agendas/minutes page:** https://rockingham.wa.gov.au/your-city/council/council-and-committee-meetings/agendas-and-minutes (Kendo template)
* **Listing JSON:** `POST https://rockingham.wa.gov.au/ksearch/mnaresult` (same `SearchParam` header as Melville). Returns `MNATitle`, `MNAFileUrl`, `MNADocumentType` (Agenda / Minutes / Report Attachments / Bulletins), `MNAMeetingType`, `MNAFileSizeByte` and `MNAPublicationDate` (`20261119`).
* **Budget page:** https://rockingham.wa.gov.au/your-city/about-us/budget-and-finance
  - **Annual Budget 2026-2027:** https://rockingham.wa.gov.au/getContentAsset/0399c61d-9a71-4817-9f9d-29d207082d86/3ca954ad-3848-47c6-8f97-68cebe0b47a2/Annual-Budget-2026-2027.pdf?language=en (8.3 MB, 100 pages, Word-generated)
  - "May 2026 Business Plan" (10.6 MB) and a Budget Newsletter are on the same page.
* **Budget content:** account-code style (`06050 - Infra - Drainage 1,541,125`). Project detail is limited.
* **Example agenda:** https://rockingham.wa.gov.au/getContentAsset/bf814a1d-f809-401e-8895-48da3a720111/3ca954ad-3848-47c6-8f97-68cebe0b47a2/2026-11-(Nov)-Agenda-Ordinary-Council.pdf?language=en (4.9 MB, the smallest of the set)
* **Example minutes:** https://rockingham.wa.gov.au/getContentAsset/59a532a4-02b5-43c1-a892-c9a3e0b17a7e/3ca954ad-3848-47c6-8f97-68cebe0b47a2/2026-10-(Oct)-Minutes-Ordinary-Council.pdf?language=en
* **Also published:** a separate "Planning and Asset Services Committee" agenda/minutes each month. That committee is where engineering and road reports land, so it is high value. There's also a monthly "Information Bulletin".
* **Tender portal:** "Rock Port" portal (account required for documents).
* **Terms:** explicit "must not be reproduced, reused, or redistributed without prior written permission".

#### 1.2.7 City of Perth (rank 7)

* **Website:** https://perth.wa.gov.au/ (Sitecore)
* **Meetings:** static per-year pages, e.g. https://perth.wa.gov.au/council/council-meetings/2026-council-meetings, with direct PDF links under `/-/media/Project/COP/COP/COP/Documents-and-Forms/Council/Documents/Council-Meetings/Agendas-and-minutes/<YYYY>/<MM>---<Month>/<file>.pdf`.
  - Example: `.../2026/06---June/Minutes---OCM-30-June-2026-signed.pdf`.
  - **Caveat:** at retrieval the 2026 page only listed January–July 2026, with nothing for August–November. Either the page lags or later meetings are published elsewhere; `search-meetings` had no links in its HTML. Freshness needs checking before Perth goes into v1.
* **Budget page:** https://perth.wa.gov.au/council/budget
  - **Annual Budget 2026/27:** https://perth.wa.gov.au/-/media/Project/COP/COP/COP/Documents-and-Forms/Council/Documents/Reports-and-Important-Documents/Annual-Budgets/Budget-2026-27/Annual-Budget-2026-27.pdf (3.5 MB, 69 pages, text-extractable)
  - Includes some named capital items ("PCEC Car Park Resurfacing (1,780,000)", "Roads, Paths & Kerb Renewals $6.5M", "Asset Renewal - Paths & Kerbs 455,000").
* **Open data:** "City of Perth Capital Projects Outlook" (`perth-xb-capital-works-app`) on data.wa.gov.au, **CC BY 4.0**, resource `https://geohub-perth.opendata.arcgis.com/apps/perth::xb-capital-works-app`. The underlying ArcGIS feature service was not inspected yet. It is likely queryable as JSON.
* **Major projects:** https://perth.wa.gov.au/building-and-planning/future-perth/major-projects
* **Terms:** personal / non-commercial only. No "deconstruction" of documents (see §3).
* **Fit:** a CBD-centric works mix. It's lower priority for suburban resurfacing, footpath and kerbing contractors.

#### 1.2.8 City of Canning (blocked by robots on document host; strong otherwise)

The Canning listing is the best-designed of all twelve, so this is a frustrating exclusion.

* `https://www.canning.wa.gov.au/agendas-and-minutes/` embeds `<script id="SearchResult" type="application/json">` with `totalItems: 608` and `urlPdf` per item.
* Finance page `https://www.canning.wa.gov.au/about-us/council-business/finance/` links annual budgets back to 2016-17.
* **But every document URL is on `cdn.canning.wa.gov.au`, whose robots.txt is `Disallow: /`.**
* **Disclosure:** before I fetched that robots.txt, I had already downloaded the 2026-27 budget PDF (`https://cdn.canning.wa.gov.au/media/bwzj3z1j/annual-budget-2026-2027-final.pdf`, 67 pages) and made two 2 KB range checks. Afterwards I mistakenly made one more HEAD request while checking headers across councils. No further Canning CDN fetches will be made.
* **Recommendation:** email the City of Canning to ask whether automated retrieval of public council documents is permitted. Until then, list Canning meetings as metadata only (title, date, link) from the `www` page, and let users click through.

### 1.3 Cross-cutting observations

* **Platform clusters.** Stirling, Joondalup, Melville, Rockingham and Wanneroo (main site) all run the same Kentico build. They share `aw_*` JS modules and the `getContentAsset/<guid>/3ca954ad-.../file.pdf` URL shape. One adapter with a per-site config (endpoint path, `SearchParam`) covers four listing endpoints. Swan uses the same vendor stack but puts its API under robots-disallowed `/v1/`.
* **Formats.** All eight budget PDFs I downloaded are born-digital. Producers: Word, Excel, InDesign, Power PDF, Acrobat Distiller, Aspose. `pdftotext -layout` gave 177k–957k characters each. **No scanned PDFs were found** in the sample, though older agenda attachments (plans, signed letters) may be scanned.
* **Change detection.** Every document host returned `ETag` and/or `Last-Modified` (Stirling, Wanneroo, Joondalup, Cockburn, Melville, Rockingham, and Canning, though the last is excluded). So conditional GETs (`If-None-Match` / `If-Modified-Since`) can avoid re-downloading 30–60 MB agendas.
* **File sizes.** Agendas with attachments run 25–67 MB, and Canning "Large Attachments" go up to 128 MB. That calls for a size cap, an attachments-excluded variant where one exists (Wanneroo `_ATT_EXCLUDED`, Canning separate attachments), and Infocouncil HTML in preference to PDF.
* **Sitemaps.** All seven `www` sites publish `sitemap.xml`. Melville's includes every meeting page and Joondalup's includes meeting event pages. They are useful as a cheap change feed.
* **No RSS** feeds for agendas were found on any target (not exhaustively searched).

---

## 2. Western Australian state sources

### 2.1 Tenders WA

* **Portal:** https://www.tenders.wa.gov.au/watenders/index.do. It is still the official WA Government portal, run by the Department of Treasury and Finance (Procurement Business Systems).
* **robots.txt:**
  ```
  User-agent: *
  Allow: /watenders/index.do
  Disallow: /
  ```
  **All search, tender detail and contract pages are off-limits to automated agents.**
* **Access model (from the homepage):**
  - Public menu items exist for Advertised, Early Tender Advice, Closed and Under Evaluation, Results, New This Week, Contract Search and Recently Awarded. All are `.action` URLs carrying a per-session `CSRFNONCE` token.
  - Login (with email token 2FA) is needed for registered-business features such as document download and lodgement.
  - The homepage links a "Procurement Data Portal" at `https://gpreports.dtf.wa.gov.au/irdclient/login.aspx`, which is a login page.
* **Fields visible without an account:** not assessed by crawling, because robots disallows it. A human can browse them manually.
* **Terms** (I read `copyright.vm`, `terms-and-conditions.vm` and `about.vm` once each as a human-equivalent compliance check. They sit under the disallowed path, so the pipeline must not fetch them):
  - "Copyright in this Website ... is vested in the State of Western Australia. Apart from any fair dealing for the purposes of private study, research ... **no part of this Website, Application or any Material in either of them may be reproduced or re-used for any commercial purpose.**"
  - Application-specific conditions: users "must not ... make money out of its use" (last modified August 2023).
* **Official API / RSS:** none found. Email alerts exist for registered users.
* **Open-data substitute (usable):** data.wa.gov.au (CKAN 2.11.5) has **"Tenders WA contract award details"** for 2021-22, 2022-23 and 2023-24. Publisher: Department of Treasury and Finance. Licence: **CC BY 4.0**. Metadata modified 2026-09-18.
  - 2023-24 CSV: 5,817 rows, 4.6 MB. Fields: `Public_Authority, Public_Authority_Address, Title, Reference_Number, Description, Type_of_Work (Goods & Services | Works), Procurement_Method, UNSPSC codes ×3, region flags (Perth_Metropolitan, Peel, …), Number_of_Submissions, Panel_Contract, Contract_Term, Closing_Date, Award_Date, Commencement_Date, Expiry dates, Original_Contract_Value, Supplier_Name, Supplier_Address`.
  - **No local governments appear as Public_Authority.** The top authorities are Health, Finance, Main Roads (643 rows, e.g. "Provision of asphalt resurfacing on Tonkin Hwy ... $300,000") and Public Transport Authority.
  - It is useful as **market context** (who wins state road resurfacing work, at what values) but **not a council source**. It also lags by more than two years.
  - Download URLs redirect (302) to time-limited signed S3 URLs, so always start from the CKAN resource URL.
* **Why it matters less than expected:** WA councils run their own tender processes under the Local Government (Functions and General) Regulations 1996 and advertise on their own portals:

  | Council | Portal |
  |---|---|
  | Joondalup | TenderLink `portal.tenderlink.com/joondalup` |
  | Melville | TenderLink `tenderlink.com/melville` |
  | Canning | TenderLink `tenderlink.com/canning` |
  | Wanneroo | eTenderBox `wanneroo.etenderbox.com.au` |
  | Cockburn | SL360 `public.sl360.au/request/open/cockburnprod` |
  | Stirling | SL360 public portal, embedded |
  | Rockingham | Rock Port, account required |

  None of these portals were crawled. Many need a supplier login for documents. Councils also publish a **tender register** (e.g. Stirling `/tender-register`, Joondalup `/city-tenders-and-procurement/tender-register`, a "Tender register" section on Rockingham's page). Those are public council web pages and are a good source of award facts.

### 2.2 Other official open data / program sources

| Source | What | Access / licence | Use in v1 |
|---|---|---|---|
| **data.wa.gov.au CKAN API** `https://catalogue.data.wa.gov.au/api/3/action/package_search?q=…` | Free JSON API. `capital works` = 33 hits (mostly Water Corporation CIP planned assets, City of Perth XB app). `mrwa-road-hierarchy` (Main Roads, CC BY 4.0) | Open, no key. Per-dataset licence | Road hierarchy for road classification. Perth capital works layer |
| **Main Roads WA – Black Spot Program** https://www.mainroads.wa.gov.au/road-safety/road-safety-improvement-programs/black-spot-program/ | Approved annual project lists, including **2026-27 State Black Spot Program – Metropolitan and Regional Local Roads** (PDF; redirects to `assets.mainroads.wa.gov.au`, 418 KB). Each entry gives road/intersection, council, treatment description and approved budget, e.g. "Lawley Street-Lockwood Street (City of Stirling)" | robots allows (disallows only `/bin`, `/tmp`, `/EPiServer/CMS/`). Licence not verified (`/copyright/` returned 404) | **High value**: a funding reference plus an early signal for intersection works in target councils |
| Australian Government Black Spot Program 2026-27 (WA) | Hosted by Main Roads and investment.infrastructure.gov.au (search results; PDF not fetched) | Commonwealth | Same use |
| **Roads to Recovery** nominal allocations 2024-29 v2.0 https://investment.infrastructure.gov.au/sites/default/files/documents/rtr-program-nominal-annual-funding-allocations-2024-29_v2.0.pdf | Per-council allocations (fetched, 1.0 MB). Not project-level | robots OK | Funding-reference tagging ("R2R") |
| **MRRG** (Metropolitan Regional Road Group): MRWA secretariat, WALGA State Road Funds advisory committee papers | State road funds to local roads. Procedures PDF (Jan 2026) and WALGA SAC agendas mention a "Roads Program 2026-27 (Indicative)" (search results only; not fetched) | Public PDFs | Later. Councils' agendas usually echo MRRG approvals |
| MyCouncil (DLGSC) `mycouncil` dataset | OData `https://prod-mycouncil-services.azurewebsites.net/data.svc/` (CC BY 4.0, last modified 2022-05-25) | Not fetched | Council reference data only |
| WALGA | Industry body; IP page asserts ownership. No open capital works data found | n/a | Context only |

### 2.3 Budget timing (WA)

* **Annual budget adoption:** by **31 August** under s 6.2(1) *Local Government Act 1995* (DLGSC "Local Government Budget Process — Timeline and Considerations"). In practice, the Perth metro councils sampled adopted 2026-27 budgets in late June to July 2026. For example, Stirling's adoption news is dated 08 July 2026, Joondalup, Cockburn and Rockingham have "adopts 2026-27 budget" news items, and Stirling's 2025-26 file is stamped 11 Aug 2025.
* **Mid-year budget review:** since 1 July 2023 the review is carried out **between 1 January and the last day of February**, and submitted to council **by 31 March** (Local Government Regulations Amendment Regulations 2023; DLGSC guidance). It used to be 1 Jan–31 Mar.
* **Copy to the Department** within 14 days of adoption (previously 30).
* **Practical calendar for the radar:**
  - **Apr–Jun:** draft budget and Corporate Business Plan workshops and agenda items. Earliest capital works signals appear here (e.g. Rockingham's "May 2026 Business Plan").
  - **Jun–Aug:** budget adoption. The capital works program is published (Joondalup schedule, Melville CWP page, Cockburn Project Plans).
  - **Jul–Nov:** tender award reports in council minutes for the new year's program.
  - **Feb–Mar:** mid-year review, with budget amendments, carry-forwards and deletions.
  - **Monthly:** financial activity statements in agendas often carry capital works status.
  - **Aug:** Black Spot approvals (the 2026-27 announcement was 8 Aug 2026).

---

## 3. Licensing, terms and legal caution

### 3.1 What each source says (fetched 2026-11-22)

| Source | Copyright statement (quoted or summarised) | Reuse posture |
|---|---|---|
| Stirling (`/disclaimer`) | Content "is protected by the Copyright Act 1968 and is owned by the City of Stirling unless otherwise stated." | All rights reserved; silent on reuse |
| Wanneroo (`/disclaimer`) | Information "may only be used, copied or otherwise reproduced ... on the understanding ... that at all times copyright ... remains with the City of Wanneroo. Where appropriate, users will be required to acknowledge that copyright." | Reuse tolerated **with attribution**; the most permissive wording |
| Joondalup (`/disclaimer`) | All content "is the property of the City of Joondalup". | All rights reserved |
| Cockburn (`/Disclaimer`) | "Material ... can be reproduced for personal and **non-commercial** ..." | Non-commercial only |
| Melville (`/terms-of-use`) | "may be used for private, **non-commercial** personal reference only. If you wish to use material for commercial or non-personal use, please contact us." | Non-commercial; permission needed for commercial use |
| Rockingham (`/copyright`) | "must not be reproduced, reused, or redistributed **without prior written permission** from the City." | Permission required |
| Perth (`/terms-and-conditions`) | Personal or non-commercial use only. "Website pages or documents are not to be deconstructed or modified in any form ..." | Strict |
| Canning (`/disclaimer/`) | Protected by the Copyright Act 1968 and "remains the property of the City of Canning." Plus the CDN robots block | Excluded |
| Tenders WA | No reuse "for any commercial purpose"; must not "make money out of its use". robots disallows all but the homepage | Do not crawl. Manual reference only |
| data.wa.gov.au Tenders WA awards, Perth capital works app, MRWA road hierarchy | **CC BY 4.0** | Free reuse with attribution |
| WA legislation site (context) | State copyright material licensed CC BY 4.0, with exclusions | Not applicable to councils |

### 3.2 Facts vs documents

This is general research, not legal advice. Get advice before charging money.

* Australian copyright protects *expression*, not facts. The High Court in *IceTV v Nine Network* (2009) and the Full Federal Court in *Telstra v Phone Directories* (2010) held that facts and mechanically assembled compilations generally lack protection where there is no independent intellectual effort in expression. Extracting a **short factual record** (project name, street, suburb, work type, dollar value, meeting date, resolution number) and linking to the official source is therefore far lower risk than copying text.
* **Republishing full PDFs, attachments or long verbatim passages is reproduction** and is restricted by every council's terms above. Don't mirror documents. Also bear in mind that WA councils publish agendas and minutes under statutory public-access duties (*Local Government Act 1995* Part 5). That is an access right, not a reuse licence.
* **Commercial use clauses** (Cockburn, Melville, Perth, Rockingham, Tenders WA) make a *paid* service riskier than a free one, even for facts, because website terms can bind as a contract when accepted. Recommendations:
  1. Keep v1 free or private while validating.
  2. Before charging, **email each council's governance or communications team** describing the use (facts plus links, no document redistribution) and ask for written consent. Wanneroo's wording suggests attribution is enough there.
  3. Prefer CC BY sources where they exist.

### 3.3 Practical compliance rules for the build

1. **robots.txt is enforced in code.** Check before every host's first request each day. The current blocks are `cdn.canning.wa.gov.au` (all), Swan `/v1/`, Tenders WA (all except the homepage), Stirling/Joondalup/Swan `/v1/` and `/admin/`, and Perth `/upload/`.
2. **Never bypass** 401/403 responses, WAF challenges (Gosnells Akamai, Sucuri, Cloudflare), logins (tender portals, Tenders WA, Joondalup search host) or CAPTCHAs. On a 403, 429 or challenge page, stop that host for 24 h and alert a human.
3. **Rate limits:** at most 1 request per 5 s per host, one concurrent connection per host, and a scheduled weekly run (plus a daily listing-only check at most). Back off exponentially on 5xx or 429.
4. **Honest User-Agent** with a project name and contact URL. Don't impersonate a browser.
5. **Conditional GET** (`If-None-Match` / `If-Modified-Since`), a size cap (e.g. 80 MB) and HEAD before GET, so large agendas are only fetched when they change.
6. **No document hoarding:** keep downloaded PDFs only as long as needed to extract (e.g. a 30-day cache). Store extracted text plus a SHA-256 hash, the source URL and the retrieval timestamp. Never serve the PDF from our own host; link to the council URL.
7. **Attribution on every fact:** "Source: City of X, <document title>, <date>, p. N, retrieved YYYY-MM-DD" with a link. The digest footer should state that council material is © the respective council.
8. **Quote minimally:** at most one sentence of context per item in digests.
9. **LLM use** (if any) runs on locally stored text, as a classifier only, never the system of record. Note Infocouncil's Cloudflare content-signal preamble: `ai-input` isn't signalled either way, so prefer local models and don't send council text to third-party AI APIs without checking.
10. **Log every fetch** (URL, status, bytes, ETag, timestamp) for auditability.

---

## 4. Competitors and substitutes

| Product | What they sell | Indicative pricing (public) | Coverage | Gap a small free tool can fill |
|---|---|---|---|---|
| **CCF WA** (Civil Contractors Federation WA) https://ccfwa.com.au/Contractor-Membership | Membership benefit: "member-exclusive government pipeline of civil works" via the online Member Portal, plus fortnightly e-newsletters, industry news and contract/tender interpretation help | Fees not published ("email ccfwa@ccfwa.com.au"); pro-rata by financial year | WA civil, government pipeline (scope as described on the page; portal contents not visible) | Likely a state/major-project pipeline. Granular **council minor works** (resurfacing lists, footpath programs, Black Spot intersections) at street level, with source links, is probably not covered (unverified, members-only) |
| **Cordell Connect** (Cotality, formerly CoreLogic) https://www.cotality.com/software-solutions/cordell-connect | Construction project database from planning to completion: contacts, timelines, values, stages, alerts on new tenders and early-stage projects, API | **Lite: $577.50/month, one state, 2 users, $6,930 minimum over 12 months.** Civil/mining by quote | Residential, commercial, industrial, **civil** | Expensive for small subcontractors. Council *budget-line* projects (e.g. "Warwick Road westbound resurfacing $406k") may sit below its project threshold or appear only at tender stage (unverified) |
| **DA Leads** (search results; site not fetched) | Development applications from 330+ councils, AI-classified into 21 trade categories | Not found publicly | National DA data | DAs are *private* development. They don't capture council capital works at all, so it complements rather than competes |
| **TenderLink** (illion) https://illion.tenderlink.com/subscribe-online/ | Tender aggregation and alerts. Also the actual e-tender portal for several WA councils | Standard monthly (+GST): $95 local region, $135 one state, up to $605 Trans-Tasman; Enterprise up to $875 | AU/NZ; Perth selectable as a region | Only *advertised* tenders. No signal months earlier from budgets or minutes |
| **Australian Tenders** https://www.australiantenders.com.au/pricing/ | Tender aggregation and alerts | Lite $67/month (1 region), Core $99, Premium $145 | "Any publicly available tender in Australia and New Zealand" | Same: tender-stage only |
| **AskTender**, **Tenderwell**, **Tenders Agent** (Capterra listings, search results only) | Tender search/alerts | AskTender from A$299 (flat); others have free tiers | Varies | Tender-stage only |
| **PlanningAlerts** (OpenAustralia Foundation, free) | DA alerts | Free | Many councils | DAs, not capital works |
| Apify community scrapers (e.g. AusTender/state portal actors) | Pay-per-result tender scraping | ~US$1–10 per 1,000 results | AusTender, some state portals | Tender-stage, and may conflict with portal terms. Not a model to copy |

**Positioning:** the defensible niche is **pre-tender, council-level, street-level civil works signals**: budget schedules, capital works programs, Black Spot approvals, budget amendments and tender award reports. Each comes with a source link and a confidence flag, for 6–8 metro councils, in a weekly digest. Tender aggregators start at tender release. Cordell is broad and expensive. CCF WA's pipeline is members-only and probably macro. Typical lead time is a budget line in June–July, then the tender in September–November, then an award report in the minutes. That gives **2–5 months of earlier signal**. The Stirling minutes example shows a tender advertised in August 2026 and awarded in November 2026 for a project already in the program.

---

## 5. Extraction feasibility

### 5.1 How noisy are the documents?

| Document type | Observed noise | Notes |
|---|---|---|
| **Budget schedules** (Joondalup) | Medium | `pdftotext -layout` preserves columns, but **descriptions wrap across 2–4 lines above and below the numeric row**. Rows must be re-joined by anchoring on the project-number regex (`^[A-Z]{3}\d{4}\s+W\d{4}`) and gathering wrapped text within the description column's x-range. Use PyMuPDF word coordinates rather than plain text. "-" means zero |
| **Statutory budgets** (Stirling, Wanneroo, Rockingham, Perth, Melville) | Medium–high for project facts | Mostly aggregates by asset class or account code, with negative numbers in parentheses and multi-year columns. Good for the funding context of a council-year, poor for projects |
| **Project plans** (Cockburn) | Low–medium | One project per page, with labelled fields ("ID:", "Directorate:", budget table). Bullet lists of road segments as "Street – From to To" |
| **HTML capital works page** (Melville) | Low | One paragraph per project: "Name ($value) description". A regex gets most of it |
| **Agendas/minutes** | High volume, medium structure | 100–200+ pages. TOC with dotted leaders. Item numbering varies by council (Stirling `12.1/SP1`, Wanneroo `AS01-11/26`, Infocouncil). Tender reports follow a recognisable template (Regulation 18 evaluation, "Tender No.", "That the tender from X ... be accepted", value ex GST). Recommendations appear twice (officer recommendation and council resolution), so dedupe within a document. Confidential attachments are withheld |
| **Infocouncil HTML** (Wanneroo) | Low | Real headings and paragraphs. Better than PDF |
| **Main Roads Black Spot PDF** | Low | "Road SLK x - y (City of Z)" heading line, then a description, then a dollar amount and comment |

The sample contained no scanned budgets. OCR (Tesseract, free) should be a fallback only, triggered when a page has fewer than ~50 extractable characters.

### 5.2 Recommended v1 extraction schema

**`source_document`**
`doc_id` (sha256 of bytes), `council_id`, `doc_type` (budget | capital_program | project_plans | agenda | minutes | attachments | tender_register | blackspot_list | other), `title`, `meeting_type`, `meeting_date`, `financial_year`, `source_url`, `listing_url`, `retrieved_at`, `http_etag`, `http_last_modified`, `bytes`, `page_count`, `extractor_version`, `text_sha256`.

**`project_mention`** (one row per fact sighting; the system of record)
| field | type | notes |
|---|---|---|
| `mention_id` | uuid | |
| `doc_id`, `page`, `char_span` / `bbox` | provenance | **required** |
| `council_id` | enum | |
| `council_project_code` | text? | e.g. Joondalup `RPR3542`, cost code `W5033`; Cockburn `4C.18` |
| `project_name_raw` | text | verbatim |
| `location_raw` | text | e.g. "Warwick Road (westbound) ... Road Centreline" |
| `road_name`, `from_road`, `to_road`, `suburb`, `ward` | text? | parsed |
| `slk_from`, `slk_to` | num? | Main Roads lists |
| `work_type` | enum[] | see §5.5 |
| `work_type_method` | rule | llm | manual |
| `value_amount` | num? | AUD |
| `value_basis` | enum | budget_total | current_year | municipal | grant | contract_award | estimate |
| `value_gst` | ex | inc | unknown |
| `funding_refs` | enum[] | Municipal, Reserve, Grant, Black Spot (State/Federal), Roads to Recovery, MRRG, Loan, Contribution, Carry-forward |
| `stage` | enum | proposed_budget | adopted_budget | budget_review | tender_advertised | tender_awarded | in_progress | completed | deleted/deferred |
| `date_stated` | text? | e.g. "July 2026 – June 2029" |
| `financial_year` | text | "2026-27" |
| `resolution_ref` | text? | minutes item number |
| `supplier_name` | text? | award reports only |
| `confidence` | 0–1 | from the rules |
| `uncertainty_flags` | enum[] | value_ambiguous_column, wrapped_text_join, name_only_no_location, multi_year, carry_forward, possible_duplicate, ocr_used |
| `extracted_at`, `extractor_version` | | |

**`project`** (deduplicated entity): `project_id`, `council_id`, canonical name, location, work types, latest value, latest stage, first_seen, last_seen, `mention_ids[]`.

### 5.3 Approach without a paid LLM

1. **Fetch layer** with per-council adapters (Kentico ksearch JSON, Joondalup htmlresult, Infocouncil HTML, Cockburn HTML list, Perth static HTML). Store listings as JSON snapshots.
2. **Text layer:** PyMuPDF (`fitz`, already installed here) for words with coordinates, `pdftotext -layout` as a fallback, Tesseract only for image-only pages.
3. **Segmentation:**
   - Budgets: locate capital-works sections by heading keywords ("Capital Works", "Capital Expenditure", "Project Number").
   - Agendas/minutes: split on the council-specific item-number regex. Classify each item by title keywords: tender, capital works, budget amendment/review, Black Spot, road, footpath, drainage, MRRG, Roads to Recovery, petition (petitions for footpaths and traffic treatment are early signals).
4. **Field extraction:** per-template rules.
   - Table row anchors plus column x-ranges (Joondalup).
   - Labelled-field regexes (Cockburn project plans).
   - `Name ($value)` patterns (Melville).
   - Tender report regexes: `Tender No\.?\s*(\d+)`, `tender from (.+?) (?:for|be accepted)`, `\$[\d,]+(?:\.\d+)?\s*\((?:ex|excl)`.
   - Road-segment regex: `(?P<road>[A-Z][\w' ]+ (?:Road|Rd|Street|St|Drive|Dr|Avenue|Ave|Boulevard|Blvd|Way|Parade|Pde|Crescent|Cres|Highway|Hwy))\s*[–-]\s*(?P<from>.+?) to (?P<to>.+)`.
   - Money normalisation: parentheses mean negative, "$4.3 million" becomes 4,300,000, "-" means 0.
5. **Gazetteer** for locations: suburb lists per council (static) plus the CC BY MRWA road hierarchy / Landgate road names. Resolve road + suburb and flag ambiguous names.
6. **Optional later:** a local LLM (e.g. a small instruction model via llama.cpp/Ollama on CPU) **only** to (a) classify work type when rules abstain, and (b) write one-line summaries. Its output is stored as a suggestion with `method=llm` and never overwrites rule-extracted facts or provenance. Free-tier hosted APIs aren't needed for v1.

### 5.4 Dedupe keys and change tracking

* **Document identity:** canonical URL (strip `?v=1`, `language=en`), plus the sha256 of the bytes. A new hash at the same URL is a **new version**. Keep both, and diff the extracted mentions. Cockburn's `ECM_<id>_v<n>` and Infocouncil's `_SUP` suffix carry explicit versioning.
* **Project identity, strongest key first:**
  1. `(council_id, council_project_code)`, e.g. Joondalup `RPR3542`, Cockburn `4C.18`. Stable across budget, review and award where reused.
  2. `(council_id, normalised road_name, from_road, to_road, work_type)` for segment works.
  3. `(council_id, normalised intersection pair)` for Black Spot and roundabouts (order-independent: sort the two road names).
  4. `(council_id, tender_no)` for tender reports.
  5. Fallback: fuzzy match on normalised name tokens (rapidfuzz ≥ 90) within the same council and financial year, with `possible_duplicate` flagged for review rather than auto-merged.
* **Normalisation:** lower-case, expand abbreviations (Rd→road, Dr→drive, Ave→avenue), strip punctuation and "(westbound)"-type qualifiers into a `direction` field, and remove year suffixes ("26/27").
* **Change events** emitted per project: `new`, `value_changed` (with delta), `stage_advanced` (budget → tender → award), `deferred/deleted` (mid-year review), `location_refined`, `source_added`. The weekly digest is built from change events, not full lists.

### 5.5 Trade-matching taxonomy (v1)

| Trade code | Label | Rule keywords (case-insensitive; tune per council) | Typical contractor |
|---|---|---|---|
| `ROAD_RESURF` | Road resurfacing / preservation | resurfac*, reseal*, asphalt overlay, mill and fill, road preservation, pavement renewal, rehabilitation (with road), spray seal | Asphalt / profiling |
| `ROAD_CONST` | Road construction / upgrade | duplication, widening, new road, extension, dual carriageway, road construction, reconstruct* | Civil earthworks / road builders |
| `INTERSECTION` | Intersection / traffic treatment | roundabout, intersection, black spot, traffic calming, raised plateau, median, slip lane, signals | Civil / traffic |
| `DRAINAGE` | Drainage | drainage, stormwater, sump, soak well, gully, culvert, pipe, outfall, basin, swale | Drainage / pipe-laying |
| `FOOTPATH` | Footpaths / shared paths | footpath, path, shared path, PSP, dual use path, pedestrian, cycleway, bike lane | Concrete / paving |
| `KERBING` | Kerbing | kerb*, kerbing, mountable kerb, barrier kerb, kerb ramp | Kerbing contractors |
| `LINE_MARK` | Line marking | line marking, linemarking, pavement marking, RRPM, raised pavement markers | Line marking |
| `CARPARK` | Car parks (cross-trade) | car park, carpark, parking bays | Asphalt / civil |
| `BRIDGE` | Bridges / underpasses | bridge, underpass, boardwalk (structural) | Structural civil |
| `STREETSCAPE` | Streetscape (multi-trade) | streetscape, verge, landscaping (with road context) | Multi |
| `OTHER_CIVIL` / `NON_CIVIL` | Residual | buildings, HVAC, changerooms, IT, so excluded or low priority | n/a |

Rules: multi-label is allowed (a road upgrade is often `ROAD_CONST + DRAINAGE + KERBING + FOOTPATH + LINE_MARK`). Negative keywords exclude recreation surfaces ("tennis court resurfacing" goes to `NON_CIVIL`, as in Joondalup `PEP2925`). Joondalup project prefixes (`RPR`→`ROAD_RESURF`, `RDC`→`ROAD_CONST`) act as strong priors. Each label records which keyword fired.

---

## 6. Recommendation

### 6.1 Ranked v1 councils

| Rank | Council | Why | Main risks |
|---|---|---|---|
| 1 | **Joondalup** | Project-level capital schedule with codes, suburb, ward and funding split. Open listing endpoint. Predictable host | Free-text 2026 filenames; JSON POST listing; 28 MB agendas |
| 2 | **Wanneroo** | Infocouncil with HTML papers and predictable URLs. Fast-growth council with a large roads budget ($91M Roads line). Most permissive copyright wording | 60+ MB PDFs (use HTML); statutory budget is aggregated |
| 3 | **Stirling** | Largest metro council. Richest listing JSON (agenda, minutes and attachments per meeting). Frequent meetings with tender reports | Aggregated budget; Sucuri WAF; JSON POST |
| 4 | **Melville** | HTML capital works program with values and Black Spot flags. Sitemap lists every meeting page | Non-commercial terms; 50 MB agendas |
| 5 | **Cockburn** | Plain HTML listing. Project Plans PDF names resurfacing segments and budgets. ECM versioned filenames | Non-commercial terms; 45 MB agendas |
| 6 | **Rockingham** | Clean JSON listing with document types and byte sizes. Planning and Asset Services committee papers. Small agendas | Strictest copyright (written permission) |
| 7 | **Perth** | Static HTML with direct links. CC BY capital works open data | 2026 meetings page stale after July at retrieval; CBD-centric mix; strict terms |
| Reserve | Fremantle | WP JSON endpoint, robots-permitted | Endpoint parameters not yet worked out |
| Blocked | Canning, Swan, Gosnells, Armadale | robots (Canning CDN, Swan `/v1/`), bot wall (Gosnells), JS-only (Armadale) | Ask Canning and Swan for permission; revisit Armadale later |

**v1 = the top 6** (Joondalup, Wanneroo, Stirling, Melville, Cockburn, Rockingham). Add Perth once its meeting-page freshness is confirmed.

### 6.2 Minimum viable document set

Per council, per year:
1. **Adopted annual budget** PDF, plus the capital works program, project plans or HTML page where one exists (Joondalup budget schedule, Melville CWP page, Cockburn Project Plans).
2. **Ordinary Council meeting agendas and minutes**, plus the **infrastructure/asset committee** papers where separate (Rockingham Planning and Asset Services; Stirling Community and Resources). Agendas give early signal; minutes confirm decisions and awards.
3. **Mid-year budget review** agenda item (Feb–Mar).
4. **Council tender register page** (award facts).

Statewide:
5. **Main Roads 2026-27 State Black Spot (local roads)** and the **Australian Government Black Spot WA** lists.
6. **Roads to Recovery allocations** (funding tag only).
7. *(Context only)* data.wa.gov.au Tenders WA award CSVs (CC BY), for state-agency civil award benchmarks.

### 6.3 Risks and open questions

1. **Licensing for commercial use.** Four of the six v1 councils restrict commercial reuse or require permission. *Action:* send permission requests before any paid launch. Run v1 as a free or private pilot.
2. **Canning and Swan** are good councils blocked by robots on specific paths. *Action:* ask whether automated access to public documents is acceptable, or whether they could add an allow rule.
3. **Undocumented JSON endpoints** (Kentico ksearch, Joondalup htmlresult) could change without notice. *Mitigation:* contract tests that alert on schema change, with the sitemap as a fallback discovery path.
4. **Large files** (up to ~130 MB) and bandwidth politeness. *Mitigation:* conditional GET, size caps, HTML or attachment-excluded variants.
5. **Budget granularity varies.** Only Joondalup and Cockburn (via Project Plans) and Melville (HTML) give project lists. For the others, signal depends on agenda parsing, which has lower precision.
6. **Perth meeting page freshness** (Aug–Nov 2026 missing at retrieval).
7. **Fremantle endpoint parameters** are untested.
8. **CCF WA pipeline contents** are members-only. How much it overlaps with this product is unknown.
9. **Cordell's coverage of council minor works** is unverified. A trial or a conversation with a contractor would settle it.
10. **Main Roads licence terms** weren't found (the `/copyright/` URL returned 404). Confirm before republishing Black Spot facts.
11. **Infocouncil content-signals:** if Cloudflare later sets `ai-input=no`, the optional LLM step must skip Wanneroo text.
12. **Gosnells:** is the 403 aimed at non-browser clients generally, or a geo/IP block? Don't probe further. A human can check in a browser and ask the council.

---

## 7. Sources (all retrieved 2026-11-22 unless noted)

### Councils (fetched with curl)
- Stirling: robots https://www.stirling.wa.gov.au/robots.txt · sitemap https://www.stirling.wa.gov.au/sitemap.xml · minutes & agendas https://www.stirling.wa.gov.au/city-and-council/about-council/council-and-committee-meetings/minutes-and-agendas · listing JSON `POST https://www.stirling.wa.gov.au/ksearchmna/ksearchresult` · annual reports & budgets https://www.stirling.wa.gov.au/city-and-council/about-council/governance-and-transparency/annual-reports-and-financial-statements · budget PDF https://www.stirling.wa.gov.au/awcontent/Web/Documents/City%20and%20Council/Governance%20and%20transparency/Annual%20Reports%20and%20Financial%20Statements/City-of-Stirling-Statutory-Budget-2027.pdf · agenda https://www.stirling.wa.gov.au/getmedia/11cb4985-d185-435a-aa2c-af1d7e0ea512/Council-Agenda-17-November-2026.pdf · minutes https://www.stirling.wa.gov.au/getmedia/bc87f6d5-383c-46c3-b4c6-9ece60b0be18/Council-Minutes-17-November-2026.pdf · disclaimer https://www.stirling.wa.gov.au/disclaimer · tenders https://www.stirling.wa.gov.au/business-and-investment/doing-business-with-the-city/current-tender-and-procurement-opportunities
- Wanneroo: robots https://www.wanneroo.wa.gov.au/robots.txt · minutes & agendas https://www.wanneroo.wa.gov.au/city-and-council/council,-committees-and-meetings/minutes-and-agendas · Infocouncil https://wanneroo.infocouncil.biz/ (robots https://wanneroo.infocouncil.biz/robots.txt) · agenda PDF https://wanneroo.infocouncil.biz/RedirectToDoc.aspx?URL=Open/2026/11/OC_24112026_AGN_AT.PDF · agenda HTML https://wanneroo.infocouncil.biz/Open/2026/11/OC_24112026_AGN_AT.HTM · minutes https://wanneroo.infocouncil.biz/RedirectToDoc.aspx?URL=Open/2026/10/OC_27102026_MIN_AT.PDF · budget news https://www.wanneroo.wa.gov.au/city-and-council/latest-news/budget-2026-27 · budget PDF https://www.wanneroo.wa.gov.au/getContentAsset/3aeb5e3f-84a5-46b1-80a2-b53f979c4125/3ca954ad-3848-47c6-8f97-68cebe0b47a2/2026_2027_Statutory_Budget.PDF?language=en · disclaimer https://www.wanneroo.wa.gov.au/disclaimer · tenders https://www.wanneroo.wa.gov.au/business-and-investment/tenders-and-contracts/tenders,-quotes-and-eois
- Joondalup: robots https://www.joondalup.wa.gov.au/robots.txt · agendas & minutes https://www.joondalup.wa.gov.au/city-and-council/council-and-committee-meetings/council-agenda-and-minutes · listing `POST https://www.joondalup.wa.gov.au/search/htmlresult` · listing JS https://www.joondalup.wa.gov.au/awcontent/web/assets/js/modules/aw_pagetypelisting.js · search iframe https://search.joondalup.wa.gov.au/docs/embed.html (robots → 401) · publications https://www.joondalup.wa.gov.au/city-and-council/publications/documents-and-publications · budget PDF https://www.joondalup.wa.gov.au/awcontent/Web/Documents/Budget,%20Fees%20and%20Financial%20Reports/Budget-2026-27.pdf · agenda https://api.joondalup.wa.gov.au/files/councilmeetings/2026/27%20Oct%202026%20-%20Council%20Agenda%20Inc%20Attachments.pdf?v=1 · minutes https://api.joondalup.wa.gov.au/files/councilmeetings/2026/27%20October%202026%20-%20Council%20-%20Minutes%20inc%20Attach.pdf?v=1 · disclaimer https://www.joondalup.wa.gov.au/disclaimer · tenders https://www.joondalup.wa.gov.au/business-and-investment/city-tenders-and-procurement/tenders
- Melville: robots https://www.melvillecity.com.au/robots.txt · agendas & minutes https://www.melvillecity.com.au/our-city/our-council/agendas-and-minutes · listing `POST https://www.melvillecity.com.au/ksearch/mnaresult` · budget https://www.melvillecity.com.au/our-city/city-management/financial-information/budget · budget PDF https://www.melvillecity.com.au/getContentAsset/8c0a400e-cb96-4604-98e1-befd824a6285/3ca954ad-3848-47c6-8f97-68cebe0b47a2/26-130-Budget-Document-_-WEB-READY.pdf?language=en · capital works https://www.melvillecity.com.au/our-city/about-our-city/capital-works-program · agenda https://www.melvillecity.com.au/getContentAsset/4c1124ba-2322-45d8-8cc2-ebbbb8fedd46/3ca954ad-3848-47c6-8f97-68cebe0b47a2/Agenda-8-December-2026-OMC.pdf?language=en · minutes https://www.melvillecity.com.au/getContentAsset/21ed1f23-57da-4b53-82a9-79b75dcad9b2/3ca954ad-3848-47c6-8f97-68cebe0b47a2/Minutes-17-November-2026-OMC.pdf?language=en · terms https://www.melvillecity.com.au/terms-of-use · tenders https://www.melvillecity.com.au/our-city/contractors-suppliers-and-current-tenders
- Cockburn: robots https://www.cockburn.wa.gov.au/robots.txt · agendas & minutes https://www.cockburn.wa.gov.au/City-and-Council/About-Council-Meetings-and-Elections/Agendas-and-Minutes · strategies & plans https://www.cockburn.wa.gov.au/Strategies-and-Plans · budget PDF https://www.cockburn.wa.gov.au/getattachment/eaa1f7d8-69b9-4c8f-b3b9-ac533c2ea170/ECM_12777460_v1_Annual-Budget-2026-2027-pdf.aspx · project plans https://www.cockburn.wa.gov.au/getattachment/91e79b42-247f-4c86-a1bd-050ad2cf769f/ECM_12774885_v2_Project-Plans-2026-2027-pdf.aspx · budget highlights https://www.cockburn.wa.gov.au/City-and-Council/Rates/Budget-Highlights-2026-27 · agenda https://www.cockburn.wa.gov.au/getattachment/a5cf4eca-728c-4e5c-ae81-32e01d71c0fd/ECM_12928826_v1_Ordinary-Council-Meeting-Agenda-10-November-2026-pdf.aspx · minutes https://www.cockburn.wa.gov.au/getattachment/a9c882fe-03ab-4938-bc8d-53ceab8e719b/ECM_12915695_v3_Ordinary-Council-Meeting-Minutes-13-October-2026-pdf.aspx · disclaimer https://www.cockburn.wa.gov.au/Disclaimer · tenders https://www.cockburn.wa.gov.au/City-and-Council/Jobs-and-Tenders/Tenders-and-Quotations/Public-Tenders
- Rockingham: robots https://rockingham.wa.gov.au/robots.txt · agendas & minutes https://rockingham.wa.gov.au/your-city/council/council-and-committee-meetings/agendas-and-minutes · listing `POST https://rockingham.wa.gov.au/ksearch/mnaresult` · listing JS https://rockingham.wa.gov.au/assets/modules/aw_ksearch.js?v=260909 · budget & finance https://rockingham.wa.gov.au/your-city/about-us/budget-and-finance · budget PDF https://rockingham.wa.gov.au/getContentAsset/0399c61d-9a71-4817-9f9d-29d207082d86/3ca954ad-3848-47c6-8f97-68cebe0b47a2/Annual-Budget-2026-2027.pdf?language=en · agenda https://rockingham.wa.gov.au/getContentAsset/bf814a1d-f809-401e-8895-48da3a720111/3ca954ad-3848-47c6-8f97-68cebe0b47a2/2026-11-(Nov)-Agenda-Ordinary-Council.pdf?language=en · minutes https://rockingham.wa.gov.au/getContentAsset/59a532a4-02b5-43c1-a892-c9a3e0b17a7e/3ca954ad-3848-47c6-8f97-68cebe0b47a2/2026-10-(Oct)-Minutes-Ordinary-Council.pdf?language=en · copyright https://rockingham.wa.gov.au/copyright · tenders https://rockingham.wa.gov.au/your-city/business-and-investment/tenders-and-eois
- Perth: robots https://perth.wa.gov.au/robots.txt · 2026 meetings https://perth.wa.gov.au/council/council-meetings/2026-council-meetings · search https://perth.wa.gov.au/council/council-meetings/search-meetings · budget https://perth.wa.gov.au/council/budget · budget PDF https://perth.wa.gov.au/-/media/Project/COP/COP/COP/Documents-and-Forms/Council/Documents/Reports-and-Important-Documents/Annual-Budgets/Budget-2026-27/Annual-Budget-2026-27.pdf · terms https://perth.wa.gov.au/terms-and-conditions
- Canning: robots https://www.canning.wa.gov.au/robots.txt and **https://cdn.canning.wa.gov.au/robots.txt (Disallow: /)** · agendas & minutes https://www.canning.wa.gov.au/agendas-and-minutes/ · finance https://www.canning.wa.gov.au/about-us/council-business/finance/ · disclaimer https://www.canning.wa.gov.au/disclaimer/
- Swan: robots https://www.swan.wa.gov.au/robots.txt · meetings https://www.swan.wa.gov.au/city-and-council/your-council/meetings (listing API under disallowed `/v1/`)
- Gosnells: https://www.gosnells.wa.gov.au/robots.txt and https://www.gosnells.wa.gov.au/ → **HTTP 403 Access Denied (Akamai)**
- Armadale: https://www.armadale.wa.gov.au/ → https://my.armadale.wa.gov.au/service/ · https://my.armadale.wa.gov.au/service/agendas-and-minutes/ (8.1 MB SPA, no document links)
- Fremantle: robots https://www.fremantle.wa.gov.au/robots.txt · https://www.fremantle.wa.gov.au/your-city-and-council/about-council/agendas-and-minutes/ · `POST https://www.fremantle.wa.gov.au/wp-json/aw/v1/minutes-and-agendas` → 404 "No posts found" with an empty body

### State / federal (fetched with curl)
- Tenders WA: robots https://www.tenders.wa.gov.au/robots.txt · home https://www.tenders.wa.gov.au/watenders/index.do · copyright https://www.tenders.wa.gov.au/watenders/copyright.vm · terms https://www.tenders.wa.gov.au/watenders/terms-and-conditions.vm · about https://www.tenders.wa.gov.au/watenders/about.vm (the last three read once for compliance; disallowed for automation)
- data.wa.gov.au CKAN: https://catalogue.data.wa.gov.au/api/3/action/package_search?q=tender · Tenders WA contract award details 2023-24 https://catalogue.data.wa.gov.au/dataset/90b272e8-9ae0-4995-8dc9-86f8249b51d1 (CSV, CC BY 4.0) · 2022-23 https://catalogue.data.wa.gov.au/dataset/a66013f6-07db-433b-bc6a-984b249a1013 · 2021-22 https://catalogue.data.wa.gov.au/dataset/cab2f08c-6776-4328-b4b3-776f1f88eb3a · Perth capital works https://catalogue.data.wa.gov.au/api/3/action/package_show?id=perth-xb-capital-works-app · MyCouncil https://catalogue.data.wa.gov.au/api/3/action/package_show?id=mycouncil · data.wa.gov.au robots https://data.wa.gov.au/robots.txt
- Main Roads WA: robots https://www.mainroads.wa.gov.au/robots.txt · 2026-27 State Black Spot (local roads) https://www.mainroads.wa.gov.au/4a2bf3/globalassets/road-safety/road-safety-improvement-programs/black-spot-program/2026-27-state-black-spot-program-metropolitan-and-regional-local-roads.pdf (redirects to assets.mainroads.wa.gov.au) · copyright URL https://www.mainroads.wa.gov.au/copyright/ → 404
- Roads to Recovery 2024-29 v2.0: https://investment.infrastructure.gov.au/sites/default/files/documents/rtr-program-nominal-annual-funding-allocations-2024-29_v2.0.pdf

### Web search / WebFetch (2026-11-22; secondary)
- DLGSC budget process timeline: https://www.wa.gov.au/government/publications/local-government-budget-process-timeline-and-considerations
- DLGSC budget review guidance: https://www.wa.gov.au/government/announcements/local-government-guidance-undertaking-your-next-prescribed-budget-review
- Local Government Regulations Amendment Regulations 2023: https://dlgsc.wa.gov.au/local-government/local-government-reform/local-government-regulations-amendment-regulations-2023
- Main Roads Black Spot program page: https://www.mainroads.wa.gov.au/road-safety/road-safety-improvement-programs/black-spot-program/
- WA media statement 8 Aug 2026 (State Black Spot $23.29M, 73 projects): https://www.wa.gov.au/government/media-statements/Cook Labor Government/Record-funding-boost-for-road-safety-across-Western-Australia-20260808
- MRRG policies: https://www.mainroads.wa.gov.au/globalassets/technical-commercial/local-government-funding/metropolitan-regional-road-group-policies-and-practices.pdf · WALGA SAC agenda 4 May 2026: https://walga.asn.au/awcontent/Web/Documents/20260504-SAC-Meeting-1-of-2026-Agenda.pdf
- CCF WA membership: https://ccfwa.com.au/Contractor-Membership
- Cordell Connect (Cotality): https://www.cotality.com/software-solutions/cordell-connect
- TenderLink pricing: https://illion.tenderlink.com/subscribe-online/
- Australian Tenders pricing: https://www.australiantenders.com.au/pricing/
- DA Leads description (search results): https://dev.to/daleads/australian-building-permit-data-is-a-goldmine-heres-what-we-built-with-it-bf9 · https://www.g2.com/products/da-leads/discuss
- PlanningAlerts: https://www.planningalerts.org.au/faq
- WA legislation copyright (CC BY context): https://www.legislation.wa.gov.au/legislation/statutes.nsf/copyright.html
