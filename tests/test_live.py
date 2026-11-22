"""Opt-in live checks (PLAN §11.3). Skipped unless RADAR_LIVE=1; run with ``RADAR_LIVE=1 pytest -q -m live``.

All requests go through one polite Fetcher (robots, >=5 s per host, honest User-Agent). Its cache, fetch log and
pause file live in a temporary directory, so these checks never touch data/ or .cache/.
"""

import os

import pytest

from radar.fetch import Fetcher, blocklist_match
from radar.settings import load_councils

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("RADAR_LIVE") != "1", reason="live checks need RADAR_LIVE=1")]

CONFIG = load_councils()
# one listing call per adapter type (PLAN §11.3): council that uses it
ADAPTER_SAMPLES = [("static_links", "cockburn"), ("cockburn_html", "cockburn"), ("infocouncil", "wanneroo"),
                   ("kentico_ksearch", "stirling"), ("joondalup_htmlresult", "joondalup")]


@pytest.fixture(scope="module")
def fetcher(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live")
    return Fetcher(config=CONFIG, cache_dir=tmp / "cache", log_path=tmp / "fetch_log.jsonl", pause_path=tmp / "pause.json")


def test_blocked_hosts_stay_blocked_without_contact(fetcher):
    for url in ("https://cdn.canning.wa.gov.au/x.pdf", "https://www.swan.wa.gov.au/v1/api",
                "https://www.tenders.wa.gov.au/watenders/", "https://search.joondalup.wa.gov.au/"):
        assert blocklist_match(url, CONFIG.global_blocklist, fetcher._blocked_hosts)
        assert fetcher.fetch(url).outcome == "blocked"


@pytest.mark.parametrize("council", [k for k, c in CONFIG.councils.items() if c.enabled])
def test_robots_allows_each_enabled_council(fetcher, council):
    host = CONFIG.councils[council].hosts[0]
    allowed, reason = fetcher.robots.can_fetch(f"https://{host}/")
    assert allowed, f"{host}: {reason}"


@pytest.mark.parametrize("adapter_type,council", ADAPTER_SAMPLES)
def test_one_listing_per_adapter_type(fetcher, adapter_type, council):
    """The adapter itself (no sitemap fallback), so a broken listing contract shows up here."""
    import importlib

    from radar.adapters import ADAPTER_MODULES

    c = CONFIG.councils[council]
    adapter = next(a for a in c.adapters if a.type == adapter_type)
    refs = importlib.import_module(ADAPTER_MODULES[adapter_type]).list_docs(c, adapter.raw, fetcher)
    assert refs, f"{council}/{adapter_type} listed nothing (the sitemap fallback would hide this in a run)"
    assert all(r.url.startswith("https://") for r in refs)


def test_melville_cwp_fetch_and_extract(fetcher, tmp_path):
    from radar import db
    from radar.adapters.static_links import docs_from_config
    from radar.ingest import fetch_and_extract

    melville = CONFIG.councils["melville"]
    cfg = next(a.raw for a in melville.adapters if a.type == "static_links")
    ref = next(r for r in docs_from_config("melville", cfg) if r.extractor == "melville_cwp")
    ref.extractor = ref.extractor or cfg.get("extractor")
    res = fetch_and_extract(fetcher, db.connect(tmp_path / "live.sqlite"), ref)
    assert res.outcome == "extracted" and res.mentions >= 1, res.detail
