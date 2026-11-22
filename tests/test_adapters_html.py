"""M6: HTML listing adapters on small hand-built fixtures (no network)."""

from pathlib import Path
from types import SimpleNamespace

from radar.adapters import cockburn_html, infocouncil, list_council, static_links
from radar.adapters.base import DocRef, parse_ecm, parse_size
from radar.dates import parse_date_text, parse_date_flexible
from radar.settings import load_councils

FIX = Path(__file__).parent / "fixtures"
INFO_URL = "https://wanneroo.infocouncil.biz/"
CK_URL = "https://www.cockburn.wa.gov.au/City-and-Council/About-Council-Meetings-and-Elections/Agendas-and-Minutes"


def test_date_helpers():
    assert parse_date_text("Council Agenda 27 Oct 2026 inc att") == "2026-10-27"
    assert parse_date_text("8 Septembe 2026 - Briefing") == "2026-09-08"
    assert parse_date_text("Minutes-17-November-2026-OMC") == "2026-11-17"
    assert parse_date_text("ECM_12915695_v3_Foo") is None
    assert parse_date_flexible("20261119") == "2026-11-19"
    assert parse_date_flexible("2026-11-19T00:00:00") == "2026-11-19"
    assert parse_date_flexible("/Date(1795046400000)/") == "2026-11-19"


def test_ecm_and_size():
    assert parse_ecm("/getattachment/x/ECM_12774885_v2_Project-Plans-pdf.aspx") == ("12774885", 2)
    assert parse_ecm("/nothing") == (None, None)
    assert parse_size("PDF 31.3 MB") == int(31.3 * 1048576)
    assert parse_size("(512 KB)") == 512 * 1024
    assert parse_size("1234") == 1234 and parse_size("n/a") is None


def test_infocouncil_prefers_htm_paper():
    refs = infocouncil.parse_listing("wanneroo", (FIX / "infocouncil_listing.html").read_text(), INFO_URL)
    by = {(r.meeting_date, r.doc_type, r.version_hint): r for r in refs}
    # 24 Nov agenda: PDF, _WEB frameset, _BMK, ATT_EXCLUDED all linked -> the derived .HTM paper wins.
    nov = by[("2026-11-24", "agenda", None)]
    assert nov.url == "https://wanneroo.infocouncil.biz/RedirectToDoc.aspx?URL=Open/2026/11/OC_24112026_AGN_AT.HTM"
    assert nov.fmt == "html" and nov.meeting_type == "Ordinary Council"
    # 27 Oct agenda: explicit .HTM linked beside PDF -> HTM.
    assert by[("2026-10-27", "agenda", None)].url.endswith("OC_27102026_AGN_AT.HTM")
    # Minutes only have a PDF.
    mins = by[("2026-10-27", "minutes", None)]
    assert mins.url.endswith("OC_27102026_MIN_AT.PDF") and mins.fmt == "pdf"
    # Supplementary paper is its own document.
    assert by[("2026-10-27", "agenda", "SUP")].url.endswith("_SUP.PDF")
    # No _BMK or Help links leak through.
    assert not any("_BMK" in r.url or "Help" in r.url for r in refs)
    # Newest first.
    assert refs[0].meeting_date == "2026-11-24"


def test_infocouncil_meeting_code_filter_and_attachments_excluded_fallback():
    html = (FIX / "infocouncil_listing.html").read_text()
    only_oc = infocouncil.parse_listing("wanneroo", html, INFO_URL, meeting_codes=["OC"])
    assert {r.meeting_type for r in only_oc} == {"Ordinary Council"}
    pdf_only = '''<a href="RedirectToDoc.aspx?URL=Open/2026/09/OC_01092026_AGN_AT.PDF">a</a>
                  <a href="RedirectToDoc.aspx?URL=Open/2026/09/OC_01092026_AGN_AT_ATT_EXCLUDED.PDF">b</a>'''
    (ref,) = infocouncil.parse_listing("wanneroo", pdf_only, INFO_URL)
    assert ref.url.endswith("_ATT_EXCLUDED.PDF")


def test_cockburn_listing():
    refs = cockburn_html.parse_listing("cockburn", (FIX / "cockburn_listing.html").read_text(), CK_URL)
    assert len(refs) == 3
    mins = next(r for r in refs if r.doc_type == "minutes")
    assert mins.url == ("https://www.cockburn.wa.gov.au/getattachment/a9c882fe-03ab-4938-bc8d-53ceab8e719b/"
                        "ECM_12915695_v3_Ordinary-Council-Meeting-Minutes-13-October-2026-pdf.aspx")
    assert (mins.ecm_id, mins.ecm_version, mins.version_hint) == ("12915695", 3, "ecm:12915695:v3")
    assert mins.meeting_date == "2026-10-13" and mins.size_bytes == int(31.3 * 1048576) and mins.fmt == "pdf"
    agenda = next(r for r in refs if r.doc_type == "agenda")
    assert agenda.meeting_date == "2026-11-10" and agenda.ecm_version == 1
    att = next(r for r in refs if r.doc_type == "attachments")
    assert att.size_bytes == 512 * 1024
    assert refs[0].meeting_date == "2026-11-10"


def test_cockburn_query_url():
    url = cockburn_html.build_listing_url(
        "https://x/Agendas", {"page": 1, "keyword": "OCM|Ordinary Council Meeting", "titleonly": True, "years": 2026})
    assert url == "https://x/Agendas?page=1&keyword=OCM|Ordinary%20Council%20Meeting&titleonly=true&years=2026"


def test_static_links_from_config():
    council = load_councils().councils["melville"]
    refs = static_links.docs_from_config("melville", council.adapters[0].raw)
    assert len(refs) == 1
    r = refs[0]
    assert r.doc_type == "capital_program" and r.extractor == "melville_cwp" and r.financial_year == "2026-27"
    assert r.fmt == "html" and r.url.endswith("/capital-works-program")


def test_static_links_discover_and_version_supersede():
    cfg = load_councils().councils["cockburn"].adapters[0].raw
    html = (FIX / "cockburn_strategies.html").read_text()
    found = static_links.discover_links("cockburn", html, "https://www.cockburn.wa.gov.au/Strategies-and-Plans",
                                        cfg["discover_pattern"], doc_type="project_plans", extractor="cockburn_plans")
    assert len(found) == 1                      # budget and service plans do not match
    assert found[0].ecm_version == 3 and found[0].financial_year == "2026-27"
    merged = static_links.merge_refs(static_links.docs_from_config("cockburn", cfg) + found)
    assert len(merged) == 1                     # configured v2 superseded by discovered v3
    assert merged[0].ecm_version == 3 and merged[0].extractor == "cockburn_plans" and merged[0].doc_type == "project_plans"


class FakeFetcher:
    """Serves fixture bytes by URL prefix; records requests."""

    def __init__(self, tmp_path, pages):
        self.tmp, self.pages, self.calls = tmp_path, pages, []

    def fetch(self, url, **kw):
        self.calls.append(url)
        for prefix, fname in self.pages.items():
            if url.startswith(prefix):
                p = self.tmp / "resp.html"
                p.write_bytes((FIX / fname).read_bytes())
                return SimpleNamespace(ok=True, path=p, read_bytes=p.read_bytes)
        return SimpleNamespace(ok=False, path=None)


def test_list_council_end_to_end_with_fake_fetcher(tmp_path):
    councils = load_councils()
    f = FakeFetcher(tmp_path, {"https://www.cockburn.wa.gov.au/Strategies-and-Plans": "cockburn_strategies.html",
                               "https://www.cockburn.wa.gov.au/City-and-Council": "cockburn_listing.html"})
    refs = list_council(councils.councils["cockburn"], f)
    assert any("Strategies-and-Plans" in c for c in f.calls)
    assert any("keyword=OCM|Ordinary%20Council%20Meeting" in c and "years=2026" in c for c in f.calls)
    plans = [r for r in refs if r.doc_type == "project_plans"]
    assert len(plans) == 1 and plans[0].extractor == "cockburn_plans"
    assert sum(r.doc_type in ("agenda", "minutes") for r in refs) == 2
    assert all(isinstance(r, DocRef) for r in refs)

    f2 = FakeFetcher(tmp_path, {"https://wanneroo.infocouncil.biz/": "infocouncil_listing.html"})
    refs = list_council(councils.councils["wanneroo"], f2)
    assert {r.meeting_type for r in refs} == {"Ordinary Council"}     # config: meeting_codes [OC]
    assert all(r.extractor == "agenda_items" for r in refs)


def test_cli_list_unknown_council(capsys):
    from radar.cli import main
    assert main(["list", "--council", "nowhere"]) == 2
    assert "unknown council" in capsys.readouterr().err


def test_format_docrefs():
    from radar.cli import format_docrefs
    refs = cockburn_html.parse_listing("cockburn", (FIX / "cockburn_listing.html").read_text(), CK_URL)
    out = format_docrefs(refs, 2)
    assert len(out.splitlines()) == 2 and "2026-11-10" in out
