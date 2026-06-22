"""``hermes vity`` CLI subcommands — terminal memory management.

This is the Hermes analog of ``openclaw maximem ...`` from Maximem's OpenClaw
plugin. It is auto-wired by the host's ``discover_plugin_cli_commands()`` when
``memory.provider`` is set to ``vity``:

  - ``register_cli(subparser)`` builds the argparse tree
  - ``vity_command(args)`` is the dispatch handler

Commands::

    hermes vity status                     # config + connection check
    hermes vity search "favorite color"    # semantic search
    hermes vity search "deadlines" --limit 20 --json
    hermes vity store "I prefer dark mode" --type preference
    hermes vity forget "old project" --yes # delete (omit --yes for dry-run)
"""

from __future__ import annotations

import json as _json
import sys


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


def _make_client():
    """Build a VityClient from the active config, or exit with a message."""
    try:
        from maximem_vity import VityClient
    except ImportError:
        print("maximem-vity-sdk not installed. Run: pip install maximem-vity-sdk")
        sys.exit(1)

    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        print("MAXIMEM_API_KEY not set. Add it to ~/.hermes/.env or run:")
        print("  hermes memory setup vity")
        sys.exit(1)
    endpoint = cfg.get("endpoint", "")
    if endpoint:
        return VityClient(api_key=api_key, endpoint=endpoint)
    return VityClient(api_key=api_key)


def _cmd_status() -> None:
    cfg = _load_config()
    has_key = bool(cfg.get("api_key"))
    print("Vity (Maximem AI) memory")
    print(f"  API key:        {'set ✓' if has_key else 'NOT set ✗'}")
    print(f"  endpoint:       {cfg.get('endpoint') or 'default (prod)'}")
    print(f"  auto_recall:    {cfg.get('auto_recall')}")
    print(f"  auto_capture:   {cfg.get('auto_capture')}")
    print(f"  max_recall_tokens: {cfg.get('max_recall_tokens')}")
    print(f"  min_prompt_length: {cfg.get('min_prompt_length')}")
    if not has_key:
        return
    # Live connection check.
    try:
        client = _make_client()
        client.search(query="connection check", top_k=1)
        print("  connection:     ok ✓")
        client.close()
    except Exception as e:
        print(f"  connection:     failed ✗ ({e})")


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


def _cmd_forget(query: str, confirm: bool) -> None:
    client = _make_client()
    try:
        result = client.forget(query=query, dry_run=not confirm)
    finally:
        client.close()
    count = result.get("count", 0)
    if not confirm:
        print(f"Would delete {count} memories. Re-run with --yes to confirm.")
    else:
        print(f"Deleted {count} memories.")


def vity_command(args) -> None:
    """Route ``hermes vity <subcommand>``."""
    sub = getattr(args, "vity_subcommand", None)
    if sub == "status" or sub is None:
        _cmd_status()
    elif sub == "search":
        _cmd_search(args.query, args.limit, args.json)
    elif sub == "store":
        _cmd_store(args.content, args.type)
    elif sub == "forget":
        _cmd_forget(args.query, args.yes)
    else:
        print(f"Unknown vity subcommand: {sub}")


def register_cli(subparser) -> None:
    """Build the ``hermes vity`` argparse subcommand tree."""
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

    forget_p = subs.add_parser("forget", help="Delete memories matching a query")
    forget_p.add_argument("query", help="What to delete")
    forget_p.add_argument("--yes", action="store_true", help="Confirm deletion (omit for dry-run)")
