"""`hermes-maximem-vity uninstall` undoes what install did.

Install copies the plugin, sets ``memory.provider = maximem_vity`` through the hermes CLI and
writes MAXIMEM_API_KEY to $HERMES_HOME/.env. Uninstall used to remove only the plugin folder,
leaving Hermes pointed at a provider that no longer exists and a key that made a later
reinstall skip sign-in.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_maximem_vity import installer  # noqa: E402

# A stand-in `hermes` CLI: memory.provider lives in $HERMES_HOME/provider, every call is logged.
_FAKE_HERMES = """#!/bin/sh
echo "$@" >> "$HERMES_HOME/calls"
if [ "$1 $2 $3" = "config get memory.provider" ]; then cat "$HERMES_HOME/provider"; exit 0; fi
if [ "$1 $2 $3" = "config set memory.provider" ]; then printf '%s\\n' "$4" > "$HERMES_HOME/provider"; exit 0; fi
exit 2
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    hermes = bindir / "hermes"
    hermes.write_text(_FAKE_HERMES)
    hermes.chmod(hermes.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    plugin = tmp_path / "plugins" / installer.PLUGIN_NAME
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("")
    return tmp_path


def _provider(home: Path) -> str:
    return (home / "provider").read_text().strip()


def test_uninstall_clears_our_provider(home, capsys):
    (home / "provider").write_text("maximem_vity\n")

    assert installer.cmd_uninstall() == 0

    assert _provider(home) == ""
    assert not (home / "plugins" / installer.PLUGIN_NAME).exists()
    assert "Cleared memory.provider (was maximem_vity)" in capsys.readouterr().out


def test_uninstall_leaves_another_provider_alone(home, capsys):
    (home / "provider").write_text("honcho\n")

    installer.cmd_uninstall()

    assert _provider(home) == "honcho"
    assert "config set" not in (home / "calls").read_text()
    assert "memory.provider" not in capsys.readouterr().out


def test_uninstall_without_hermes_cli_prints_the_manual_command(home, monkeypatch, capsys):
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)

    assert installer.cmd_uninstall() == 0

    assert 'hermes config set memory.provider ""' in capsys.readouterr().out


def test_uninstall_removes_only_the_api_key_line(home, capsys):
    (home / "provider").write_text("other\n")
    env = home / ".env"
    env.write_text(
        "# my settings\n"
        "OPENAI_API_KEY=sk-keep\n"
        "MAXIMEM_API_KEY=mx_secret\n"
        "\n"
        "  MAXIMEM_API_KEY=mx_dupe\n"
        "VITY_BASE_URL=https://example.test\n"
        "NOT_MAXIMEM_API_KEY=keep\n"
    )
    env.chmod(0o600)

    installer.cmd_uninstall()

    assert env.read_text() == (
        "# my settings\n"
        "OPENAI_API_KEY=sk-keep\n"
        "\n"
        "VITY_BASE_URL=https://example.test\n"
        "NOT_MAXIMEM_API_KEY=keep\n"
    )
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert f"Removed MAXIMEM_API_KEY from {env}" in capsys.readouterr().out


def test_uninstall_without_key_leaves_env_untouched(home, capsys):
    (home / "provider").write_text("other\n")
    env = home / ".env"
    env.write_text("OPENAI_API_KEY=sk-keep")  # no trailing newline, kept byte for byte

    installer.cmd_uninstall()

    assert env.read_text() == "OPENAI_API_KEY=sk-keep"
    assert "MAXIMEM_API_KEY" not in capsys.readouterr().out


def test_reinstall_after_uninstall_asks_for_a_key_again(home, monkeypatch):
    monkeypatch.delenv("MAXIMEM_API_KEY", raising=False)
    monkeypatch.delenv("VITY_API_KEY", raising=False)
    (home / "provider").write_text("maximem_vity\n")
    installer._write_api_key("mx_old")
    assert installer._key_already_set()

    installer.cmd_uninstall()

    assert not installer._key_already_set()
