"""Listing adapters (PLAN.md §3.1): turn a council listing into ``DocRef``s.

Each adapter module exposes ``list_docs(council, cfg, fetcher) -> list[DocRef]`` plus pure ``parse_*``
functions that the offline tests drive with fixtures.
"""

from __future__ import annotations

import importlib

from radar.adapters.base import DocRef

ADAPTER_MODULES = {
    "static_links": "radar.adapters.static_links",
    "cockburn_html": "radar.adapters.cockburn_html",
    "infocouncil": "radar.adapters.infocouncil",
    "kentico_ksearch": "radar.adapters.kentico_ksearch",
    "joondalup_htmlresult": "radar.adapters.joondalup_htmlresult",
    "sitemap": "radar.adapters.sitemap",
}


def list_adapter(council, adapter, fetcher) -> tuple[list[DocRef], list[str], list[str]]:
    """DocRefs of one adapter, with the optional sitemap ``fallback`` (PLAN M18). Returns (refs, errors, notes).

    The fallback runs when the adapter raises or lists nothing. If the adapter raises and there is no fallback the
    exception propagates; a failing fallback propagates too.
    """
    cfg = adapter.raw
    failure: Exception | None = None
    try:
        refs = importlib.import_module(ADAPTER_MODULES[adapter.type]).list_docs(council, cfg, fetcher)
    except Exception as exc:
        if not cfg.get("fallback"):
            raise
        failure, refs = exc, []
    errors: list[str] = []
    notes: list[str] = []
    if not refs and cfg.get("fallback"):
        fb = {"type": "sitemap", **cfg["fallback"]}
        if not fb.get("extractor") and cfg.get("extractor"):
            fb["extractor"] = cfg["extractor"]
        refs = importlib.import_module(ADAPTER_MODULES[fb["type"]]).list_docs(council, fb, fetcher)
        for ref in refs:
            ref.extractor = ref.extractor or fb.get("extractor")
        if failure is not None:
            errors.append(f"adapter {adapter.type} failed ({type(failure).__name__}: {failure}); "
                          f"used {fb['type']} fallback ({len(refs)} docs)")
        else:
            notes.append(f"adapter {adapter.type} listed no documents; used {fb['type']} fallback ({len(refs)} docs)")
    return refs, errors, notes


def list_council(council, fetcher) -> list[DocRef]:
    """All DocRefs for one council, from every configured adapter. Adapter failures are not swallowed."""
    refs: list[DocRef] = []
    for adapter in council.adapters:
        got, _errors, _notes = list_adapter(council, adapter, fetcher)
        for ref in got:
            ref.extractor = ref.extractor or adapter.raw.get("extractor")
            refs.append(ref)
    return refs


__all__ = ["DocRef", "list_council", "list_adapter", "ADAPTER_MODULES"]
