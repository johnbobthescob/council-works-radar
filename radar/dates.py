"""Date parsing helpers shared by adapters and extractors. Pure functions; results are ISO ``YYYY-MM-DD``."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# "27 Oct 2026", "17-November-2026", "8 Septembe 2026" (typos tolerated: only the first 3 letters count).
_D_MON_Y = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?[\s\-_,]+([A-Za-z]{3,9})\.?[\s\-_,]+(\d{4})(?!\d)")
_MON_Y = re.compile(r"\b([A-Za-z]{3,9})\.?[\s\-_,]+(\d{4})(?!\d)")
_DMY_NUM = re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)")
_COMPACT8 = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")      # DDMMYYYY (Infocouncil)
_YMD8 = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")          # YYYYMMDD (Rockingham)
_ISO = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
_MS_DATE = re.compile(r"/Date\((-?\d+)")


def _mk(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_date_text(text: str | None) -> str | None:
    """First day-month-year date found in free text (titles, filenames), or None."""
    if not text:
        return None
    for m in _D_MON_Y.finditer(text):
        mon = MONTHS.get(m.group(2)[:3].lower())
        if mon:
            iso = _mk(int(m.group(3)), mon, int(m.group(1)))
            if iso:
                return iso
    m = _DMY_NUM.search(text)
    if m:
        return _mk(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def parse_month_year(text: str | None) -> str | None:
    """'July 2026' -> '2026-07-01' (first of the month); used for project start/end dates."""
    for m in _MON_Y.finditer(text or ""):
        mon = MONTHS.get(m.group(1)[:3].lower())
        if mon:
            return _mk(int(m.group(2)), mon, 1)
    return None


def parse_date_flexible(value: object) -> str | None:
    """Date from a JSON value: ISO, '/Date(ms)/', YYYYMMDD, dd/mm/yyyy or 'd Month yyyy'."""
    if value is None or value == "":
        return None
    s = str(value).strip()
    m = _ISO.search(s)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _MS_DATE.search(s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()
    if re.fullmatch(r"\d{8}", s):
        m = _YMD8.fullmatch(s)
        if m:
            return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return parse_date_text(s)


def parse_compact_ddmmyyyy(s: str) -> str | None:
    m = _COMPACT8.fullmatch(s)
    return _mk(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
