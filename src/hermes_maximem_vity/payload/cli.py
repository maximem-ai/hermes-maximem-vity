"""``hermes maximem_vity`` CLI subcommands: terminal memory management.

Auto-wired by the host's ``discover_plugin_cli_commands()`` when
``memory.provider`` is set to ``maximem_vity``:

  - ``register_cli(subparser)`` builds the argparse tree
  - ``maximem_vity_command(args)`` is the dispatch handler

Commands::

    hermes maximem_vity status                     # config + connection check
    hermes maximem_vity search "favorite color"    # semantic search
    hermes maximem_vity search "deadlines" --limit 20 --json
    hermes maximem_vity store "I prefer dark mode" --type preference
    hermes maximem_vity forget "old project"       # preview the matches
    hermes maximem_vity forget "old project" --yes # move them to trash (30-day restore)
    hermes maximem_vity restore <deletion id>      # undo a forget
    hermes maximem_vity restore --ids <memory id> ...
"""

from __future__ import annotations

import importlib.util
import json as _json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load_support():
    """The sibling ``vity_support`` module, loaded by path (no parent package here)."""
    spec = importlib.util.spec_from_file_location(
        "maximem_vity_cli_support", Path(__file__).with_name("vity_support.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


support = _load_support()


def _load_config() -> dict:
    """Self-contained config loader for the CLI.

    The CLI module is imported in a lightweight path where the plugin's parent
    package (``_hermes_user_memory.vity``) is NOT registered, so relative
    imports (``from . import ...``) fail. This reads env + ``vity.json``
    directly, mirroring the provider's loader, with no package dependency.
    """
    import os

    cfg = {
        "api_key": (
            os.environ.get("MAXIMEM_API_KEY")
            or os.environ.get("VITY_API_KEY")
            or ""
        ),
        "endpoint": (
            os.environ.get("MAXIMEM_ENDPOINT")
            or os.environ.get("MAXIMEM_API_URL")
            or ""
        ),
        "auto_recall": True,
        "auto_capture": True,
        "max_recall_tokens": 1000,
        "min_prompt_length": 5,
        "recall_timeout": 6.0,
    }
    try:
        from hermes_constants import get_hermes_home
        path = get_hermes_home() / "vity.json"
        if path.exists():
            data = _json.loads(path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if v not in (None, ""):
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def _sdk_available() -> bool:
    """True if the Maximem SDK is importable by THIS interpreter (Hermes')."""
    try:
        import maximem_vity  # noqa: F401
        return True
    except Exception:
        return False


def _make_client():
    """Build a VityClient from the active config, or exit with a message."""
    try:
        from maximem_vity import VityClient
    except ImportError:
        print("maximem-vity-sdk not installed in Hermes' environment.")
        print("Fix: hermes-maximem-vity install")
        sys.exit(1)

    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        print("MAXIMEM_API_KEY not set. Add it to ~/.hermes/.env or run:")
        print("  hermes-maximem-vity install")
        sys.exit(1)
    endpoint = cfg.get("endpoint", "")
    if endpoint:
        return VityClient(api_key=api_key, endpoint=endpoint)
    return VityClient(api_key=api_key)


def _row(label: str, value: str) -> str:
    """One aligned ``label: value`` status row (longest label is 17 chars)."""
    return f"  {label + ':':<20}{value}"


def _cmd_status() -> None:
    cfg = _load_config()
    has_key = bool(cfg.get("api_key"))
    sdk_ok = _sdk_available()

    print("Vity (Maximem AI) memory")
    print(_row("API key", "set ✓" if has_key else "not set ✗"))
    print(_row("SDK", "installed ✓" if sdk_ok else "not installed ✗"))
    print(_row("endpoint", cfg.get("endpoint") or "default (prod)"))
    print(_row("auto_recall", str(cfg.get("auto_recall"))))
    print(_row("auto_capture", str(cfg.get("auto_capture"))))
    print(_row("max_recall_tokens", str(cfg.get("max_recall_tokens"))))
    print(_row("min_prompt_length", str(cfg.get("min_prompt_length"))))
    print(_row("recall_timeout", str(cfg.get("recall_timeout"))))

    # Surface the two blockers cleanly instead of failing mid-connection.
    if not sdk_ok:
        print("\n  ⚠ SDK missing. Run:  hermes-maximem-vity install")
        return
    if not has_key:
        print("\n  ⚠ API key missing. Run:  hermes-maximem-vity install")
        return

    # Live connection check.
    try:
        client = _make_client()
        client.search(query="connection check", top_k=1)
        print(_row("connection", "ok ✓"))
        # Prefer a real memory count when the router exposes /stats; fall back
        # silently to the connection check above until that route ships (404).
        try:
            stats = client.stats() or {}
            total = stats.get("total", stats.get("count"))
            if total is not None:
                print(_row("memories", str(total)))
        except Exception:
            pass
        client.close()
    except Exception as e:
        print(_row("connection", f"failed ✗ ({e})"))


def _is_placeholder(result: dict) -> bool:
    """True for the backend's synthesized non-memory results.

    The API sometimes returns a profile-summary blob (``type: "context"``) or an
    "empty profile" sentinel (``type: null`` with a fixed message) instead of
    discrete stored memories, e.g. for short/low-signal queries. These are not
    real hits, so the human-readable ``search`` view treats them as "no results".
    Real memories can also carry ``type: null``, so we match the sentinel by its
    content, not by type alone.
    """
    if result.get("type") == "context":
        return True
    content = (result.get("content") or "").lower()
    return "memory profile is empty" in content


def _cmd_search(query: str, limit: int, as_json: bool) -> None:
    client = _make_client()
    try:
        results = client.search(query=query, top_k=limit)
    finally:
        client.close()
    if as_json:
        print(_json.dumps(results, indent=2))
        return
    if not results:
        print("No memories found.")
        return
    # Dedupe near-identical hits the backend sometimes returns for one memory.
    seen = set()
    shown = 0
    for r in results:
        # Skip the backend's synthesized placeholders (profile-summary blob /
        # "empty profile" sentinel), not discrete stored memories.
        # `--json` still shows everything for power users.
        if _is_placeholder(r):
            continue
        content = (r.get("content") or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        shown += 1
        score = r.get("score", 0.0)
        mtype = r.get("type", "fact")
        print(f"  [{score:.2f}] ({mtype}) {content}")
    if shown == 0:
        print("No memories found.")


def _cmd_store(content: str, memory_type: str) -> None:
    client = _make_client()
    try:
        result = client.store(content=content, memory_type=memory_type)
    finally:
        client.close()
    print(f"Stored ✓ (id={result.get('id', '')})")


def _noun(count: int) -> str:
    return "memory" if count == 1 else "memories"


def _preview_path():
    """Where a forget preview's token waits for ``--yes`` (None outside Hermes)."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "vity_forget_preview.json"
    except Exception:
        return None


def _take_saved_token(query: str):
    """The token a preview of ``query`` saved, if still valid. Removed either way: one use."""
    path = _preview_path()
    if path is None or not path.exists():
        return None
    try:
        saved = _json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        expires = datetime.fromisoformat(str(saved["tokenExpiresAt"]).replace("Z", "+00:00"))
        if saved.get("query") == query and expires > datetime.now(timezone.utc):
            return saved.get("token")
    except Exception:
        pass
    return None


def _save_token(query: str, preview: dict) -> None:
    path = _preview_path()
    if path is None or not preview.get("token"):
        return
    try:
        path.write_text(_json.dumps({"query": query, "token": preview["token"],
                                     "tokenExpiresAt": preview.get("tokenExpiresAt")}), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass


def _cmd_forget(query: str, confirm: bool) -> None:
    """Preview, then (``--yes``) move exactly the previewed memories to the trash."""
    forget = "/v1/memory/forget"
    client = _make_client()
    try:
        token = _take_saved_token(query) if confirm else None
        if token is None:
            preview = support.api_post(client, forget, {"query": query, "dryRun": True}) or {}
            count = preview.get("count", 0)
            if count == 0:
                print("No matching memories to forget.")
                return
            for m in preview.get("memories") or []:
                print(f"  {m.get('id', '')}  {m.get('preview', '')}")
            if not confirm:
                _save_token(query, preview)
                print(f"Would move {count} {_noun(count)} to trash. "
                      "Re-run with --yes within 15 minutes to confirm.")
                return
            token = preview.get("token")
            if not token:
                print("The server did not confirm this preview. Nothing was deleted; try again.")
                return
        result = support.api_post(client, forget, {"query": query, "token": token}) or {}
    finally:
        client.close()
    count = result.get("count", 0)
    print(f"Moved {count} {_noun(count)} to trash. Restore within 30 days with "
          f"`hermes maximem_vity restore {result.get('deletionId', '')}` or at app.maximem.ai.")
    gone = len(result.get("notFound") or [])
    if gone:
        print(f"{gone} listed {_noun(gone)} {'was' if gone == 1 else 'were'} already gone.")


_RESTORE_REASONS = {
    "past_window": "past the 30-day restore window",
    "not_deleted": "not in the trash",
    "not_found": "not found",
}


def _cmd_restore(deletion_id, ids) -> None:
    body = {"memoryIds": list(ids)} if ids else {"deletionId": deletion_id}
    client = _make_client()
    try:
        result = support.api_post(client, "/v1/memory/restore", body) or {}
    finally:
        client.close()
    restored = len(result.get("restored") or [])
    print(f"Restored {restored} {_noun(restored)}.")
    failed = result.get("notRestorable") or []
    if failed:
        print(f"Could not restore {len(failed)}:")
        for f in failed:
            reason = f.get("reason", "")
            print(f"  {f.get('id', '')}: {_RESTORE_REASONS.get(reason, reason)}")


def maximem_vity_command(args) -> None:
    """Route ``hermes maximem_vity <subcommand>``.

    API errors (rejected key, outage, invalid input) print one readable line
    and exit 1, never a Python traceback.
    """
    sub = getattr(args, "vity_subcommand", None)
    try:
        if sub == "status" or sub is None:
            _cmd_status()
        elif sub == "search":
            _cmd_search(args.query, args.limit, args.json)
        elif sub == "store":
            _cmd_store(args.content, args.type)
        elif sub == "forget":
            _cmd_forget(args.query, args.yes)
        elif sub == "restore":
            if bool(args.deletion_id) == bool(args.ids):
                print("Give a deletion id (printed by forget) or --ids with memory ids.", file=sys.stderr)
                sys.exit(1)
            _cmd_restore(args.deletion_id, args.ids)
        else:
            print(f"Unknown vity subcommand: {sub}")
    except Exception as e:
        plain = support.forget_error(e)
        if plain:
            print(f"✗ {plain}", file=sys.stderr)
            sys.exit(1)
        hint = ""
        if any(c.__name__ == "VityAuthError" for c in type(e).__mro__):
            hint = "\n  Fix the key: hermes-maximem-vity install --login (or --api-key mx_...)"
        print(f"✗ {e}{hint}", file=sys.stderr)
        sys.exit(1)


def register_cli(subparser) -> None:
    """Build the ``hermes maximem_vity`` argparse subcommand tree."""
    subs = subparser.add_subparsers(dest="vity_subcommand")

    subs.add_parser("status", help="Show Vity config and connection status")

    search_p = subs.add_parser("search", help="Semantic search of stored memories")
    search_p.add_argument("query", help="Natural-language search query")
    search_p.add_argument("--limit", type=int, default=10, help="Max results (default 10)")
    search_p.add_argument("--json", action="store_true", help="Emit raw JSON")

    store_p = subs.add_parser("store", help="Store an explicit memory fact")
    store_p.add_argument("content", help="The fact or preference to remember")
    store_p.add_argument(
        "--type", default="fact",
        choices=["fact", "preference", "emotion", "episode", "knowledge", "profile"],
        help="Memory type (default: fact)",
    )

    forget_p = subs.add_parser(
        "forget", help="Move memories matching a query to trash (restorable for 30 days)")
    forget_p.add_argument("query", help="What to forget")
    forget_p.add_argument("--yes", action="store_true",
                          help="Move the previewed memories to trash (omit to preview)")

    restore_p = subs.add_parser("restore", help="Restore forgotten memories from the trash (within 30 days)")
    restore_p.add_argument("deletion_id", nargs="?", help="Deletion id printed by forget")
    restore_p.add_argument("--ids", nargs="+", metavar="MEMORY_ID", help="Restore these memory ids instead")
