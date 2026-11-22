"""M0: the CLI lists its planned subcommands and stubs exit with a clear message."""

import pytest

from radar import cli


def test_help_lists_planned_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name, _, _ in cli.STUB_COMMANDS:
        assert name in out, f"{name} missing from --help"
    assert "config-check" in out and "link" in out


def test_config_check_exits_zero(capsys):
    assert cli.main(["config-check"]) == 0
    assert "councils: 6 configured" in capsys.readouterr().out


def test_stub_command_reports_not_implemented(capsys):
    import argparse

    assert cli._make_stub("future", "M99")(argparse.Namespace()) == cli.NOT_IMPLEMENTED_EXIT
    err = capsys.readouterr().err
    assert "not implemented" in err
    assert "M99" in err


def test_robots_check_offline_shows_blocklist_and_never_networks(capsys):
    assert cli.main(["robots-check", "--offline", "--url", "https://cdn.canning.wa.gov.au/x.pdf",
                     "--url", "https://www.swan.wa.gov.au/v1/api", "--url", "https://www.tenders.wa.gov.au/watenders/"]) == 0
    out = capsys.readouterr().out
    assert out.count("BLOCKED") == 3


def test_robots_check_offline_default_lists_configured_hosts(capsys):
    assert cli.main(["robots-check", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "joondalup" in out and "cdn.canning.wa.gov.au" in out and "UNKNOWN" in out


def test_robots_check_against_local_server(capsys, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            body = b"User-agent: *\nDisallow: /nope/\n"
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        from radar.fetch import Fetcher
        from radar.settings import load_councils
        f = Fetcher(config=load_councils(), cache_dir=tmp_path, log_path=None, pause_path=None,
                    sleep=lambda s: None)
        assert cli.robots_decision(f, base + "/ok", offline=False).startswith("ALLOWED")
        assert cli.robots_decision(f, base + "/nope/x", offline=False).startswith("DISALLOWED")
    finally:
        srv.shutdown()
