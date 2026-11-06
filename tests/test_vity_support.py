"""Unit tests for the provider's stateless helpers (payload/vity_support.py)."""

from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

import pytest

_SUPPORT = Path(__file__).resolve().parent.parent / "src" / "hermes_maximem_vity" / "payload" / "vity_support.py"
_spec = importlib.util.spec_from_file_location("vity_support_under_test", _SUPPORT)
support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(support)

NOW = 1_800_000_000.0  # fixed clock (seconds)


@pytest.mark.parametrize("age_s,label", [
    (5, "just now"), (120, "2m ago"), (3 * 3600, "3h ago"), (2 * 86400, "2d ago"),
    (45 * 86400, "1mo ago"), (400 * 86400, "1y ago"),
])
def test_age_label(age_s, label):
    assert support.age_label((NOW - age_s) * 1000, now_s=NOW) == label


@pytest.mark.parametrize("bad", [None, 0, -5, "x"])
def test_age_label_unknown_timestamp_is_blank(bad):
    assert support.age_label(bad, now_s=NOW) == ""


def test_format_memory_line_labels_age_and_relevance():
    line = support.format_memory_line(
        {"content": "User likes tea", "created_at": (NOW - 3 * 86400) * 1000, "score": 0.823}, now_s=NOW)
    assert line == "- [3d ago · 82%] User likes tea"
    assert support.format_memory_line({"content": "plain", "created_at": 0, "score": None}) == "- plain"


@pytest.mark.parametrize("stored,old,same", [
    ("User drinks masala chai every morning before standup.",
     "I drink masala chai every morning before standup.", True),
    ("User favourite code editor is Zed.", "favourite code editor is Zed", True),
    ("User favourite code editor is Zed.", "favourite code editor is VS Code", False),
    ("User lives in Pune.", "User works in Pune.", False),
    ("User likes tea.", "I don't like tea", False),              # negation must never match
    ("User lives in Pune.", "User does not live in Pune.", False),
    ("User drinks tea.", "User drinks tea and coffee.", False),  # a broader fact is a different fact
    ("", "anything", False),
])
def test_is_same_fact_is_strict(stored, old, same):
    assert support.is_same_fact(stored, old) is same


def test_classify_error_by_class_name_including_subclasses():
    class VityError(Exception): ...
    class VityAuthError(VityError): ...
    class CustomAuth(VityAuthError): ...
    kind, message = support.classify_error(CustomAuth("401"))
    assert kind == support.AUTH_ERROR and "hermes-maximem-vity install --api-key" in message
    assert support.classify_error(RuntimeError("boom")) == ("other", "boom")


@pytest.mark.parametrize("text,notice", [
    ("[CONTEXT COMPACTION] earlier turns were summarized", True),
    ("  [ASYNC DELEGATION COMPLETE] task 3 finished", True),
    ("A background subagent you dispatched earlier has finished.", True),
    ("I read about [CONTEXT COMPACTION] in the docs", False),   # quoted mid-message: real input
    ("remember that I prefer dark mode", False),
])
def test_is_hermes_notice(text, notice):
    assert support.is_hermes_notice(text) is notice


def test_pending_writes_round_trip_bounded_and_private(tmp_path, monkeypatch):
    path = tmp_path / support.PENDING_WRITES_FILE
    monkeypatch.setattr(support, "PENDING_WRITES_MAX", 3)
    support.save_pending_writes(path, [{"kind": "store", "content": f"fact {i}"} for i in range(5)])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with open(path, "a") as f:
        f.write("not json\n")                                  # a corrupt line is skipped
    jobs = support.take_pending_writes(path)
    assert [j["content"] for j in jobs] == ["fact 2", "fact 3", "fact 4"]   # newest kept
    assert not path.exists()                                   # taken: the caller re-queues them
    assert support.take_pending_writes(path) == []


def test_render_profile_card_never_cuts_mid_item():
    card = {"knowledge": {"skills": [{"value": "x" * 50, "confidence": 1.0}],
                          "expertise": [{"value": "short", "confidence": 1.0}]}}
    out = support.render_profile_card(card, max_chars=30)
    assert out == "- Expertise: short"
    assert json.dumps(out)  # plain text, serialisable
