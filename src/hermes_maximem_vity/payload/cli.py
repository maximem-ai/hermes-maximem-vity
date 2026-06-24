"""``hermes vity`` CLI subcommands — terminal memory management.

Auto-wired by the host's ``discover_plugin_cli_commands()`` when
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
        print("  hermes memory setup vity")
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

    # Surface the two blockers cleanly instead of failing mid-connection.
    if not sdk_ok:
        print("\n  ⚠ SDK missing — run:  hermes-maximem-vity install")
        return
    if not has_key:
        print("\n  ⚠ API key missing — run:  hermes-maximem-vity install")
        return

    # Live connection check.
    try:
        client = _make_client()
        client.search(query="connection check", top_k=1)
        print(_row("connection", "ok ✓"))
        client.close()
    except Exception as e:
        print(_row("connection", f"failed ✗ ({e})"))


def _is_placeholder(result: dict) -> bool:
    """True for the backend's synthesized non-memory results.

    The API sometimes returns a profile-summary blob (``type: "context"``) or an
    "empty profile" sentinel (``type: null`` with a fixed message) instead of
    discrete stored memories — e.g. for short/low-signal queries. These are not
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
        # "empty profile" sentinel) — not discrete stored memories.
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


def _cmd_forget(query: str, confirm: bool) -> None:
    client = _make_client()
    try:
        result = client.forget(query=query, dry_run=not confirm)
    finally:
        client.close()
    count = result.get("count", 0)
    noun = "memory" if count == 1 else "memories"
    if not confirm:
        if count == 0:
            print("Nothing matches — no memories to delete.")
        else:
            print(f"Would delete {count} {noun}. Re-run with --yes to confirm.")
    else:
        print(f"Deleted {count} {noun}.")


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
