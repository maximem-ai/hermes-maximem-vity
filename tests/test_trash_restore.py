"""Forget moves memories to the trash (restorable 30 days); agents only file deletion requests.

Server contract (memoryinfra #597/#600): a dry run returns the matches and a token; a delete with that
token trashes exactly what the dry run listed; ``mode: "request"`` (forced by the X-Maximem-Agent header)
files a request the user approves in the dashboard; POST /v1/memory/restore undoes a forget.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

import vity_support as support

_CLI_PATH = Path(__file__).resolve().parent.parent / "src" / "hermes_maximem_vity" / "payload" / "cli.py"

PREVIEW = {
    "count": 2,
    "ids": ["m1", "m2"],
    "memories": [{"id": "m1", "preview": "User drinks masala chai", "category": "preference"},
                 {"id": "m2", "preview": "User drinks chai at 4pm", "category": "fact"}],
    "token": "tok-1",
    "tokenExpiresAt": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
}
DELETED = {"count": 2, "ids": ["m1", "m2"], "deletionId": "del-1",
           "restorableUntil": "2026-12-18T00:00:00+00:00", "notFound": []}


class _CodedError(Exception):
    """An SDK error carrying the API's error code."""

    def __init__(self, code):
        super().__init__(f"refused ({code})")
        self.code = code


# -- api_post: the one place that reaches past SDK 0.3.x's forget() ---------------------------

def _sdk_client(handler):
    from maximem_vity import VityClient
    return VityClient(api_key="mx_test_key", endpoint="http://vity.test", max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_api_post_sends_body_auth_and_agent_header():
    seen = {}

    def handler(request):
        seen.update(headers=request.headers, body=json.loads(request.content), path=request.url.path)
        return httpx.Response(200, json={"requestId": "r1", "count": 1, "status": "pending"})

    out = support.api_post(_sdk_client(handler), "/v1/memory/forget", {"query": "chai", "mode": "request"},
                           agent=True)

    assert out["requestId"] == "r1"
    assert seen["path"] == "/v1/memory/forget"
    assert seen["body"] == {"query": "chai", "mode": "request"}
    assert seen["headers"]["authorization"] == "Bearer mx_test_key"
    assert seen["headers"]["x-maximem-agent"] == "mcp"


def test_api_post_without_agent_sends_no_agent_header():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json={})

    support.api_post(_sdk_client(handler), "/v1/memory/restore", {"deletionId": "d"})
    assert "x-maximem-agent" not in seen["headers"]


def test_api_post_maps_refusals_to_sdk_errors_with_their_code():
    def handler(request):
        return httpx.Response(422, json={"detail": {"error": {"code": "too_many_matches",
                                                              "message": "31 memories match", "count": 31}}})

    with pytest.raises(Exception) as exc:
        support.api_post(_sdk_client(handler), "/v1/memory/forget", {"query": "x", "dryRun": True})
    assert exc.value.code == "too_many_matches"
    assert "too many memories match" in support.forget_error(exc.value).lower()


@pytest.mark.parametrize("code, words", [
    ("too_many_matches", "app.maximem.ai"),
    ("search_unavailable", "nothing was deleted"),
    ("delete_unavailable", "nothing was deleted"),
    ("token_used", "already used"),
    ("invalid_token", "expired"),
])
def test_forget_errors_have_plain_messages(code, words):
    assert words in support.forget_error(_CodedError(code))


def test_unknown_errors_have_no_forget_message():
    assert support.forget_error(_CodedError("rate_limited")) is None
    assert support.forget_error(ValueError("x")) is None


# -- CLI ---------------------------------------------------------------------------------------

@pytest.fixture
def cli(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("vity_cli_trash_test", _CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_preview_path", lambda: tmp_path / "vity_forget_preview.json")
    monkeypatch.setattr(mod, "_make_client", lambda: SimpleNamespace(close=lambda: None))
    return mod


def _api(monkeypatch, cli, *responses):
    """Answer cli's api_post calls in order; return the list of (path, body, agent) sent."""
    calls, queue = [], list(responses)

    def fake(client, path, body, agent=False):
        calls.append((path, body, agent))
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(cli.support, "api_post", fake)
    return calls


def test_forget_preview_lists_matches_and_keeps_the_token(cli, monkeypatch, capsys):
    calls = _api(monkeypatch, cli, PREVIEW)

    cli._cmd_forget("chai", confirm=False)

    out = capsys.readouterr().out
    assert calls == [("/v1/memory/forget", {"query": "chai", "dryRun": True}, False)]
    assert "m1" in out and "User drinks masala chai" in out and "m2" in out
    assert "Would move 2 memories to trash" in out and "--yes" in out
    saved = json.loads(cli._preview_path().read_text())
    assert saved["query"] == "chai" and saved["token"] == "tok-1"


def test_forget_yes_after_preview_deletes_exactly_the_preview(cli, monkeypatch, capsys):
    _api(monkeypatch, cli, PREVIEW)
    cli._cmd_forget("chai", confirm=False)
    calls = _api(monkeypatch, cli, DELETED)

    cli._cmd_forget("chai", confirm=True)

    assert calls == [("/v1/memory/forget", {"query": "chai", "token": "tok-1"}, False)]
    out = capsys.readouterr().out
    assert ("Moved 2 memories to trash. Restore within 30 days with "
            "`hermes maximem_vity restore del-1` or at app.maximem.ai.") in out
    assert not cli._preview_path().exists()   # a token is single use


def test_forget_yes_without_a_preview_previews_then_deletes_with_that_token(cli, monkeypatch, capsys):
    calls = _api(monkeypatch, cli, PREVIEW, DELETED)

    cli._cmd_forget("chai", confirm=True)

    assert calls == [("/v1/memory/forget", {"query": "chai", "dryRun": True}, False),
                     ("/v1/memory/forget", {"query": "chai", "token": "tok-1"}, False)]
    out = capsys.readouterr().out
    assert "User drinks masala chai" in out and "Moved 2 memories to trash" in out


def test_forget_yes_ignores_a_preview_for_another_query(cli, monkeypatch):
    cli._preview_path().write_text(json.dumps({"query": "other", "token": "tok-x",
                                               "tokenExpiresAt": PREVIEW["tokenExpiresAt"]}))
    calls = _api(monkeypatch, cli, PREVIEW, DELETED)
    cli._cmd_forget("chai", confirm=True)
    assert calls[1][1] == {"query": "chai", "token": "tok-1"}


def test_forget_yes_ignores_an_expired_preview(cli, monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    cli._preview_path().write_text(json.dumps({"query": "chai", "token": "tok-old", "tokenExpiresAt": past}))
    calls = _api(monkeypatch, cli, PREVIEW, DELETED)
    cli._cmd_forget("chai", confirm=True)
    assert calls[1][1] == {"query": "chai", "token": "tok-1"}


def test_forget_yes_never_deletes_without_a_token(cli, monkeypatch, capsys):
    calls = _api(monkeypatch, cli, {**PREVIEW, "token": None})
    cli._cmd_forget("chai", confirm=True)
    assert len(calls) == 1                     # the dry run only
    assert "Nothing was deleted" in capsys.readouterr().out


def test_forget_no_matches(cli, monkeypatch, capsys):
    _api(monkeypatch, cli, {"count": 0, "ids": [], "memories": []})
    cli._cmd_forget("chai", confirm=True)
    assert "No matching memories to forget." in capsys.readouterr().out


def test_forget_one_memory_is_singular(cli, monkeypatch, capsys):
    one = {**PREVIEW, "count": 1, "ids": ["m1"], "memories": PREVIEW["memories"][:1]}
    _api(monkeypatch, cli, one, {**DELETED, "count": 1, "ids": ["m1"]})
    cli._cmd_forget("chai", confirm=True)
    assert "Moved 1 memory to trash." in capsys.readouterr().out


def test_forget_reports_memories_already_gone(cli, monkeypatch, capsys):
    _api(monkeypatch, cli, PREVIEW, {**DELETED, "count": 1, "ids": ["m1"], "notFound": ["m2"]})
    cli._cmd_forget("chai", confirm=True)
    assert "1 listed memory was already gone." in capsys.readouterr().out


@pytest.mark.parametrize("code, words", [
    ("too_many_matches", "more specific query"),
    ("search_unavailable", "nothing was deleted"),
    ("invalid_token", "expired"),
    ("token_used", "already used"),
])
def test_forget_refusals_print_a_plain_line(cli, monkeypatch, capsys, code, words):
    _api(monkeypatch, cli, _CodedError(code))
    with pytest.raises(SystemExit) as exc:
        cli.maximem_vity_command(SimpleNamespace(vity_subcommand="forget", query="chai", yes=False))
    err = capsys.readouterr().err
    assert exc.value.code == 1 and words in err and "refused (" not in err


def test_restore_by_deletion_id(cli, monkeypatch, capsys):
    calls = _api(monkeypatch, cli, {"restored": ["m1", "m2"], "notRestorable": []})
    cli._cmd_restore("del-1", None)
    assert calls == [("/v1/memory/restore", {"deletionId": "del-1"}, False)]
    assert "Restored 2 memories." in capsys.readouterr().out


def test_restore_by_memory_ids_reports_what_could_not_come_back(cli, monkeypatch, capsys):
    calls = _api(monkeypatch, cli, {"restored": ["m1"], "notRestorable": [
        {"id": "m2", "reason": "past_window"}, {"id": "m3", "reason": "not_deleted"},
        {"id": "m4", "reason": "not_found"}]})
    cli._cmd_restore(None, ["m1", "m2", "m3", "m4"])
    assert calls == [("/v1/memory/restore", {"memoryIds": ["m1", "m2", "m3", "m4"]}, False)]
    out = capsys.readouterr().out
    assert "Restored 1 memory." in out and "Could not restore 3:" in out
    assert "m2: past the 30-day restore window" in out
    assert "m3: not in the trash" in out and "m4: not found" in out


def test_restore_parser_takes_a_deletion_id_or_memory_ids(cli):
    import argparse
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    a = parser.parse_args(["restore", "del-1"])
    assert a.vity_subcommand == "restore" and a.deletion_id == "del-1" and a.ids is None
    b = parser.parse_args(["restore", "--ids", "m1", "m2"])
    assert b.deletion_id is None and b.ids == ["m1", "m2"]


@pytest.mark.parametrize("deletion_id, ids", [(None, None), ("del-1", ["m1"])])
def test_restore_needs_exactly_one_target(cli, monkeypatch, capsys, deletion_id, ids):
    calls = _api(monkeypatch, cli)
    with pytest.raises(SystemExit):
        cli.maximem_vity_command(SimpleNamespace(vity_subcommand="restore", deletion_id=deletion_id, ids=ids))
    assert calls == []
    assert "deletion id" in capsys.readouterr().err


# -- the agent (provider) never deletes --------------------------------------------------------

def _agent(vity_module, monkeypatch, *responses):
    client = MagicMock()
    monkeypatch.setitem(sys.modules, "maximem_vity", SimpleNamespace(VityClient=lambda **_: client))
    calls, queue = [], list(responses)

    def fake(c, path, body, agent=False):
        calls.append((path, body, agent))
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(vity_module.support, "api_post", fake)
    provider = vity_module.VityMemoryProvider()
    provider._api_key = "mx_test_key"
    return provider, client, calls


def test_agent_forget_files_a_request_never_a_delete(vity_module, monkeypatch):
    provider, client, calls = _agent(vity_module, monkeypatch, {
        "requestId": "r1", "count": 2, "status": "pending", "expiresAt": "2026-11-25T00:00:00+00:00",
        "approveUrl": "https://app.maximem.ai/memories?tab=pending"})

    out = json.loads(provider.handle_tool_call("vity_forget", {"query": "chai", "dry_run": False}))

    assert calls == [("/v1/memory/forget", {"query": "chai", "mode": "request"}, True)]
    client.forget.assert_not_called()
    assert "I asked to delete 2 memories" in out["result"]
    assert "https://app.maximem.ai/memories?tab=pending" in out["result"]
    assert "Nothing is deleted until the user approves" in out["result"]


def test_agent_forget_dry_run_previews_with_the_agent_header(vity_module, monkeypatch):
    provider, client, calls = _agent(vity_module, monkeypatch, PREVIEW)

    out = json.loads(provider.handle_tool_call("vity_forget", {"query": "chai"}))

    assert calls == [("/v1/memory/forget", {"query": "chai", "dryRun": True}, True)]
    assert "2 memories match" in out["result"]
    assert out["memories"] == [{"id": "m1", "preview": "User drinks masala chai"},
                               {"id": "m2", "preview": "User drinks chai at 4pm"}]


def test_agent_forget_with_no_matches(vity_module, monkeypatch):
    provider, _, _ = _agent(vity_module, monkeypatch, {"count": 0, "status": "no_matches"})
    out = json.loads(provider.handle_tool_call("vity_forget", {"query": "chai", "dry_run": False}))
    assert "No matching memories" in out["result"]


def test_agent_forget_too_many_matches_is_a_plain_error(vity_module, monkeypatch):
    provider, _, _ = _agent(vity_module, monkeypatch, _CodedError("too_many_matches"))
    out = json.loads(provider.handle_tool_call("vity_forget", {"query": "everything", "dry_run": False}))
    assert "more specific query" in out["error"]


def test_tool_description_says_the_user_approves(vity_module):
    description = vity_module.VITY_FORGET_SCHEMA["description"]
    assert "never deletes" in description and "approves" in description


def test_mirrored_remove_files_a_request_for_exactly_the_verified_memories(vity_module, monkeypatch):
    preview = {**PREVIEW, "count": 1, "ids": ["m1"], "memories": PREVIEW["memories"][:1]}
    provider, client, calls = _agent(vity_module, monkeypatch, preview, {"requestId": "r1", "count": 1})
    client.search.return_value = [{"id": "m1", "content": "User drinks masala chai before standup."}]

    provider.on_memory_write("remove", "user", "", metadata={"old_text": "I drink masala chai before standup."})
    provider._write_queue.join()

    q = "I drink masala chai before standup."
    assert calls == [("/v1/memory/forget", {"query": q, "dryRun": True}, True),
                     ("/v1/memory/forget", {"query": q, "token": "tok-1", "mode": "request"}, True)]
    client.forget.assert_not_called()


def test_mirrored_remove_skips_when_the_preview_has_other_memories(vity_module, monkeypatch):
    provider, client, calls = _agent(vity_module, monkeypatch, PREVIEW)
    client.search.return_value = [{"id": "m1", "content": "User drinks masala chai before standup."},
                                  {"id": "m2", "content": "User drinks coffee after lunch."}]

    provider.on_memory_write("remove", "user", "", metadata={"old_text": "I drink masala chai before standup."})
    provider._write_queue.join()

    assert len(calls) == 1   # the dry run only
