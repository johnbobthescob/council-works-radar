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
}


def list_council(council, fetcher) -> list[DocRef]:
    """All DocRefs for one council, from every configured adapter. Adapter failures are not swallowed."""
    refs: list[DocRef] = []
    for adapter in council.adapters:
        module = importlib.import_module(ADAPTER_MODULES[adapter.type])
        for ref in module.list_docs(council, adapter.raw, fetcher):
            ref.extractor = ref.extractor or adapter.raw.get("extractor")
            refs.append(ref)
    return refs


__all__ = ["DocRef", "list_council", "ADAPTER_MODULES"]
