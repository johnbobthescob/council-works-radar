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
    assert "config-check" in out


def test_config_check_exits_zero(capsys):
    assert cli.main(["config-check"]) == 0
    assert "councils: 6 configured" in capsys.readouterr().out


def test_stub_command_reports_not_implemented(capsys):
    assert cli.main(["link"]) == cli.NOT_IMPLEMENTED_EXIT
    err = capsys.readouterr().err
    assert "not implemented" in err
    assert "M12" in err
