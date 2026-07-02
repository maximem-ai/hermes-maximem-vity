"""Tests for installing the SDK into Hermes' OWN interpreter.

Regression guard for the "maximem-vity-sdk not installed" bug: a plain
`pip install` puts the SDK in whichever Python ran pip (often Anaconda), but
Hermes runs in its own venv and imports `maximem_vity` there. The installer
must resolve Hermes' interpreter and install into it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Import the installer from THIS repo's src, not any pip-installed copy.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hermes_maximem_vity import installer  # noqa: E402


# -- _hermes_python: resolving Hermes' interpreter ---------------------------

def test_resolves_venv_under_hermes_home(tmp_path, monkeypatch):
    """The standard layout: ~/.hermes/hermes-agent/venv/bin/python."""
    monkeypatch.setattr(installer, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    py = tmp_path / "hermes-agent" / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")  # existence is all _hermes_python checks

    assert installer._hermes_python() == str(py)


def test_resolves_python_from_bash_wrapper(tmp_path, monkeypatch):
    """`hermes` is a bash wrapper that execs a venv's hermes -> sibling python."""
    # No HERMES_HOME venv, so resolution must come from the launcher.
    monkeypatch.setattr(installer, "_hermes_home", lambda: tmp_path / "empty")

    venv = tmp_path / "elsewhere" / "venv"
    (venv / "bin").mkdir(parents=True)
    real_py = venv / "bin" / "python"
    real_py.write_text("")
    wrapper = tmp_path / "bin" / "hermes"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "unset PYTHONPATH\n"
        f'exec "{venv}/bin/hermes" "$@"\n'
    )
    monkeypatch.setattr(installer.shutil, "which", lambda _: str(wrapper))

    assert installer._hermes_python() == str(real_py)


def test_returns_none_when_unresolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_hermes_home", lambda: tmp_path / "nope")
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    assert installer._hermes_python() is None


# -- Windows layout (regression: SDK-not-installed on Windows) ---------------

def test_venv_python_layout_per_os(monkeypatch):
    # Build the input Path first; pathlib.Path("...") would try to make a
    # WindowsPath (and raise) once os.name is faked to "nt" on a POSIX host.
    v = Path("v")
    monkeypatch.setattr(installer.os, "name", "nt")
    assert installer._venv_python(v).as_posix() == "v/Scripts/python.exe"
    monkeypatch.setattr(installer.os, "name", "posix")
    assert installer._venv_python(v).as_posix() == "v/bin/python"


def test_windows_resolves_scripts_python(tmp_path, monkeypatch):
    """On Windows the interpreter is venv\\Scripts\\python.exe, not bin/python."""
    py = tmp_path / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(installer.os, "name", "nt")
    monkeypatch.setattr(installer, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    assert installer._hermes_python() == str(py)


# -- _install_sdk_into_hermes: install/skip behaviour ------------------------

def test_install_skips_when_already_importable(monkeypatch):
    """If Hermes' python already imports maximem_vity, do not shell out to pip."""
    monkeypatch.setattr(installer, "_hermes_python", lambda: sys.executable)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # First call is the `import maximem_vity` probe -> succeed.
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    assert installer._install_sdk_into_hermes() is True
    # Only the import probe ran — no `pip install`.
    assert len(calls) == 1
    assert any("import maximem_vity" in str(c) for c in calls)
    assert not any("install" in str(c) for c in calls)


def test_install_runs_pip_then_reverifies(monkeypatch):
    monkeypatch.setattr(installer, "_hermes_python", lambda: "/fake/python")
    seq = iter([1, 0, 0, 0])  # import-probe fails, pip --version ok, pip install ok, reverify ok

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, next(seq), b"", b"")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    assert installer._install_sdk_into_hermes() is True


def test_install_returns_false_when_no_interpreter(monkeypatch):
    monkeypatch.setattr(installer, "_hermes_python", lambda: None)
    assert installer._install_sdk_into_hermes() is False
