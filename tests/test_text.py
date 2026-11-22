"""M3: text layer on a synthetic PDF generated in-test (no council documents are committed)."""

import pymupdf
import pytest

from radar import text as T


@pytest.fixture
def pdf_path(tmp_path):
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 100), "Project Number   Cost Code   Project Name", fontsize=11)
    p1.insert_text((72, 120), "RPR3542   W5033   Warwick Road (westbound) resurfacing and rehabilitation works", fontsize=11)
    doc.new_page()                                   # page 2: totally empty
    p3 = doc.new_page()                              # page 3: image only, no text layer
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60))
    pix.clear_with(200)
    p3.insert_image(pymupdf.Rect(72, 72, 272, 272), pixmap=pix)
    path = tmp_path / "synthetic.pdf"
    doc.save(path)
    doc.close()
    return path


def test_pdf_pages_words_and_bboxes(pdf_path):
    dt = T.extract_pdf(pdf_path, use_pdftotext_fallback=False)
    assert dt.kind == "pdf" and dt.page_count == 3
    p1 = dt.pages[0]
    assert "RPR3542" in p1.text and "Warwick Road" in p1.text
    by_text = {w.text: w for w in p1.words}
    anchor, cost = by_text["RPR3542"], by_text["W5033"]
    assert anchor.x0 < cost.x0                       # columns left to right
    assert anchor.y0 == pytest.approx(cost.y0, abs=2)
    assert 72 <= anchor.x0 < 80 and anchor.x1 > anchor.x0 and anchor.y1 > anchor.y0
    header_y = by_text["Number"].y0
    assert header_y < anchor.y0                      # header above the row
    assert not p1.ocr_needed


def test_empty_and_image_pages_flagged_ocr_needed(pdf_path):
    dt = T.extract_pdf(pdf_path, use_pdftotext_fallback=False)
    assert [p.ocr_needed for p in dt.pages] == [False, True, True]
    assert dt.ocr_needed_pages == [2, 3]
    assert dt.status == "partial"


def test_all_blank_pdf_status_ocr_needed(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    path = tmp_path / "blank.pdf"
    doc.save(path)
    assert T.extract_pdf(path).status == "ocr_needed"


def test_corrupt_pdf_does_not_raise(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    dt = T.extract_pdf(bad, use_pdftotext_fallback=False)
    assert dt.status == "failed" and dt.error


def test_pdftotext_fallback_does_not_hurt(pdf_path):
    dt = T.extract_pdf(pdf_path)                     # uses poppler if present, harmless otherwise
    assert dt.page_count == 3 and "RPR3542" in dt.pages[0].text


HTML = b"""<html><head><title>x</title><style>p{}</style></head><body>
<nav><a href='/'>Home</a></nav>
<h1>Capital Works Program 2026-27</h1>
<p>Alvan Street Resurfacing ($250,000) Resurfacing of the road.</p>
<ul><li>Footpath renewal
  <ul><li>Leach Highway shared path</li></ul></li></ul>
<table><tr><th>Road</th><th>Value</th></tr><tr><td>Kardan Dr</td><td>$1,200</td></tr></table>
<script>var a = 1;</script><footer>Copyright</footer></body></html>"""


def test_html_blocks():
    dt = T.extract_html(HTML)
    kinds = [(b.kind, b.text) for b in dt.blocks]
    assert ("h1", "Capital Works Program 2026-27") in kinds
    assert ("p", "Alvan Street Resurfacing ($250,000) Resurfacing of the road.") in kinds
    assert ("li", "Footpath renewal") in kinds and ("li", "Leach Highway shared path") in kinds
    assert ("tr", "Road | Value") in kinds and ("tr", "Kardan Dr | $1,200") in kinds
    assert not any("Home" in t or "var a" in t or "Copyright" in t for _, t in kinds)
    assert dt.blocks[0].level == 1
    # offsets index into full_text
    for b in dt.blocks:
        assert dt.full_text[b.char_start:b.char_end] == b.text


def test_html_empty_is_failed():
    assert T.extract_html(b"<html><body></body></html>").status == "failed"


def test_text_cache_roundtrip_and_dispatch(pdf_path, tmp_path):
    cache = tmp_path / "cache"
    dt = T.get_text(pdf_path, "abc123", "application/pdf", cache_dir=cache)
    assert T.text_cache_path("abc123", cache).exists()
    pdf_path.unlink()                                # second call must come from the cache, not the file
    again = T.get_text(pdf_path, "abc123", "application/pdf", cache_dir=cache)
    assert again.page_count == 3 and again.ocr_needed_pages == dt.ocr_needed_pages
    assert again.pages[0].words[0].text == dt.pages[0].words[0].text
    assert again.text_sha256 == dt.text_sha256


def test_html_cache_dispatch(tmp_path):
    f = tmp_path / "cwp.html"
    f.write_bytes(HTML)
    dt = T.get_text(f, "h1", "text/html; charset=utf-8", cache_dir=tmp_path)
    assert dt.kind == "html"
    assert T.load_text("h1", tmp_path).blocks[0].text.startswith("Capital Works")
