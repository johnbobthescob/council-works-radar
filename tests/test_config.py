"""M0 acceptance tests: config loads and the compliance blocklist is present.

Offline only: no network access and no council documents (PLAN.md §11.1).
"""

import re
from pathlib import Path

import pytest
import yaml

from radar import settings

EXPECTED_COUNCILS = {"joondalup", "wanneroo", "stirling", "melville", "cockburn", "rockingham"}
EXPECTED_TRADES = {
    "ROAD_RESURF", "ROAD_CONST", "INTERSECTION", "DRAINAGE", "FOOTPATH", "KERBING",
    "LINE_MARK", "CARPARK", "BRIDGE", "STREETSCAPE", "OTHER_CIVIL", "NON_CIVIL", "UNKNOWN",
}
REPO_ROOT = Path(__file__).resolve().parent.parent


def test_councils_yaml_is_a_mapping():
    with settings.COUNCILS_YAML.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict)
    assert {"defaults", "councils", "global_blocklist"} <= data.keys()


def test_councils_load_and_match_plan():
    cfg = settings.load_councils()
    assert set(cfg.councils) == EXPECTED_COUNCILS
    assert cfg.defaults.min_delay_s == 5
    assert cfg.defaults.max_bytes == 80 * 1024 * 1024
    assert cfg.defaults.max_meeting_docs_per_run == 2


def test_every_enabled_council_has_at_least_one_adapter():
    cfg = settings.load_councils()
    enabled = [c for c in cfg.councils.values() if c.enabled]
    assert enabled, "expected at least one enabled council"
    for council in enabled:
        assert council.adapters, f"{council.key} is enabled but has no adapters"
        assert council.hosts, f"{council.key} has no hosts"
        for adapter in council.adapters:
            assert adapter.type, f"{council.key} has an adapter without a type"


def test_blocklist_contains_required_entries():
    blocklist = settings.load_councils().global_blocklist

    canning = [b for b in blocklist if b.host == "cdn.canning.wa.gov.au"]
    assert canning, "Canning CDN missing from global_blocklist"

    swan = [b for b in blocklist if b.host == "www.swan.wa.gov.au" and b.path_prefix == "/v1/"]
    assert swan, "Swan /v1/ missing from global_blocklist"

    tenders = [b for b in blocklist if "tenders.wa.gov.au" in b.host]
    assert tenders, "Tenders WA missing from global_blocklist"


def test_blocked_hosts_are_not_listed_as_fetchable_hosts():
    cfg = settings.load_councils()
    for council in cfg.councils.values():
        overlap = set(council.hosts) & set(council.blocked_hosts)
        assert not overlap, f"{council.key}: {overlap} is both fetchable and blocked"


def test_trades_load_with_full_taxonomy():
    trades = settings.load_trades()
    assert set(trades.trades) == EXPECTED_TRADES
    for trade in trades.trades.values():
        assert trade.label
        for pattern in trade.keywords + trade.negative:
            re.compile(pattern, re.IGNORECASE)  # every pattern must be a valid regex


def test_trades_negative_override_targets_non_civil():
    trades = settings.load_trades()
    resurf = trades.trades["ROAD_RESURF"]
    assert resurf.on_negative == "NON_CIVIL"
    assert any("tennis" in p for p in resurf.negative)


def test_trade_code_prefixes_and_context_rules():
    trades = settings.load_trades()
    assert trades.code_prefixes["joondalup"]["RPR"] == "ROAD_RESURF"
    assert trades.code_prefixes["joondalup"]["RDC"] == "ROAD_CONST"
    assert "drain" in trades.context_required["pipe"]
    assert "foot" in trades.context_required["path"]


def test_user_agent_is_honest_and_has_no_email():
    ua = settings.USER_AGENT
    assert ua.startswith("CouncilWorksRadar/")
    assert "respects robots.txt" in ua
    assert "@" not in ua
    assert "Mozilla" not in ua


def test_cache_directory_is_gitignored():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".cache/" in lines
    assert "data/" in lines


@pytest.mark.parametrize("name", ["config/councils.yaml", "config/trades.yaml"])
def test_config_files_exist(name):
    assert (REPO_ROOT / name).is_file()
