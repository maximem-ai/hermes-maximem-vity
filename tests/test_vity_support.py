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
    assert kind == support.AUTH_ERROR and "hermes-maximem-vity install --login" in message
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


# -- dates: when a memory happened, and message times as epoch ms ------------------------------

def test_happened_ms_prefers_when_it_was_said():
    assert support.happened_ms({"created_at": 7, "chat_at": 3, "happened_at": 3}) == 3
    assert support.happened_ms({"created_at": 7, "chat_at": 3}) == 3            # no happened_at
    assert support.happened_ms({"created_at": 7, "chat_at": None, "happened_at": 7}) == 7
    assert support.happened_ms({"created_at": 7}) == 7                           # SDK without the new fields
    assert support.happened_ms({}) is None


def test_format_memory_line_ages_by_when_it_happened():
    memory = {"content": "User lives in Berlin", "score": None,
              "created_at": (NOW - 60) * 1000, "chat_at": (NOW - 400 * 86400) * 1000}
    assert support.format_memory_line(memory, now_s=NOW) == "- [1y ago] User lives in Berlin"


_MS = 1_700_000_000_000  # 2023-11-14T22:13:20Z


@pytest.mark.parametrize("given", [1_700_000_000, 1_700_000_000.0, _MS, "2023-11-14T22:13:20Z",
                                   "2023-11-14T22:13:20+00:00", "2023-11-15T03:43:20+05:30"])
def test_epoch_ms_normalises(given):
    assert support.epoch_ms(given) == _MS


def test_epoch_ms_reads_a_zoneless_string_as_local_time():
    # Hermes stamps messages with datetime.now().isoformat(): local time, no zone.
    from datetime import datetime
    local = datetime.fromtimestamp(_MS / 1000).isoformat()
    assert support.epoch_ms(local) == _MS


@pytest.mark.parametrize("bad", [None, "", "garbage", "2023-13-45", True, 0, -5, float("nan"),
                                 float("inf"), [1], {"t": 1}])
def test_epoch_ms_unusable_is_none(bad):
    assert support.epoch_ms(bad) is None
