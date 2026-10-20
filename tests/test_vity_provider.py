"""Unit tests for the standalone Vity memory provider."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


def test_name_is_maximem_vity(vity_module):
    assert vity_module.VityMemoryProvider().name == "maximem_vity"


def test_is_available_requires_api_key(vity_module, monkeypatch):
    monkeypatch.delenv("MAXIMEM_API_KEY", raising=False)
    monkeypatch.delenv("VITY_API_KEY", raising=False)
    assert vity_module.VityMemoryProvider().is_available() is False

    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    monkeypatch.setitem(sys.modules, "maximem_vity", SimpleNamespace())  # SDK importable
    assert vity_module.VityMemoryProvider().is_available() is True


def test_unavailable_reason_names_the_fix(vity_module, monkeypatch):
    monkeypatch.delenv("MAXIMEM_API_KEY", raising=False)
    monkeypatch.delenv("VITY_API_KEY", raising=False)
    assert "MAXIMEM_API_KEY" in vity_module.VityMemoryProvider().unavailable_reason()

    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    monkeypatch.delitem(sys.modules, "maximem_vity", raising=False)
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert "maximem-vity-sdk is not installed" in vity_module.VityMemoryProvider().unavailable_reason()


def test_config_loads_key_and_tunable_defaults(vity_module, monkeypatch):
    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    cfg = vity_module._load_config()

    assert cfg["api_key"] == "mx_test_key"
    assert cfg["auto_recall"] is True
    assert cfg["auto_capture"] is True
    assert cfg["max_recall_tokens"] == 1000
    assert cfg["min_prompt_length"] == 5


def test_config_schema_offers_key_and_typed_tunables(vity_module):
    schema = vity_module.VityMemoryProvider().get_config_schema()
    assert schema[0]["key"] == "api_key" and schema[0]["secret"] is True
    assert schema[0]["env_var"] == "MAXIMEM_API_KEY"
    tunables = {item["key"]: item for item in schema[1:]}
    assert set(tunables) == {"auto_recall", "auto_capture", "profile_warmup",
                             "recall_min_score", "recall_deadline", "max_recall_tokens"}
    assert all("type" in f and "default" in f and not f.get("secret") for f in tunables.values())


def test_post_setup_saves_only_tunables_and_reports_connection(vity_module, monkeypatch, tmp_path, capsys):
    provider = vity_module.VityMemoryProvider()
    monkeypatch.setattr(provider, "_connection_summary", lambda home=None: "✓ Connected to Maximem")
    provider.post_setup(str(tmp_path), {"recall_min_score": 0.5, "api_key": "mx_secret", "junk": 1})
    saved = json.loads((tmp_path / "vity.json").read_text())
    assert saved == {"recall_min_score": 0.5}               # the secret never lands in vity.json
    assert "✓ Connected to Maximem" in capsys.readouterr().out


def test_tool_schemas_are_static(vity_module):
    # Hermes routes tools from get_tool_schemas() ONCE, before initialize(). The
    # meeting tool must therefore always be listed (update/delete stay gated on
    # the router route, which is a build-time constant).
    provider = vity_module.VityMemoryProvider()
    names = {t["name"] for t in provider.get_tool_schemas()}
    assert names == {"vity_recall", "vity_profile", "vity_store", "vity_forget",
                     "vity_knowledge", "vity_meeting_recall"}
    provider._meetings_enabled = True
    assert {t["name"] for t in provider.get_tool_schemas()} == names


def test_meeting_tool_reports_not_enabled_without_calling_ask(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.meetings_enabled.return_value = False
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    out = json.loads(provider.handle_tool_call("vity_meeting_recall", {"question": "what did we decide?"}))

    assert "not enabled" in out["result"] and "Do not retry" in out["result"]
    fake_client.meeting_ask.assert_not_called()


def test_forget_requires_a_query(vity_module, monkeypatch):
    fake_client = MagicMock()
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    out = json.loads(provider.handle_tool_call("vity_forget", {"query": "  ", "dry_run": False}))
    assert "Missing required parameter: query" in out["error"]
    fake_client.forget.assert_not_called()


def test_crud_tools_gated_off_by_default(vity_module):
    # Router route for /memory/{id} not live yet -> update/delete not advertised.
    names = {t["name"] for t in vity_module.VityMemoryProvider().get_tool_schemas()}
    assert "vity_update" not in names
    assert "vity_delete" not in names


def test_knowledge_tool_dispatch(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.knowledge.return_value = {
        "items": [{"topic": "astronomy", "topic_category": "science", "summary": "likes stars"}]
    }
    provider = vity_module.VityMemoryProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: fake_client)

    out = json.loads(provider.handle_tool_call("vity_knowledge", {"query": "space"}))
    fake_client.knowledge.assert_called_once()
    assert fake_client.knowledge.call_args.kwargs["scope"] == "chat"
    assert out["result"][0]["topic"] == "astronomy"
    assert out["result"][0]["summary"] == "likes stars"


def test_meeting_recall_dispatch(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.meeting_ask.return_value = {"answer": "We decided to ship Friday."}
    provider = vity_module.VityMemoryProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: fake_client)

    out = json.loads(provider.handle_tool_call(
        "vity_meeting_recall", {"question": "what did we decide?", "status": "open"}))
    fake_client.meeting_ask.assert_called_once()
    assert fake_client.meeting_ask.call_args.kwargs["status"] == "open"
    assert out["result"]["answer"].startswith("We decided")


def test_client_built_with_hermes_channel(vity_module, monkeypatch):
    constructor = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "maximem_vity", SimpleNamespace(VityClient=constructor))

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    provider._get_client()

    # SDK 0.3.x defaults channel/agent_id to "unspecified"; Hermes must self-identify,
    # since the server builds the conversation id as "{channel}:{sessionKey}".
    constructor.assert_called_once_with(api_key="mx_test_key", channel="hermes", agent_id="hermes")


def test_store_tool_dispatch(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.store.return_value = {"id": "mem_123", "stored": True}
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    out = json.loads(provider.handle_tool_call("vity_store", {"content": "I like apples"}))

    assert out["id"] == "mem_123"
    fake_client.store.assert_called_once()


def test_forget_dry_run_does_not_confirm(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.forget.return_value = {"count": 3, "ids": ["a", "b", "c"]}
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    out = json.loads(provider.handle_tool_call("vity_forget", {"query": "old"}))

    assert "Would delete 3" in out["result"]
    fake_client.forget.assert_called_once_with(query="old", dry_run=True)


def test_auto_capture_disabled_skips_sync(vity_module, monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )

    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    provider._auto_capture = False
    provider.sync_turn("hi", "hello", session_id="s1")

    fake_client.capture.assert_not_called()


def test_register_entry_point(vity_module):
    captured = {}

    class Ctx:
        def register_memory_provider(self, provider):
            captured["provider"] = provider

    vity_module.register(Ctx())
    assert captured["provider"].name == "maximem_vity"


def test_system_prompt_mentions_api_key_scope(vity_module):
    block = vity_module.VityMemoryProvider().system_prompt_block()
    assert "configured Maximem API key" in block


def _provider_with_client(vity_module, monkeypatch, fake_client):
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )
    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    return provider


def test_prefetch_uses_search_not_recall(vity_module, monkeypatch):
    # The bug: auto-recall used recall() (misses specific memories). It must
    # use search() (the same call the vity_recall tool / `hermes vity search`
    # make), so stored memories are injected automatically, every turn.
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "User loves dark mode", "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    injected = provider.prefetch("what theme do I like?")

    fake_client.search.assert_called_once()
    fake_client.recall.assert_not_called()
    assert "## Vity Memory" in injected
    assert "User loves dark mode" in injected


def test_prefetch_never_calls_recall_endpoint(vity_module, monkeypatch):
    # Auto-recall must NOT touch recall(), it's slow/unhealthy (times out in
    # prod) and misses specifics. Empty search → inject nothing this turn.
    fake_client = MagicMock()
    fake_client.search.return_value = []
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    assert provider.prefetch("tell me about my projects") == ""
    fake_client.search.assert_called_once()
    fake_client.recall.assert_not_called()


def test_prefetch_degrades_when_endpoint_slow(vity_module, monkeypatch):
    # A hanging/timing-out endpoint must degrade to "no memory this turn",
    # never raise or block the caller.
    fake_client = MagicMock()
    fake_client.search.side_effect = RuntimeError("Request to /v1/memory/recall timed out after 6s")
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    assert provider.prefetch("what did I say about the deadline?") == ""
    fake_client.recall.assert_not_called()


def test_recall_client_built_with_short_timeout(vity_module, monkeypatch):
    # Auto-recall uses a short-timeout client so a slow backend can't block turns.
    captured = {}

    def fake_ctor(**kwargs):
        captured.update(kwargs)
        return MagicMock(search=MagicMock(return_value=[]))

    monkeypatch.setitem(sys.modules, "maximem_vity", SimpleNamespace(VityClient=fake_ctor))
    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    provider._recall_timeout = 6.0

    provider.prefetch("hello there, anything on file?")

    assert captured.get("timeout") == 6.0


def test_prefetch_skips_synthesized_placeholders(vity_module, monkeypatch):
    # A sparse/fresh account: search returns the "empty profile" sentinel and a
    # profile-summary blob. Neither is a real memory, so nothing is injected;
    # and we must NOT inject "memory profile is empty" as if it were a memory.
    fake_client = MagicMock()
    fake_client.search.return_value = [
        {"content": "Your memory profile is empty.", "type": None},
        {"content": "some summary", "type": "context"},
    ]
    fake_client.recall.return_value = ""
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    assert provider.prefetch("who am I?") == ""


def test_prefetch_disabled_returns_empty(vity_module, monkeypatch):
    fake_client = MagicMock()
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._auto_recall = False

    assert provider.prefetch("anything") == ""
    fake_client.search.assert_not_called()


def test_prefetch_caps_injected_context_to_max_recall_tokens(vity_module, monkeypatch):
    # max_recall_tokens bounds the injected size (~4 chars/token) so a big
    # memory set can't crowd out the conversation.
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "x" * 500, "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._max_recall_tokens = 10  # ~40-char budget

    out = provider.prefetch("tell me everything")

    assert out.count("x") <= 40
    assert out.count("x") > 0          # an oversized memory is truncated, not dropped


# --------------------------------------------------------------------------
# #1: writes must be skipped in non-primary (cron/flush/subagent) contexts
# --------------------------------------------------------------------------

def _init_with_stub_client(vity_module, monkeypatch, **init_kwargs):
    """Initialize a provider with a stubbed SDK and a key set, then return it."""
    fake_client = MagicMock(search=MagicMock(return_value=[]))
    monkeypatch.setitem(
        sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: fake_client)
    )
    monkeypatch.setenv("MAXIMEM_API_KEY", "mx_test_key")
    provider = vity_module.VityMemoryProvider()
    provider.initialize("s1", **init_kwargs)
    return provider, fake_client


def test_cron_context_disables_writes(vity_module, monkeypatch):
    provider, fake_client = _init_with_stub_client(vity_module, monkeypatch, agent_context="cron")
    assert provider._write_enabled is False

    provider.sync_turn("hi", "hello", session_id="s1")
    fake_client.capture.assert_not_called()

    provider.on_memory_write("add", "user", "I like tea")
    fake_client.store.assert_not_called()


def test_subagent_and_flush_contexts_disable_writes(vity_module, monkeypatch):
    for ctx in ("flush", "subagent"):
        provider, _ = _init_with_stub_client(vity_module, monkeypatch, agent_context=ctx)
        assert provider._write_enabled is False, ctx


def test_platform_cron_disables_writes(vity_module, monkeypatch):
    provider, _ = _init_with_stub_client(vity_module, monkeypatch, platform="cron")
    assert provider._write_enabled is False


def test_primary_context_keeps_writes_enabled(vity_module, monkeypatch):
    provider, fake_client = _init_with_stub_client(vity_module, monkeypatch, agent_context="primary")
    assert provider._write_enabled is True

    provider.sync_turn("hi", "hello", session_id="s1")
    provider._write_queue.join()
    fake_client.capture.assert_called_once()


def test_no_context_defaults_to_writes_enabled(vity_module, monkeypatch):
    # A plain CLI turn passes no agent_context, writes must stay on.
    provider, _ = _init_with_stub_client(vity_module, monkeypatch)
    assert provider._write_enabled is True


# --------------------------------------------------------------------------
# #2: cold-start warm-up + wall-clock recall deadline
# --------------------------------------------------------------------------

def test_initialize_warms_recall_in_background(vity_module, monkeypatch):
    provider, fake_client = _init_with_stub_client(vity_module, monkeypatch, agent_context="primary")
    # The warm-up thread fires one throwaway search off-turn.
    if provider._prefetch_thread:
        provider._prefetch_thread.join(timeout=2)
    fake_client.search.assert_called_once()


def test_recall_deadline_caps_slow_prefetch(vity_module, monkeypatch):
    import time

    fake_client = MagicMock()

    def slow_search(*_a, **_k):
        time.sleep(1.0)
        return [{"content": "too late", "score": 0.9}]

    fake_client.search.side_effect = slow_search
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._recall_deadline = 0.1  # give up long before the 1s search returns

    assert provider.prefetch("anything on file?") == ""


# --------------------------------------------------------------------------
# #4: honour the host-injected hermes_home
# --------------------------------------------------------------------------

def test_load_config_honors_explicit_hermes_home(vity_module, tmp_path):
    custom = tmp_path / "profile_home"
    custom.mkdir()
    (custom / "vity.json").write_text(json.dumps({"min_prompt_length": 99}))

    cfg = vity_module._load_config(str(custom))
    assert cfg["min_prompt_length"] == 99


# --------------------------------------------------------------------------
# #5: on_session_switch re-arms cold_start only on a genuine reset
# --------------------------------------------------------------------------

def test_session_reset_rearms_cold_start(vity_module):
    provider = vity_module.VityMemoryProvider()
    provider._cold_start = False
    provider.on_session_switch("s2", reset=True)
    assert provider._cold_start is True


def test_session_resume_keeps_cold_start_off(vity_module):
    provider = vity_module.VityMemoryProvider()
    provider._cold_start = False
    provider.on_session_switch("s2", reset=False)
    assert provider._cold_start is False


# --------------------------------------------------------------------------
# #6: the recall indicator goes through the host (recall_status), never stdout
# --------------------------------------------------------------------------

def test_recall_status_reports_last_prefetch(vity_module, monkeypatch, capsys):
    monkeypatch.setattr(vity_module, "RecallStatus", lambda label, count: (label, count))
    fake_client = MagicMock()
    fake_client.search.side_effect = [
        [{"content": "User likes tea", "score": 0.9}, {"content": "User lives in Pune", "score": 0.8}],
        [],
    ]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider.prefetch("what do I drink and where do I live?")
    assert provider.recall_status() == ("Maximem Vity", 2)

    provider.prefetch("unrelated question with no memories")
    assert provider.recall_status() is None              # nothing injected -> no indicator
    assert capsys.readouterr().out == ""                  # no direct terminal output


# --------------------------------------------------------------------------
# #8: store dispatch tolerates a None result from the SDK
# --------------------------------------------------------------------------

def test_store_tolerates_none_result(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.store.return_value = None
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    out = json.loads(provider.handle_tool_call("vity_store", {"content": "hi"}))
    assert out["id"] == ""


# ---------------------------------------------------------------------------
# Enhancements #1-#4: knowledge dedup, tool guidance, cold-start blend, score floor
# ---------------------------------------------------------------------------

def test_knowledge_dispatch_dedupes_by_topic(vity_module, monkeypatch):
    # The backend can return several near-identical rows for one
    # topic (summaries differing by a few chars). Collapse to one per topic;
    # the first (rank-ordered) wins, so the agent isn't handed repeats.
    fake_client = MagicMock()
    fake_client.knowledge.return_value = {"items": [
        {"topic": "astronomy", "topic_category": "science", "summary": "likes stars"},
        {"topic": "astronomy", "topic_category": "science", "summary": "likes stars and planets"},
        {"topic": "cooking", "topic_category": "hobby", "summary": "home cook"},
    ]}
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    out = json.loads(provider.handle_tool_call("vity_knowledge", {"query": "interests"}))
    assert len(out["result"]) == 2                       # astronomy collapsed, cooking kept
    assert out["result"][0]["summary"] == "likes stars"  # top-ranked row survives


def test_prefetch_applies_min_score_floor(vity_module, monkeypatch):
    # Auto-recall must pass its relevance floor to search so weak matches don't
    # get injected as if they were fact.
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "User loves dark mode", "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._recall_min_score = 0.3

    provider.prefetch("what theme do I like?")

    assert fake_client.search.call_args.kwargs["min_score"] == 0.3




def test_vity_recall_tool_stays_unfiltered(vity_module, monkeypatch):
    # The explicit tool is an agent-driven request, it must NOT apply the
    # auto-recall floor (min_score defaults to 0 there).
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "a niche fact", "score": 0.15}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    out = json.loads(provider.handle_tool_call("vity_recall", {"query": "niche"}))
    assert fake_client.search.call_args.kwargs.get("min_score", 0.0) == 0.0
    assert "a niche fact" in json.dumps(out)


def test_system_prompt_guides_recall_vs_knowledge(vity_module):
    block = vity_module.VityMemoryProvider().system_prompt_block()
    assert "vity_recall" in block
    assert "vity_knowledge" in block


# ---------------------------------------------------------------------------
# Anaphoric follow-up query heuristic
# ---------------------------------------------------------------------------

def test_needs_prior_context_classifier(vity_module):
    needs = vity_module.support.needs_prior_context
    # Follow-ups that can't be searched alone:
    assert needs("why?")
    assert needs("tell me more")
    assert needs("what about that?")
    assert needs("how does it work?")
    assert needs("the second one")
    # Self-contained messages, must NOT augment (no dilution of clean topics):
    assert not needs("what's my sister's birthday?")
    assert not needs("I love Python")
    assert not needs("remind me about the database migration schedule")
    assert not needs("")


def test_prefetch_augments_followup_with_prior_message(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "M", "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    # Turn 1: a substantial question becomes the anchor.
    provider.prefetch("what did we decide about the Postgres migration?")
    # Turn 2: an anaphoric follow-up is enriched with the anchor.
    provider.prefetch("tell me more")

    second_query = fake_client.search.call_args_list[1].kwargs["query"]
    assert "Postgres migration" in second_query      # prior context folded in
    assert "tell me more" in second_query


def test_prefetch_does_not_augment_clean_topic_change(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "M", "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider.prefetch("what did we decide about the Postgres migration?")
    provider.prefetch("what's my sister's birthday?")   # self-contained, new topic

    second_query = fake_client.search.call_args_list[1].kwargs["query"]
    assert "Postgres" not in second_query               # NOT diluted
    assert second_query == "what's my sister's birthday?"


def test_prefetch_anchor_persists_across_followup_chain(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "M", "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider.prefetch("what did we decide about the Postgres migration?")
    provider.prefetch("tell me more")   # follow-up 1, must NOT overwrite the anchor
    provider.prefetch("why?")           # follow-up 2, still anchored to Postgres

    third_query = fake_client.search.call_args_list[2].kwargs["query"]
    assert "Postgres migration" in third_query


def test_prefetch_searches_short_followup_instead_of_skipping(vity_module, monkeypatch):
    # "why?" is under min_prompt_length. Before: skipped -> no memory. Now: it's
    # augmented with the anchor and searched.
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "M", "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._min_prompt_length = 5
    provider._cold_start = False

    provider._prev_query = "the Postgres migration plan"
    out = provider.prefetch("why?")

    fake_client.search.assert_called_once()             # not skipped
    assert "Postgres" in fake_client.search.call_args.kwargs["query"]
    assert "## Vity Memory" in out


def test_session_reset_clears_anchor(vity_module):
    provider = vity_module.VityMemoryProvider()
    provider._prev_query = "old topic"
    provider.on_session_switch("new-sess", reset=True)
    assert provider._prev_query == ""


# ---------------------------------------------------------------------------
# Session-start user-profile warm-load + one-time injection
# ---------------------------------------------------------------------------

def _v(value, confidence=0.9, **extra):
    return {"value": value, "confidence": confidence, "source_ids": ["m1"], **extra}


CARD = {
    "identity": {"name": _v("Ada Lovelace", 1.0), "email": _v("ada@example.com", 1.0)},
    "professional": {"current_company": _v("Analytical Engines"), "current_role": _v("Engineer"),
                     "company_history": [_v("Old Co")], "role_history": []},
    "personal": {"location": _v("London"), "location_history": [], "education": [],
                 "interests": [_v("poetry"), _v("chess", 0.4)]},
    "knowledge": {"expertise": [_v("algorithms")], "top_domains": [], "skills": [_v("Python")]},
    "goals": {"active": [_v("Publish the notes")], "completed": [_v("Old goal")]},
    "preferences": [_v("concise answers"), _v("tea", 0.2, is_override=True)],
    "candidates": [{"field_key": "location", **_v("Paris")}],
}


def test_render_profile_card_current_state_only(vity_module):
    out = vity_module.support.render_profile_card(CARD)
    assert out.splitlines()[:3] == [
        "- Name: Ada Lovelace",
        "- Work: Engineer at Analytical Engines",
        "- Location: London",
    ]
    assert "- Skills: Python" in out and "- Active goals: Publish the notes" in out
    assert "poetry" in out and "chess" not in out           # below the confidence floor
    assert "tea" in out                                      # manual override always kept
    for absent in ("ada@example.com", "Paris", "Old Co", "Old goal"):
        assert absent not in out                              # email, candidates, history


def test_render_profile_card_empty_and_malformed(vity_module):
    render = vity_module.support.render_profile_card
    assert render({}) == ""
    assert render(None) == ""
    assert render({"identity": "oops", "preferences": [None, {"value": ""}]}) == ""


def test_render_profile_card_keeps_whole_lines_under_cap(vity_module, monkeypatch):
    out = vity_module.support.render_profile_card(CARD, max_chars=60)
    assert len(out) <= 60
    assert all(line.startswith("- ") and ": " in line for line in out.splitlines())


def test_render_profile_card_caps_items_per_field(vity_module):
    many = {"knowledge": {"skills": [_v(f"skill{i}") for i in range(12)]}}
    line = vity_module.support.render_profile_card(many)
    assert line.count(";") == vity_module.support.PROFILE_INJECT_MAX_ITEMS - 1


def test_load_profile_renders_card(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.get_profile_card.return_value = CARD
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider._load_profile()

    assert provider._profile_context.startswith("- Name: Ada Lovelace")
    assert provider._profile_fields == len(provider._profile_context.splitlines())
    fake_client.get_profile_card.assert_called_once()
    fake_client.recall.assert_not_called()                  # no /recall-based profile anymore


def test_load_profile_empty_card_injects_nothing(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.get_profile_card.return_value = {"identity": {"name": None}, "preferences": []}
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider._load_profile()

    assert provider._profile_context == ""
    provider._load_profile()                                 # empty is not cached: retried
    assert fake_client.get_profile_card.call_count == 2


def test_load_profile_failure_is_non_fatal(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.get_profile_card.side_effect = RuntimeError("route down")
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider._load_profile()                                 # must not raise

    assert provider._profile_context == ""


def test_load_profile_reuses_cache_within_ttl(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.get_profile_card.return_value = CARD
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider._load_profile()
    provider._load_profile()   # second call within TTL must NOT re-fetch

    fake_client.get_profile_card.assert_called_once()


def test_load_profile_disabled_makes_no_call(vity_module, monkeypatch):
    fake_client = MagicMock()
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._warm_profile = False

    provider._load_profile()

    fake_client.get_profile_card.assert_not_called()


def test_vity_profile_tool_returns_card_rows(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.get_profile_card.return_value = CARD
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    out = json.loads(provider.handle_tool_call("vity_profile", {}))

    assert out["result"]["Name"] == ["Ada Lovelace"]
    assert out["result"]["Work"] == ["Engineer at Analytical Engines"]
    assert "ada@example.com" not in json.dumps(out)
    fake_client.get_profile.assert_not_called()             # deprecated SDK helper unused


def test_vity_profile_tool_empty_card(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.get_profile_card.return_value = {}
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    out = json.loads(provider.handle_tool_call("vity_profile", {}))

    assert out["result"].startswith("No profile yet")


def test_profile_fields_reported_in_telemetry(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.search.return_value = []
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._profile_context, provider._profile_fields = "- Name: Ada\n- Location: London", 2
    events = []
    monkeypatch.setattr(provider, "_track", lambda event, props=None: events.append((event, props)))

    provider.prefetch("hello there")
    provider.prefetch("and again")

    recalls = [p for e, p in events if e == "recall_performed"]
    assert recalls[0]["profile_injected"] is True and recalls[0]["profile_fields"] == 2
    assert recalls[1]["profile_fields"] == 0                 # only reported when injected


def test_prefetch_injects_profile_once_as_background_block(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.search.return_value = [{"content": "User loves dark mode", "score": 0.9}]
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._profile_context = "The user is a backend engineer working on an AI memory SDK."

    first = provider.prefetch("what theme do I like?")
    second = provider.prefetch("anything else?")

    # Turn 1: profile block present, framed as background, plus the memory block.
    assert "About the user" in first
    assert "backend engineer" in first
    assert "## Vity Memory" in first
    # Turn 2: profile NOT repeated.
    assert "About the user" not in second


def test_prefetch_injects_profile_even_if_memory_empty(vity_module, monkeypatch):
    fake_client = MagicMock()
    fake_client.search.return_value = []          # sparse: no discrete memories
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    provider._profile_context = "The user is a vegetarian who builds side projects."

    out = provider.prefetch("hey there")
    assert "About the user" in out
    assert "vegetarian" in out


def test_session_reset_rearms_profile_injection(vity_module):
    provider = vity_module.VityMemoryProvider()
    provider._profile_context = "cached profile"
    provider._profile_injected = True
    provider.on_session_switch("new", reset=True)
    assert provider._profile_injected is False       # will re-inject
    assert provider._profile_context == "cached profile"  # cache preserved


# ---------------------------------------------------------------------------
# Memory writes must survive process exit
# ---------------------------------------------------------------------------

def test_shutdown_waits_for_inflight_memory_write(vity_module, monkeypatch):
    # Regression: the built-in-memory mirror ran on an untracked daemon thread, so a
    # fact saved just before exit (e.g. a one-shot turn) was killed before store().
    import threading
    import time as _time
    stored = threading.Event()
    fake_client = MagicMock()
    fake_client.store.side_effect = lambda **_: (_time.sleep(0.3), stored.set())
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)

    provider.on_memory_write("add", "user", "favourite editor is Zed")
    provider.shutdown()

    assert stored.is_set(), "shutdown returned before the mirrored store() completed"
    fake_client.store.assert_called_once_with(content="favourite editor is Zed", memory_type="preference")


def test_shutdown_write_drain_is_bounded(vity_module, monkeypatch):
    import threading
    import time as _time
    release = threading.Event()
    fake_client = MagicMock()
    fake_client.store.side_effect = lambda **_: release.wait(5)
    provider = _provider_with_client(vity_module, monkeypatch, fake_client)
    monkeypatch.setattr(vity_module, "_WRITE_DRAIN_TIMEOUT_S", 0.2)

    provider.on_memory_write("add", "memory", "slow fact")
    t0 = _time.monotonic()
    provider.shutdown()
    assert _time.monotonic() - t0 < 2.0          # a wedged backend never blocks exit
    release.set()


def test_exit_drain_registered_once(vity_module, monkeypatch):
    registered = []
    monkeypatch.setattr(vity_module.atexit, "register", lambda fn: registered.append(fn))
    provider = vity_module.VityMemoryProvider()
    monkeypatch.setattr(provider, "_warm_recall", lambda: None)

    provider.initialize("s1")
    provider.initialize("s2")                    # /new or a second session in one process

    assert registered == [provider.shutdown]
