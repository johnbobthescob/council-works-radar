"""Text layer (PLAN.md §2.1 [3]).

PDF -> per-page text plus word boxes (PyMuPDF). HTML -> cleaned text blocks (BeautifulSoup + lxml).
Pages with fewer than ``OCR_MIN_CHARS`` extractable characters are flagged ``ocr_needed`` (no OCR is
run here). ``pdftotext -layout`` (poppler) is an optional fallback for pages PyMuPDF cannot read.
Extracted text is cached as JSON lines in ``.cache/text/<doc_id>.jsonl`` (never committed).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from radar.settings import CACHE_DIR

OCR_MIN_CHARS = 50          # PLAN §2.1: image-only pages are < 50 chars
TEXT_LAYER_VERSION = 1


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int = 0
    line: int = 0
    word_no: int = 0

    @property
    def bbox(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]


@dataclass
class PageText:
    page: int                      # 1-based
    text: str
    words: list[Word] = field(default_factory=list)
    ocr_needed: bool = False
    source: str = "pymupdf"        # pymupdf | pdftotext

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


@dataclass
class Block:
    index: int
    kind: str                      # h1..h6, p, li, tr, pre, blockquote
    text: str
    level: int = 0                 # heading level, 0 otherwise
    char_start: int = 0            # offset into DocText.full_text
    char_end: int = 0


@dataclass
class DocText:
    kind: str                      # pdf | html
    pages: list[PageText] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    status: str = "ok"             # ok | partial | ocr_needed | failed
    error: str | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def ocr_needed_pages(self) -> list[int]:
        return [p.page for p in self.pages if p.ocr_needed]

    @property
    def full_text(self) -> str:
        if self.kind == "html":
            return "\n\n".join(b.text for b in self.blocks)
        return "\n\f".join(p.text for p in self.pages)

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.full_text.encode("utf-8")).hexdigest()


# ---- PDF ---------------------------------------------------------------------------------------
def pdftotext_pages(path: Path) -> list[str] | None:
    """``pdftotext -layout`` page texts, or None if poppler is missing / fails."""
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, timeout=120, check=True)
    except (subprocess.SubprocessError, OSError):
        return None
    pages = out.stdout.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def _status(pages: list[PageText]) -> str:
    if not pages:
        return "failed"
    flagged = sum(p.ocr_needed for p in pages)
    if flagged == len(pages):
        return "ocr_needed"
    return "partial" if flagged else "ok"


def extract_pdf(path: str | Path, *, use_pdftotext_fallback: bool = True) -> DocText:
    """Per-page text + words. PyMuPDF is used page by page (bounded memory); never raises on bad PDFs."""
    import pymupdf

    path = Path(path)
    pages: list[PageText] = []
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - corrupt/encrypted input must not crash a run
        fallback = pdftotext_pages(path) if use_pdftotext_fallback else None
        if fallback:
            pages = [PageText(i, t, [], len(t.strip()) < OCR_MIN_CHARS, "pdftotext") for i, t in enumerate(fallback, 1)]
            return DocText("pdf", pages=pages, status=_status(pages), error=f"pymupdf failed: {exc}")
        return DocText("pdf", status="failed", error=f"{type(exc).__name__}: {exc}")

    with doc:
        for i, page in enumerate(doc, start=1):
            raw_words = page.get_text("words")  # x0, y0, x1, y1, word, block, line, word_no
            words = [Word(round(w[0], 2), round(w[1], 2), round(w[2], 2), round(w[3], 2), w[4], w[5], w[6], w[7])
                     for w in raw_words]
            text = page.get_text("text")
            pages.append(PageText(i, text, words, ocr_needed=len(text.strip()) < OCR_MIN_CHARS))

    if use_pdftotext_fallback and any(p.ocr_needed for p in pages):
        alt = pdftotext_pages(path)
        if alt and len(alt) == len(pages):
            for p, t in zip(pages, alt):
                if p.ocr_needed and len(t.strip()) >= OCR_MIN_CHARS:
                    p.text, p.ocr_needed, p.source = t, False, "pdftotext"
    return DocText("pdf", pages=pages, status=_status(pages))


# ---- HTML --------------------------------------------------------------------------------------
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "blockquote", "dt", "dd")
_DROP_TAGS = ("script", "style", "noscript", "template", "svg", "head")


def _own_text(tag: Tag) -> str:
    """Text of ``tag`` excluding the subtrees of nested block elements (those become their own blocks)."""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif isinstance(child, Tag) and child.name not in _BLOCK_TAGS:
                if child.name in ("td", "th"):
                    parts.append(" | ")
                if child.name == "br":
                    parts.append(" ")
                walk(child)
                if child.name in ("td", "th"):
                    parts.append(" ")
    walk(tag)
    return " ".join("".join(parts).replace("|", " | ").split()).strip(" |") if tag.name == "tr" else " ".join("".join(parts).split())


def extract_html(source: str | bytes | Path) -> DocText:
    """Cleaned text blocks in document order, with character offsets into ``DocText.full_text``."""
    if isinstance(source, Path):
        source = source.read_bytes()
    soup = BeautifulSoup(source, "lxml")
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(["nav", "footer"]):
        tag.decompose()

    blocks: list[Block] = []
    offset = 0
    for tag in soup.find_all(_BLOCK_TAGS):
        text = _own_text(tag)
        if not text:
            continue
        level = int(tag.name[1]) if tag.name[0] == "h" and tag.name[1:].isdigit() else 0
        start = offset
        blocks.append(Block(len(blocks), tag.name, text, level, start, start + len(text)))
        offset = start + len(text) + 2  # "\n\n" separator
    page_text = "\n\n".join(b.text for b in blocks)
    status = "ok" if blocks else "failed"
    return DocText("html", pages=[PageText(1, page_text, [], ocr_needed=False, source="html")], blocks=blocks,
                   status=status, error=None if blocks else "no text blocks found")


# ---- cache + dispatch ------------------------------------------------------------------------
def text_cache_path(doc_id: str, cache_dir: Path | None = None) -> Path:
    return (Path(cache_dir) if cache_dir else CACHE_DIR) / "text" / f"{doc_id}.jsonl"


def save_text(doc_id: str, dt: DocText, cache_dir: Path | None = None) -> Path:
    path = text_cache_path(doc_id, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"_meta": True, "version": TEXT_LAYER_VERSION, "kind": dt.kind, "status": dt.status, "error": dt.error,
            "page_count": dt.page_count, "text_sha256": dt.text_sha256}
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta) + "\n")
        for p in dt.pages:
            fh.write(json.dumps({"page": p.page, "text": p.text, "ocr_needed": p.ocr_needed, "source": p.source,
                                 "words": [[w.x0, w.y0, w.x1, w.y1, w.text, w.block, w.line, w.word_no] for w in p.words]},
                                ensure_ascii=False) + "\n")
        for b in dt.blocks:
            fh.write(json.dumps({"block": asdict(b)}, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return path


def load_text(doc_id: str, cache_dir: Path | None = None) -> DocText | None:
    path = text_cache_path(doc_id, cache_dir)
    if not path.exists():
        return None
    meta, pages, blocks = None, [], []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("_meta"):
                meta = row
            elif "block" in row:
                blocks.append(Block(**row["block"]))
            else:
                pages.append(PageText(row["page"], row["text"], [Word(*w) for w in row["words"]],
                                      row["ocr_needed"], row["source"]))
    except (ValueError, KeyError, TypeError):
        return None
    if not meta or meta.get("version") != TEXT_LAYER_VERSION:
        return None
    return DocText(meta["kind"], pages=pages, blocks=blocks, status=meta["status"], error=meta["error"])


def get_text(path: str | Path, doc_id: str, content_type: str | None = None, *, cache_dir: Path | None = None,
             refresh: bool = False) -> DocText:
    """Extract (or load from ``.cache/text``) the text of a cached raw document."""
    if not refresh:
        cached = load_text(doc_id, cache_dir)
        if cached is not None:
            return cached
    path = Path(path)
    ctype = (content_type or "").lower()
    if "html" in ctype or path.suffix.lower() in (".html", ".htm"):
        dt = extract_html(path)
    else:
        dt = extract_pdf(path)
    save_text(doc_id, dt, cache_dir)
    return dt
