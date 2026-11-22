"""Extractors (PLAN.md §4.3): ``extract(doc_text, source_document) -> list[ProjectMention]`` per module."""

from __future__ import annotations

import importlib

IMPLEMENTED = {
    "melville_cwp": "radar.extract.melville_cwp",
    "cockburn_plans": "radar.extract.cockburn_plans",
    "joondalup_budget": "radar.extract.joondalup_budget",
    "agenda_items": "radar.extract.agenda_items",
}


def get_extractor(name: str):
    """The ``extract`` callable for ``name``; KeyError if the extractor is not built yet (M17)."""
    if name not in IMPLEMENTED:
        raise KeyError(f"extractor {name!r} is not implemented yet")
    return importlib.import_module(IMPLEMENTED[name]).extract
