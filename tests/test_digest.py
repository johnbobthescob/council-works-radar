"""M14: digest golden-structure tests on a fixture DB (offline)."""

import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest

from radar import changes, cli, db, dedupe, digest
from radar.models import FetchLogEntry, ProjectMention, SourceDocument, make_mention_id

RESURF = [{"code": "ROAD_RESURF", "method": "rule", "evidence": "kw:resurfac"},
          {"code": "KERBING", "method": "rule_implied", "evidence": "implied:ROAD_RESURF"}]
DRAIN = [{"code": "DRAINAGE", "method": "rule", "evidence": "kw:drainage"}]
FOOT = [{"code": "FOOTPATH", "method": "rule", "evidence": "kw:footpath"}]
NONCIV = [{"code": "NON_CIVIL", "method": "rule", "evidence": "kw:changeroom"}]
T1 = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)      # 2026-W47
T2 = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)      # 2026-W48


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs, self.ids = [], set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        if a.get("id"):
            self.ids.add(a["id"])


def doc(conn, name, council, url, doc_type="budget", retrieved="2026-11-17T01:00:00+00:00", ctype="application/pdf", **kw):
    d = SourceDocument(doc_id=(name * 64)[:64], doc_key=url, council_id=council, source_url=url, doc_type=doc_type,
                       retrieved_at=retrieved, content_type=ctype, title=kw.pop("title", None), **kw)
    db.upsert_source_document(conn, d)
    return d


def mention(conn, d, anchor, name, page=1, **kw):
    kw.setdefault("trade_labels", RESURF)
    kw.setdefault("quote", "A short factual quote.")
    m = ProjectMention(mention_id=make_mention_id(d.doc_id, "test", anchor), doc_id=d.doc_id, council_id=d.council_id,
                       page=page, char_start=0, char_end=1, project_name_raw=name, extractor="test", **kw)
    db.upsert_mention(conn, m)
    return m


def cycle(conn, when):
    run = changes.start_run(conn, ["joondalup", "melville"], now=when)
    dedupe.link(conn)
    rep = changes.record_changes(conn, run, now=when)
    changes.finish_run(conn, run, now=when)
    return rep


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    jd = doc(c, "a", "joondalup", "https://www.joondalup.wa.gov.au/budget.pdf", title="Budget 2026-27", financial_year="2026-27")
    mw = doc(c, "b", "melville", "https://www.melvillecity.com.au/cwp", doc_type="capital_program", ctype="text/html")
    mention(c, jd, "1", "Warwick Road (westbound)", page=41, council_project_code="RPR3542", value_amount=406364,
            value_basis="budget_total", stage="adopted_budget", suburb="Warwick", financial_year="2026-27",
            funding_refs=["Municipal", "BlackSpot_State"], uncertainty_flags=["wrapped_text_join"], confidence=0.55,
            quote="x" * 400)
    mention(c, jd, "2", "Hepburn Avenue Drainage", page=42, council_project_code="RDC1", value_amount=90000,
            value_basis="budget_total", stage="adopted_budget", trade_labels=DRAIN, confidence=0.8)
    mention(c, jd, "3", "Changeroom Upgrade", page=43, council_project_code="PEP9", value_amount=500000,
            trade_labels=NONCIV, confidence=0.9)
    mention(c, mw, "1", "Boundary Road Footpath", page=1, value_amount=60000, value_basis="budget_total",
            trade_labels=FOOT, stage="adopted_budget", confidence=0.3, uncertainty_flags=["name_only_no_location"],
            financial_year="2026-27")
    return c


def test_baseline_digest_structure(conn):
    cycle(conn, T1)
    ctx = digest.build_context(conn, "2026-W47")
    html = digest.render_html(ctx)
    assert ctx["baseline"] and "Week 47, 2026 (16–22 Nov)" in html and 'id="baseline-banner"' in html
    assert 'id="baseline"' in html and "Baseline pipeline" in html and 'id="pipeline"' not in html
    p = Links()
    p.feed(html)
    # every card links to the official source, with the PDF page anchor
    assert html.count('class="card"') == 3                       # non-civil is counted, not listed
    assert html.count("Source: <a href=") == html.count('class="card"')
    assert "https://www.joondalup.wa.gov.au/budget.pdf#page=41" in p.hrefs
    assert "https://www.melvillecity.com.au/cwp" in p.hrefs      # html page: no #page anchor
    assert "City of Joondalup, Budget 2026-27, p. 41, retrieved 2026-11-17" in html
    assert "$406,364 (budget total, FY2026-27, GST unknown)" in html
    assert "Municipal / BlackSpot_State" in html
    # flags rendered as labelled pills, confidence badges, implied chips lighter
    assert "wrapped text" in html and "name only, no location" in html
    assert "conf-Medium" in html and "conf-Low" in html and "conf-High" in html
    assert 'class="chip implied"' in html
    assert len(re.search(r'<div class="quote">“(.*?)”', html).group(1)) <= digest.MAX_QUOTE_CHARS
    # footer / attribution
    assert digest.FOOTER_TEXT in html and 'id="footer"' in html and "non-commercial" in html
    assert "Black Spot funding references" not in html
    # council x trade matrix counts all four projects, incl. non-civil
    assert ctx["baseline_section"]["project_count"] == 4
    assert "<style>" in html and "<script" not in html


def test_incremental_digest_lists_changes_grouped_by_trade_and_council(conn):
    cycle(conn, T1)
    rev = doc(conn, "c", "joondalup", "https://www.joondalup.wa.gov.au/minutes.pdf", doc_type="minutes", retrieved="2026-11-24T01:00:00+00:00",
              meeting_date="2027-02-24")
    mention(conn, rev, "1", "Warwick Road resurfacing", page=7, council_project_code="RPR3542", value_amount=456364,
            value_basis="budget_total", stage="tender_awarded", confidence=0.8, financial_year="2026-27")
    cycle(conn, T2)
    ctx = digest.build_context(conn)                              # defaults to the latest run's week
    html = digest.render_html(ctx)
    assert ctx["week"] == "2026-W48" and not ctx["baseline"]
    assert "VALUE ▲ $50,000 (+12%)" in html and "STAGE adopted_budget → tender_awarded" in html
    assert 'id="signals-ROAD_RESURF"' in html and "baseline-banner" not in html
    assert html.count('class="card"') == 1
    assert "https://www.joondalup.wa.gov.au/minutes.pdf#page=7" in html
    assert 'id="pipeline"' in html and "Hepburn Avenue Drainage" in html      # pre-tender pipeline table
    assert "Warwick Road" not in html.split('id="pipeline"')[1].split('id="review"')[0]   # awarded: out of the pipeline


def test_health_review_and_blackspot_note(conn):
    c = conn
    d = doc(c, "d", "melville", "https://www.melvillecity.com.au/bad.pdf", doc_type="agenda")
    d.extract_status = "ocr_needed"
    db.upsert_source_document(c, d)
    db.insert_fetch_log(c, FetchLogEntry(ts="2026-11-17T01:00:05+00:00", method="GET", url="https://www.joondalup.wa.gov.au/robots.txt",
                                         host="www.joondalup.wa.gov.au", status=200))
    db.insert_fetch_log(c, FetchLogEntry(ts="2026-11-17T01:00:10+00:00", method="GET", url="https://www.melvillecity.com.au/cwp",
                                         host="www.melvillecity.com.au", status=403, note="host paused"))
    cycle(c, T1)
    ctx = digest.build_context(c, "2026-W47", blackspot_enabled=True)
    html = digest.render_html(ctx)
    health = {r["council"]: r for r in ctx["health"]["councils"]}
    assert health["City of Melville"]["paused"] and not health["City of Joondalup"]["paused"]
    assert health["City of Joondalup"]["robots"] == "allowed"
    assert "PAUSED (403/429/challenge)" in html and 'class="red"' in html
    assert "ocr_needed" in html and "bad.pdf" in html
    assert "Black Spot funding references" in html
    assert "Low-confidence items (1)" in html          # Boundary Road Footpath at 0.3


def test_no_runs_still_renders(conn):
    html = digest.render_html(digest.build_context(conn, "2026-W47"))
    assert "No changes recorded this week" in html and digest.FOOTER_TEXT in html


def test_cli_digest_writes_html_and_pdf(conn, tmp_path, capsys):
    cycle(conn, T1)
    path = tmp_path / "t.sqlite"
    disk = db.connect(path)
    conn.backup(disk)
    disk.close()
    assert cli.main(["digest", "--db", str(path), "--out", str(tmp_path), "--no-pdf"]) == 0
    assert (tmp_path / "digest-2026-W47.html").exists() and not list(tmp_path.glob("*.pdf"))
    assert "wrote" in capsys.readouterr().out


def _weasy():
    try:
        import weasyprint
        weasyprint.HTML(string="<p>x</p>").write_pdf()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _weasy(), reason="weasyprint (and its system libs) not available")
def test_pdf_via_weasyprint(conn, tmp_path):
    cycle(conn, T1)
    res = digest.write_digest(conn, tmp_path, "2026-W47", engine="weasyprint")
    assert res.pdf_engine == "weasyprint" and res.pdf_path.read_bytes().startswith(b"%PDF")
    import pymupdf
    with pymupdf.open(res.pdf_path) as pdf:
        text = "".join(p.get_text() for p in pdf)
        links = [l.get("uri") for p in pdf for l in p.get_links()]
    assert "Baseline pipeline" in text and "https://www.joondalup.wa.gov.au/budget.pdf#page=41" in links


def test_pdf_story_fallback_when_weasyprint_breaks(conn, tmp_path, monkeypatch):
    import sys
    cycle(conn, T1)
    monkeypatch.setitem(sys.modules, "weasyprint", None)           # import raises ImportError
    res = digest.write_digest(conn, tmp_path, "2026-W47")
    assert res.pdf_engine == "story" and res.pdf_path.read_bytes().startswith(b"%PDF")
    import pymupdf
    with pymupdf.open(res.pdf_path) as pdf:
        assert "Baseline pipeline" in "".join(p.get_text() for p in pdf)


def test_html_still_written_when_every_pdf_engine_fails(conn, tmp_path, monkeypatch):
    cycle(conn, T1)
    monkeypatch.setattr(digest, "_story_pdf", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(digest, "html_to_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no engines")))
    res = digest.write_digest(conn, tmp_path, "2026-W47")
    assert res.html_path.exists() and res.pdf_path is None and "no engines" in res.pdf_error


def test_inline_css_is_not_html_escaped(conn):
    html = digest.render_html(digest.build_context(conn))
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "&#34;" not in style and "&quot;" not in style and 'content: "Page "' in style
