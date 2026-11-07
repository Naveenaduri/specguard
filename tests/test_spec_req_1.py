"""Clause REQ-1 (Feature request): add a --version flag to the CLI that prints the package version and exits"""
import sys

import pytest

import specguard
import specguard.__main__


def test_version_attribute_present_and_string():
    assert hasattr(specguard, "__version__"), "specguard must expose a __version__ attribute"
    assert isinstance(specguard.__version__, str), "specguard.__version__ must be a string"
    assert specguard.__version__.strip() != "", "specguard.__version__ must not be empty"


def test_cli_version_flag_exits_zero_and_prints_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["specguard", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        specguard.__main__.main()

    assert excinfo.value.code in (0, None), f"Expected exit code 0 or None, got {excinfo.value.code}"

    captured = capsys.readouterr()
    expected_version = getattr(specguard, "__version__", "")
    assert expected_version in captured.out, (
        f"CLI output '{captured.out}' does not contain expected version '{expected_version}'"
    )
    assert captured.err == "", f"Expected empty stderr, got: {captured.err}"


def test_cli_version_flag_no_subcommand_required(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["specguard", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        specguard.__main__.main()

    assert excinfo.value.code == 0, (
        f"--version should exit with status 0 without requiring subcommands, but exited with {excinfo.value.code}"
    )
