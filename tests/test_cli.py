"""Tests for the ``hermes maximem_vity`` CLI rendering — clean, predictable output.

The CLI module (payload/cli.py) is loaded by file path: at runtime it lives in
``~/.hermes/plugins/maximem_vity/cli.py`` and only imports the host lazily, so it loads
standalone without the Hermes stubs the provider needs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_CLI_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "hermes_maximem_vity" / "payload" / "cli.py"
)


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("vity_cli_under_test", _CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_client(**methods):
    """A stand-in VityClient with .close() and whatever methods are passed."""
    methods.setdefault("close", lambda *a, **k: None)
    return SimpleNamespace(**methods)


# -- status row alignment ----------------------------------------------------

def test_rows_align_values_in_one_column(cli):
    short = cli._row("SDK", "ok")
    longest = cli._row("max_recall_tokens", "1000")
    # Value starts at the same column for short and longest labels.
    assert short.index("ok") == longest.index("1000")
    # The longest label still has a gap before its value (the old bug).
    assert "max_recall_tokens: " in longest


# -- search: synthesized "context" blob is hidden in the human view ----------

def test_search_hides_context_pseudo_result(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_make_client",
        lambda: _fake_client(search=lambda **_: [
            {"type": "context", "content": "profile summary blob", "score": 1.0},
        ]),
    )
    cli._cmd_search("anything", 10, as_json=False)
    out = capsys.readouterr().out
    assert "No memories found." in out
    assert "profile summary blob" not in out


def test_search_hides_empty_profile_sentinel(cli, monkeypatch, capsys):
    """The `type: null` 'profile is empty' sentinel is not a real hit."""
    monkeypatch.setattr(
        cli, "_make_client",
        lambda: _fake_client(search=lambda **_: [
            {"type": None, "score": 0.30,
             "content": "User's Vity memory profile is empty with no personal information saved."},
        ]),
    )
    cli._cmd_search("Groq", 10, as_json=False)
    out = capsys.readouterr().out
    assert "No memories found." in out
    assert "profile is empty" not in out


def test_search_shows_real_typeless_memories(cli, monkeypatch, capsys):
    """Real memories can have type=null — those must NOT be filtered out."""
    monkeypatch.setattr(
        cli, "_make_client",
        lambda: _fake_client(search=lambda **_: [
            {"type": None, "content": "User finished debugging at 2:30 AM", "score": 0.6},
        ]),
    )
    cli._cmd_search("debugging", 10, as_json=False)
    out = capsys.readouterr().out
    assert "User finished debugging at 2:30 AM" in out


def test_search_shows_real_memories(cli, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_make_client",
        lambda: _fake_client(search=lambda **_: [
            {"type": "fact", "content": "I prefer dark mode", "score": 0.91},
            {"type": "fact", "content": "I prefer dark mode", "score": 0.90},  # dup
        ]),
    )
    cli._cmd_search("theme", 10, as_json=False)
    out = capsys.readouterr().out
    assert out.count("I prefer dark mode") == 1  # deduped
    assert "[0.91] (fact)" in out


def test_search_json_is_unfiltered(cli, monkeypatch, capsys):
    """--json shows everything, including context blobs, for power users."""
    monkeypatch.setattr(
        cli, "_make_client",
        lambda: _fake_client(search=lambda **_: [
            {"type": "context", "content": "blob", "score": 1.0},
        ]),
    )
    cli._cmd_search("q", 5, as_json=True)
    assert "context" in capsys.readouterr().out


# -- forget: correct pluralization + friendly empty message ------------------

@pytest.mark.parametrize(
    "count, confirm, expected",
    [
        (1, True, "Deleted 1 memory."),
        (2, True, "Deleted 2 memories."),
        (1, False, "Would delete 1 memory."),
        (3, False, "Would delete 3 memories."),
        (0, False, "Nothing matches — no memories to delete."),
    ],
)
def test_forget_messages(cli, monkeypatch, capsys, count, confirm, expected):
    monkeypatch.setattr(
        cli, "_make_client",
        lambda: _fake_client(forget=lambda **_: {"count": count, "ids": []}),
    )
    cli._cmd_forget("q", confirm)
    assert expected in capsys.readouterr().out
