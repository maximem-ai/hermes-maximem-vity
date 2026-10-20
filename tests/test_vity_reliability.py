"""Provider behaviour added from the competitor review: write queue, mirrored
replace/remove, outage handling, notices, compression, setup/status."""

from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class VityAuthError(Exception):
    """Stands in for the SDK error (classified by class name)."""


class VityUnavailableError(Exception):
    pass


def _provider(vity_module, monkeypatch, client, **attrs):
    monkeypatch.setitem(sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: client))
    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    for k, v in attrs.items():
        setattr(provider, k, v)
    return provider


# -- write queue ------------------------------------------------------------------------------

def test_sync_turn_never_blocks_on_a_slow_write(vity_module, monkeypatch):
    release = threading.Event()
    client = MagicMock()
    client.capture.side_effect = lambda **_: release.wait(5)
    provider = _provider(vity_module, monkeypatch, client)

    t0 = time.monotonic()
    provider.sync_turn("first", "a", session_id="s")
    provider.sync_turn("second", "b", session_id="s")   # used to join the previous thread for 5s
    assert time.monotonic() - t0 < 0.5
    release.set()
    provider._write_queue.join()


def test_writes_are_delivered_in_order(vity_module, monkeypatch):
    order = []
    client = MagicMock()
    client.capture.side_effect = lambda **kw: order.append(("capture", kw["messages"][0]["content"]))
    client.store.side_effect = lambda **kw: order.append(("store", kw["content"]))
    provider = _provider(vity_module, monkeypatch, client)

    provider.sync_turn("turn one", "ok", session_id="s")
    provider.on_memory_write("add", "user", "likes tea")
    provider.sync_turn("turn two", "ok", session_id="s")
    provider._write_queue.join()

    assert order == [("capture", "turn one"), ("store", "likes tea"), ("capture", "turn two")]


def test_failed_write_is_saved_for_replay(vity_module, monkeypatch, tmp_path):
    monkeypatch.setattr(vity_module.time, "sleep", lambda s: None)
    down = MagicMock()
    down.store.side_effect = VityUnavailableError("down")
    provider = _provider(vity_module, monkeypatch, down, _pending_path=tmp_path / "pending.jsonl")

    provider.on_memory_write("add", "memory", "fact during an outage")
    provider._write_queue.join()
    assert down.store.call_count == 2                        # tried, then retried once
    assert "fact during an outage" in (tmp_path / "pending.jsonl").read_text()


def test_initialize_replays_pending_file_from_hermes_home(vity_module, monkeypatch, tmp_path):
    vity_module.support.save_pending_writes(
        tmp_path / vity_module.support.PENDING_WRITES_FILE,
        [{"kind": "store", "content": "saved last time", "memory_type": "fact"}])
    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    client = MagicMock()
    provider = _provider(vity_module, monkeypatch, client)
    monkeypatch.setattr(provider, "_warm_recall", lambda: None)

    provider.initialize("s1", hermes_home=str(tmp_path))
    provider._write_queue.join()

    client.store.assert_called_once_with(content="saved last time", memory_type="fact")
    assert not (tmp_path / vity_module.support.PENDING_WRITES_FILE).exists()


def test_auth_failure_is_not_saved_for_replay(vity_module, monkeypatch, tmp_path):
    monkeypatch.setattr(vity_module.time, "sleep", lambda s: None)
    client = MagicMock()
    client.store.side_effect = VityAuthError("401")
    provider = _provider(vity_module, monkeypatch, client, _pending_path=tmp_path / "p.jsonl")
    provider.on_memory_write("add", "memory", "x")
    provider._write_queue.join()
    assert not (tmp_path / "p.jsonl").exists()               # replaying a rejected key is pointless


def test_shutdown_saves_writes_it_could_not_finish(vity_module, monkeypatch, tmp_path):
    release = threading.Event()
    client = MagicMock()
    client.capture.side_effect = lambda **_: release.wait(5)
    provider = _provider(vity_module, monkeypatch, client, _pending_path=tmp_path / "p.jsonl")
    monkeypatch.setattr(vity_module, "_WRITE_DRAIN_TIMEOUT_S", 0.2)

    provider.sync_turn("stuck turn", "a", session_id="s")      # occupies the writer
    provider.sync_turn("queued turn", "b", session_id="s")     # still queued at exit
    provider.shutdown()
    release.set()

    assert "queued turn" in (tmp_path / "p.jsonl").read_text()


# -- mirrored replace / remove ----------------------------------------------------------------

def _remove_client(preview_ids, search_hits):
    client = MagicMock()
    client.forget.side_effect = lambda query, dry_run: {"ids": preview_ids, "count": len(preview_ids)}
    client.search.return_value = search_hits
    return client


def test_replace_stores_new_and_removes_exact_old(vity_module, monkeypatch):
    client = _remove_client(["m1"], [{"id": "m1", "content": "User drinks masala chai before standup."}])
    provider = _provider(vity_module, monkeypatch, client)

    provider.on_memory_write("replace", "user", "I drink green tea before standup.",
                             metadata={"old_text": "I drink masala chai before standup."})
    provider._write_queue.join()

    client.store.assert_called_once_with(content="I drink green tea before standup.", memory_type="preference")
    assert [c.kwargs["dry_run"] for c in client.forget.call_args_list] == [True, False]


def test_remove_is_skipped_when_the_delete_would_hit_other_memories(vity_module, monkeypatch):
    client = _remove_client(
        ["m1", "m2"],
        [{"id": "m1", "content": "User drinks masala chai before standup."},
         {"id": "m2", "content": "User drinks coffee after lunch."}])
    provider = _provider(vity_module, monkeypatch, client)

    provider.on_memory_write("remove", "user", "", metadata={"old_text": "I drink masala chai before standup."})
    provider._write_queue.join()

    assert [c.kwargs["dry_run"] for c in client.forget.call_args_list] == [True]   # never deletes


def test_remove_without_old_text_does_nothing(vity_module, monkeypatch):
    client = MagicMock()
    provider = _provider(vity_module, monkeypatch, client)
    provider.on_memory_write("remove", "user", "", metadata={})
    provider._write_queue.join()
    client.forget.assert_not_called()


# -- capture filtering ------------------------------------------------------------------------

def test_hermes_notices_are_not_captured(vity_module, monkeypatch):
    client = MagicMock()
    provider = _provider(vity_module, monkeypatch, client)
    provider.sync_turn("[CONTEXT COMPACTION] 40 earlier turns were summarized", "ok", session_id="s")
    provider._write_queue.join()
    client.capture.assert_not_called()


# -- outages and auth -------------------------------------------------------------------------

def test_rejected_key_is_told_to_the_model_once(vity_module, monkeypatch):
    client = MagicMock()
    client.search.side_effect = VityAuthError("401")
    provider = _provider(vity_module, monkeypatch, client)

    first = provider.prefetch("what do I like?")
    second = provider.prefetch("and what else do I like?")

    assert "Vity Memory is unavailable" in first and "API key was rejected" in first
    assert "unavailable" not in second
    assert client.search.call_count == 1                      # no more searches with a bad key


def test_recall_pauses_after_repeated_failures(vity_module, monkeypatch):
    client = MagicMock()
    client.search.side_effect = VityUnavailableError("down")
    provider = _provider(vity_module, monkeypatch, client)

    for i in range(vity_module._RECALL_FAILURES_TO_PAUSE + 2):
        provider.prefetch(f"question number {i} about my preferences")

    assert client.search.call_count == vity_module._RECALL_FAILURES_TO_PAUSE
    provider._recall_paused_until = 0.0                       # pause over: next turn re-checks
    provider.prefetch("question after the pause")
    assert client.search.call_count == vity_module._RECALL_FAILURES_TO_PAUSE + 1


def test_tool_errors_name_the_real_cause(vity_module, monkeypatch):
    client = MagicMock()
    client.search.side_effect = VityAuthError("401")
    provider = _provider(vity_module, monkeypatch, client)
    out = json.loads(provider.handle_tool_call("vity_recall", {"query": "editor"}))
    assert "rejected the API key" in out["error"]


def test_recall_tool_returns_age_and_relevance(vity_module, monkeypatch):
    client = MagicMock()
    client.search.return_value = [{"content": "User uses Zed", "score": 0.91,
                                   "created_at": (time.time() - 2 * 86400) * 1000}]
    provider = _provider(vity_module, monkeypatch, client)
    out = json.loads(provider.handle_tool_call("vity_recall", {"query": "editor"}))
    assert out["result"] == [{"memory": "User uses Zed", "age": "2d ago", "relevance": 0.91}]


def test_injected_memories_are_labelled_and_marked_as_already_retrieved(vity_module, monkeypatch):
    client = MagicMock()
    client.search.return_value = [{"content": "User uses Zed", "score": 0.82,
                                   "created_at": (time.time() - 3 * 86400) * 1000}]
    provider = _provider(vity_module, monkeypatch, client)
    out = provider.prefetch("which editor do I use?")
    assert "- [3d ago · 82%] User uses Zed" in out
    assert "do not call vity_recall" in out


# -- compression ------------------------------------------------------------------------------

def test_compression_reinjects_profile_but_keeps_conversation_state(vity_module):
    provider = vity_module.VityMemoryProvider()
    provider._profile_injected, provider._prev_query, provider._cold_start = True, "my trip", False
    provider.on_session_switch("child", parent_session_id="parent", reset=False, reason="compression")
    assert provider._profile_injected is False                # profile comes back after compaction
    assert provider._prev_query == "my trip" and provider._cold_start is False


# -- status -----------------------------------------------------------------------------------

def test_status_config_reports_live_connection(vity_module, monkeypatch):
    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    card = {"identity": {"name": {"value": "Ada", "confidence": 1.0}}, "preferences": [{"value": "tea", "confidence": 0.9}]}
    client = MagicMock()
    client.__enter__ = lambda self: self
    client.__exit__ = lambda self, *a: False
    client.get_profile_card.return_value = card
    monkeypatch.setitem(sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: client))

    status = vity_module.VityMemoryProvider().get_status_config({})

    assert status["api_key"] == "set"
    assert status["connection"] == "✓ Connected to Maximem · Profile Card: 2 fields"
