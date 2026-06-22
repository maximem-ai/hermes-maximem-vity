"""Vity (Maximem AI) memory plugin — MemoryProvider for Hermes Agent.

Cross-session semantic memory with profile-based recall, a memory graph
(facts, preferences, emotions, episodes, knowledge, profile), and low-latency
context injection via the Maximem REST API (the ``maximem-vity-sdk`` client).

This is a STANDALONE plugin — install it into ``~/.hermes/plugins/vity/`` and
activate it with ``hermes memory setup vity``. It is not bundled with the core
hermes-agent repo (see that repo's CONTRIBUTING.md, "Memory Providers: Ship as
a Standalone Plugin").

Design mirrors Maximem's official OpenClaw plugin (@maximem/memory-plugin):
  before_agent_start -> recall   ==> initialize() warm-up + prefetch()
  agent_end          -> capture  ==> sync_turn()

Config (env var, recommended):
  MAXIMEM_API_KEY  — Maximem API key (required, starts with mx_)

Config (non-secret tunables, optional) — $HERMES_HOME/vity.json:
  auto_recall        (bool,  default true)   inject memories before each turn
  auto_capture       (bool,  default true)   capture conversation after each turn
  max_recall_tokens  (int,   default 1000)   token budget for recalled context
  min_prompt_length  (int,   default 5)      skip recall for trivially short prompts
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Defaults mirror the OpenClaw plugin's config surface.
_DEFAULT_MAX_RECALL_TOKENS = 1000
_DEFAULT_MIN_PROMPT_LENGTH = 5


# ---------------------------------------------------------------------------
# Tool Schemas (what the agent sees and can call)
# ---------------------------------------------------------------------------

VITY_RECALL_SCHEMA = {
    "name": "vity_recall",
    "description": (
        "Search Vity's semantic memory for relevant context about the user. "
        "Returns ranked memories from the user's memory graph. "
        "Use at conversation start or when you need to recall user preferences, "
        "past decisions, project context, or personal facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for in memory."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

VITY_PROFILE_SCHEMA = {
    "name": "vity_profile",
    "description": (
        "Retrieve the user's full memory profile — all stored facts, preferences, "
        "emotions, episodes, knowledge, and profile data. Use at conversation start "
        "for a complete context snapshot."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

VITY_STORE_SCHEMA = {
    "name": "vity_store",
    "description": (
        "Store a new memory fact about the user in Vity's memory graph. "
        "Use for explicit user preferences, corrections, important decisions, "
        "or facts the user wants remembered across sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact or preference to remember."},
            "memory_type": {
                "type": "string",
                "description": "Memory type: fact, preference, emotion, episode, knowledge, or profile.",
                "enum": ["fact", "preference", "emotion", "episode", "knowledge", "profile"],
            },
        },
        "required": ["content"],
    },
}

VITY_FORGET_SCHEMA = {
    "name": "vity_forget",
    "description": (
        "Delete memories matching a query from Vity's memory graph. "
        "Use when the user explicitly asks to forget something. "
        "Always use dry_run=true first to preview what would be deleted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to delete from memory."},
            "dry_run": {
                "type": "boolean",
                "description": "If true (default), preview deletions without deleting. Pass false to confirm.",
            },
        },
        "required": [],
    },
}

ALL_TOOL_SCHEMAS = [
    VITY_RECALL_SCHEMA,
    VITY_PROFILE_SCHEMA,
    VITY_STORE_SCHEMA,
    VITY_FORGET_SCHEMA,
]


# ---------------------------------------------------------------------------
# Helper: Load config
# ---------------------------------------------------------------------------

def _as_bool(value: Any, default: bool) -> bool:
    """Coerce a JSON/env value to bool, tolerating strings like 'false'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    if value is None:
        return default
    return bool(value)


def _load_config() -> dict:
    """Load Vity config from env vars + $HERMES_HOME/vity.json overrides.

    The API key is secret (env var only). The non-secret tunables mirror the
    OpenClaw plugin's config block and may live in vity.json.
    """
    from hermes_constants import get_hermes_home

    config: Dict[str, Any] = {
        # MAXIMEM_API_KEY is the canonical env var (matches the official npm plugin)
        "api_key": (
            os.environ.get("MAXIMEM_API_KEY")
            or os.environ.get("VITY_API_KEY")  # backward compat
            or ""
        ),
        # Optional endpoint override — point at a self-hosted / local Maximem
        # backend (e.g. http://localhost:8083). Empty = SDK default (prod).
        "endpoint": (
            os.environ.get("MAXIMEM_ENDPOINT")
            or os.environ.get("MAXIMEM_API_URL")
            or ""
        ),
        "auto_recall": True,
        "auto_capture": True,
        "max_recall_tokens": _DEFAULT_MAX_RECALL_TOKENS,
        "min_prompt_length": _DEFAULT_MIN_PROMPT_LENGTH,
    }

    config_path = get_hermes_home() / "vity.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            for key, value in file_cfg.items():
                if value not in (None, ""):
                    config[key] = value
        except Exception:
            pass

    # Normalise types for tunables that may arrive as strings from JSON/env.
    config["auto_recall"] = _as_bool(config.get("auto_recall"), True)
    config["auto_capture"] = _as_bool(config.get("auto_capture"), True)
    try:
        config["max_recall_tokens"] = int(config.get("max_recall_tokens") or _DEFAULT_MAX_RECALL_TOKENS)
    except (TypeError, ValueError):
        config["max_recall_tokens"] = _DEFAULT_MAX_RECALL_TOKENS
    try:
        config["min_prompt_length"] = int(config.get("min_prompt_length") or _DEFAULT_MIN_PROMPT_LENGTH)
    except (TypeError, ValueError):
        config["min_prompt_length"] = _DEFAULT_MIN_PROMPT_LENGTH

    return config


# ---------------------------------------------------------------------------
# Main MemoryProvider Implementation
# ---------------------------------------------------------------------------

class VityMemoryProvider(MemoryProvider):
    """Maximem Vity memory with semantic memory graph and profile-based recall."""

    def __init__(self):
        self._client = None
        self._client_lock = threading.Lock()
        self._api_key = ""
        self._endpoint = ""  # optional self-hosted/local Maximem endpoint
        # Tunables (mirror OpenClaw plugin config). Resolved in initialize().
        self._auto_recall = True
        self._auto_capture = True
        self._max_recall_tokens = _DEFAULT_MAX_RECALL_TOKENS
        self._min_prompt_length = _DEFAULT_MIN_PROMPT_LENGTH
        # Prefetch threading (non-blocking pattern — REQUIRED by Hermes contract)
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread = None
        self._sync_thread = None
        # Track whether we've done the initial warm-up recall
        self._cold_start = True
        # Timing: measure Vity API time vs Hermes LLM time
        self._vity_prefetch_ms: float = 0.0  # how long prefetch() waited for Vity
        self._vity_tool_ms: float = 0.0      # accumulated time inside handle_tool_call() this turn
        self._prefetch_done_at: float = 0.0  # perf_counter timestamp when prefetch returned
        self._vity_retrieved = False         # did this turn actually pull memory?

    # -- Identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "vity"

    # -- Availability (no network calls!) ------------------------------------

    def is_available(self) -> bool:
        """Check config only — NO network calls allowed here."""
        cfg = _load_config()
        return bool(cfg.get("api_key"))

    # -- Config schema (for `hermes memory setup`) ---------------------------

    def get_config_schema(self):
        return [
            {
                "key": "api_key",
                "description": "Maximem API key (starts with mx_)",
                "secret": True,
                "required": True,
                "env_var": "MAXIMEM_API_KEY",
                "url": "https://app.maximem.ai/api-keys",
            },
        ]

    def save_config(self, values: dict, hermes_home: str) -> None:
        """Write non-secret config to $HERMES_HOME/vity.json."""
        from pathlib import Path
        from utils import atomic_json_write
        config_path = Path(hermes_home) / "vity.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        atomic_json_write(config_path, existing, mode=0o600)

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """Setup-wizard hook (CONTRIBUTING.md recommends this for standalone plugins).

        Called by ``hermes memory setup`` after env/config collection. We
        delegate to save_config for any non-secret values supplied.
        """
        non_secret = {
            k: v for k, v in (config or {}).items()
            if k in {"auto_recall", "auto_capture", "max_recall_tokens", "min_prompt_length"}
        }
        if non_secret:
            self.save_config(non_secret, hermes_home)

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize Vity client for this session."""
        config = _load_config()
        self._api_key = config.get("api_key", "")
        self._endpoint = config.get("endpoint", "")
        self._auto_recall = config.get("auto_recall", True)
        self._auto_capture = config.get("auto_capture", True)
        self._max_recall_tokens = config.get("max_recall_tokens", _DEFAULT_MAX_RECALL_TOKENS)
        self._min_prompt_length = config.get("min_prompt_length", _DEFAULT_MIN_PROMPT_LENGTH)
        self._session_id = session_id
        logger.debug(
            "Vity initialized: session=%s auto_recall=%s auto_capture=%s",
            session_id, self._auto_recall, self._auto_capture,
        )

        # Mirror OpenClaw's before_agent_start: immediately warm the recall cache
        # with a broad profile query so the first user message has full context.
        self._cold_start = True
        if self._auto_recall:
            self.queue_prefetch(
                "user profile background context history preferences facts",
                session_id=session_id,
            )

    def _get_client(self):
        """Thread-safe lazy initialization of the Vity SDK client."""
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from maximem_vity import VityClient
                if self._endpoint:
                    self._client = VityClient(api_key=self._api_key, endpoint=self._endpoint)
                else:
                    self._client = VityClient(api_key=self._api_key)
                return self._client
            except ImportError:
                raise RuntimeError(
                    "maximem-vity-sdk not installed. "
                    "Run: pip install maximem-vity-sdk"
                )

    # -- System Prompt Block -------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# Vity Memory (Maximem AI)\n"
            "Active. Memories are scoped to the configured Maximem API key.\n\n"
            "## Rules\n"
            "1. If the user asks about something personal (facts, preferences, history, context) "
            "and it is not already in your injected memory context, call `vity_recall` with a "
            "targeted query before saying you don't know. Never claim ignorance without trying.\n"
            "2. Use `vity_store` to save any new facts the user shares about themselves.\n"
            "3. Use `vity_forget` only when the user explicitly asks to delete a memory."
        )

    # -- Prefetch (background context injection) -----------------------------

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Pre-warm Vity recall in background after each turn."""
        if not self._auto_recall:
            return

        def _run():
            try:
                client = self._get_client()
                context = client.recall(
                    current_prompt=query,
                    max_tokens=self._max_recall_tokens,
                    strategy="hybrid",
                )
                if context:
                    with self._prefetch_lock:
                        self._prefetch_result = context
            except Exception as e:
                logger.debug("Vity prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_run, daemon=True, name="vity-prefetch")
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return pre-warmed Vity context.

        On cold start (first turn of a session), waits for the warm-up recall
        triggered in initialize(). Mirrors OpenClaw's before_agent_start hook —
        memory is always injected before turn 1.
        """
        import time as _time
        _t0 = _time.perf_counter()

        if not self._auto_recall:
            return ""

        # Skip recall for trivially short prompts (matches OpenClaw minPromptLength).
        if len(query.strip()) < self._min_prompt_length and not self._cold_start:
            return ""

        wait_secs = 8.0 if self._cold_start else 3.0
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=wait_secs)

        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""

        # Cold-start fallback: blocking recall with the actual query.
        if not result and self._cold_start:
            try:
                client = self._get_client()
                result = client.recall(
                    current_prompt=query,
                    max_tokens=self._max_recall_tokens,
                    strategy="hybrid",
                )
                logger.debug("Vity cold-start blocking recall returned %d chars", len(result or ""))
            except Exception as e:
                logger.debug("Vity cold-start recall failed: %s", e)

        self._cold_start = False

        self._vity_prefetch_ms = (_time.perf_counter() - _t0) * 1000
        self._vity_tool_ms = 0.0
        self._prefetch_done_at = _time.perf_counter()
        self._vity_retrieved = bool(result)

        if not result:
            return ""
        return f"## Vity Memory\n{result}"

    # -- Sync Turn (MUST be non-blocking!) -----------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages=None,
    ) -> None:
        """Capture conversation turn into Vity's memory pipeline (non-blocking)."""
        import time
        now = int(time.time() * 1000)

        # Timing breakdown for the CLI feed — only when Vity actually retrieved.
        if self._prefetch_done_at > 0:
            total_turn_ms = (time.perf_counter() - self._prefetch_done_at) * 1000
            vity_total_ms = self._vity_prefetch_ms + self._vity_tool_ms
            hermes_llm_ms = max(0.0, total_turn_ms - self._vity_tool_ms)
            if self._vity_retrieved:
                _DIM, _RESET = "\033[2m", "\033[0m"
                print(
                    f"{_DIM}⏱ Vity memory · retrieved in {vity_total_ms:.0f}ms · "
                    f"response in {hermes_llm_ms:.0f}ms{_RESET}",
                    flush=True,
                )
            logger.debug(
                "Vity timing: retrieved=%s, vity=%.0f ms, hermes_llm=%.0f ms",
                self._vity_retrieved, vity_total_ms, hermes_llm_ms,
            )
            self._prefetch_done_at = 0.0
            self._vity_tool_ms = 0.0
            self._vity_retrieved = False

        if not self._auto_capture:
            return

        def _sync():
            try:
                client = self._get_client()
                client.capture(
                    messages=[
                        {"role": "user", "content": user_content, "timestamp": now},
                        {"role": "assistant", "content": assistant_content, "timestamp": now + 1},
                    ],
                    agent_id="hermes",
                    session_key=session_id,
                )
            except Exception as e:
                logger.warning("Vity sync failed: %s", e)

        # Wait for previous sync before starting a new one.
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="vity-sync")
        self._sync_thread.start()

    # -- Built-in memory mirroring (OpenClaw /remember parity) ---------------

    def on_memory_write(self, action, target, content, metadata=None):
        """Mirror Hermes' built-in memory writes into Vity.

        Gives parity with the OpenClaw plugin's /remember: when the user (or
        agent) writes to Hermes' built-in memory, the same fact is stored in
        Vity so it participates in semantic recall. Only mirrors 'add'/'replace'.
        """
        if not self._auto_capture or action not in {"add", "replace"} or not content:
            return

        def _mirror():
            try:
                client = self._get_client()
                memory_type = "preference" if target == "user" else "fact"
                client.store(content=content, memory_type=memory_type)
            except Exception as e:
                logger.debug("Vity on_memory_write mirror failed: %s", e)

        threading.Thread(target=_mirror, daemon=True, name="vity-memwrite").start()

    # -- Tool Schemas + Dispatch ---------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return ALL_TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        """Dispatch tool calls from the agent to Vity's API."""
        import time as _t
        _tool_start = _t.perf_counter()

        try:
            client = self._get_client()
        except Exception as e:
            return tool_error(str(e))

        try:
            if tool_name == "vity_profile":
                try:
                    profile = client.get_profile()
                    if not profile:
                        return json.dumps({"result": "No memories stored yet."})
                    self._vity_retrieved = True
                    return json.dumps({"result": profile, "source": "vity"})
                except Exception as e:
                    return tool_error(f"Failed to fetch profile: {e}")

            elif tool_name == "vity_recall":
                query = args.get("query", "")
                if not query:
                    return tool_error("Missing required parameter: query")
                top_k = min(int(args.get("top_k", 10)), 50)
                try:
                    # Use search() for specific-memory retrieval. The recall()
                    # endpoint returns a synthesized profile that reliably MISSES
                    # individual stored items (e.g. "what did I read about X"),
                    # whereas search() does true semantic retrieval over the
                    # stored memories. Dedupe near-identical hits the backend
                    # sometimes returns.
                    results = client.search(query=query, top_k=top_k)
                    seen = set()
                    memories = []
                    for r in results:
                        content = (r.get("content") or "").strip()
                        if content and content not in seen:
                            seen.add(content)
                            memories.append(content)
                    if memories:
                        self._vity_retrieved = True
                        return json.dumps({"result": memories})
                    # Fall back to the recall/profile context if search is empty.
                    context = client.recall(
                        current_prompt=query,
                        max_tokens=top_k * 100,
                        strategy="hybrid",
                    )
                    if not context:
                        return json.dumps({"result": "No relevant memories found."})
                    self._vity_retrieved = True
                    return json.dumps({"result": context})
                except Exception as e:
                    return tool_error(f"Search failed: {e}")

            elif tool_name == "vity_store":
                content = args.get("content", "")
                if not content:
                    return tool_error("Missing required parameter: content")
                memory_type = args.get("memory_type", "fact")
                try:
                    result = client.store(content=content, memory_type=memory_type)
                    return json.dumps({"result": "Memory stored successfully.", "id": result.get("id", "")})
                except Exception as e:
                    return tool_error(f"Failed to store memory: {e}")

            elif tool_name == "vity_forget":
                query = args.get("query", "")
                dry_run = args.get("dry_run", True)
                try:
                    result = client.forget(query=query, dry_run=dry_run)
                    count = result.get("count", 0)
                    if dry_run:
                        return json.dumps({"result": f"Would delete {count} memories. Call again with dry_run=false to confirm."})
                    return json.dumps({"result": f"Deleted {count} memories.", "count": count})
                except Exception as e:
                    return tool_error(f"Forget failed: {e}")

            return tool_error(f"Unknown tool: {tool_name}")

        finally:
            self._vity_tool_ms += (_t.perf_counter() - _tool_start) * 1000

    # -- Shutdown ------------------------------------------------------------

    def shutdown(self) -> None:
        """Flush background threads before process exits."""
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None


# ---------------------------------------------------------------------------
# Plugin Entry Point (required by Hermes discovery system)
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Called by Hermes memory plugin discovery. Registers Vity as a provider."""
    ctx.register_memory_provider(VityMemoryProvider())
