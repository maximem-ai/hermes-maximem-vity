"""Vity (Maximem AI) memory plugin: MemoryProvider for Hermes Agent.

Cross-session semantic memory with profile-based recall, a memory graph
(facts, preferences, emotions, episodes, knowledge, profile), and low-latency
context injection via the Maximem REST API (the ``maximem-vity-sdk`` client).

This is a standalone plugin. It is installed into ``~/.hermes/plugins/maximem_vity/``
and is not bundled with the core hermes-agent repo (see that repo's
CONTRIBUTING.md, "Memory Providers: Ship as a Standalone Plugin").

Lifecycle:
  initialize() + prefetch()  -> recall relevant memories before each turn
  sync_turn()                -> capture the exchange after each turn

Config (env var, recommended):
  MAXIMEM_API_KEY: Maximem API key (required, starts with mx_)

Config (non-secret tunables, optional), in $HERMES_HOME/vity.json:
  auto_recall        (bool,  default true)   inject memories before each turn
  auto_capture       (bool,  default true)   capture conversation after each turn
  max_recall_tokens  (int,   default 1000)   size cap for injected recall context
  min_prompt_length  (int,   default 5)      skip recall for trivially short prompts
  recall_timeout     (float, default 6.0)    max seconds to wait for pre-turn recall
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

try:  # the host's recall indicator; absent on older Hermes builds
    from agent.memory_provider import RecallStatus
except ImportError:  # pragma: no cover
    RecallStatus = None

# Stateless helpers live beside this file. Installed, the plugin is a package
# (relative import); loaded standalone (tests), the payload dir is on sys.path.
try:
    from . import vity_support as support
except ImportError:
    import vity_support as support

logger = logging.getLogger(__name__)

# Best-effort analytics. Installed as a sibling module (maximem_vity.analytics);
# under unit tests the provider is loaded standalone, so both imports fail and we
# degrade to a no-op. Analytics must never break plugin import.
try:
    from . import analytics as _analytics  # installed as the maximem_vity package
except Exception:
    try:
        import analytics as _analytics  # flat load if the dir is on sys.path
    except Exception:
        _analytics = None

# Default tunables.
_DEFAULT_MAX_RECALL_TOKENS = 1000
_DEFAULT_MIN_PROMPT_LENGTH = 5
# Short timeout (seconds) for automatic recall. Auto-recall runs on the critical
# path before every turn, so a slow/unhealthy Maximem endpoint must never block
# the user, so we bound it low and degrade to "no memory this turn". The healthy
# search endpoint responds in ~2s; capture/store keep the SDK's default timeout.
_DEFAULT_RECALL_TIMEOUT = 6.0
# Hard wall-clock cap on the WHOLE pre-turn recall. recall_timeout bounds a
# single HTTP request, but search() can chain a recall() fallback (two requests),
# so a slow backend could stack to ~2x. This caps the total prefetch wait so one
# bad turn degrades to "no memory" fast instead of blocking the user.
_DEFAULT_RECALL_DEADLINE = 3.0
# Minimum relevance for a memory to be AUTO-INJECTED before a turn. Auto-recall
# runs on every message, so a weak match (score ~0.2) injected under a "Vity
# Memory" header reads to the agent as established fact. This floor keeps noise
# out of the prompt. It applies ONLY to auto-recall; the vity_recall tool (an
# explicit agent request) stays unfiltered. 0.3 matches the knowledge endpoint's
# own cosine floor.
_DEFAULT_RECALL_MIN_SCORE = 0.3
# Session-start profile: off-turn, fetch the user's Profile Card and inject it
# ONCE per session, the first turn it's ready, as silent background context.
# Cached across sessions within the TTL so back-to-back sessions reuse it.
_DEFAULT_WARM_PROFILE = True
_PROFILE_TTL_SECONDS = 3600.0
# Shutdown waits up to this long (total) for queued memory writes. A store()
# does classification + embedding server-side and can take several seconds.
_WRITE_DRAIN_TIMEOUT_S = 15.0
# Recall pause: after this many failed recalls in a row (errors or timeouts),
# skip auto-recall for _RECALL_PAUSE_SECONDS so an outage stops costing every
# turn the full deadline. The next recall after the pause is the health check.
_RECALL_FAILURES_TO_PAUSE = 3
_RECALL_PAUSE_SECONDS = 60.0
# Shown when Hermes' SDK predates the Profile Card (a partial upgrade). Recall and
# capture keep working; only the profile needs the newer SDK.
_SDK_TOO_OLD = ("The Profile Card needs maximem-vity-sdk 0.3.1 or newer in Hermes' "
                "environment. Run: hermes-maximem-vity install")

# Label for the host's recall indicator line. No glyph: Hermes defaults to an
# emoji, which we leave out so the line is plain text.
_PROVIDER_LABEL = "Maximem Vity"


# ---------------------------------------------------------------------------
# Tool Schemas (what the agent sees and can call)
# ---------------------------------------------------------------------------

VITY_RECALL_SCHEMA = {
    "name": "vity_recall",
    "description": (
        "Retrieve SPECIFIC stored memories about the user: individual facts, "
        "preferences, past decisions, project context. Fast (about 0.5s). The most "
        "relevant memories are already injected before each turn under 'Vity Memory'; "
        "call this only for something that is not there. (For what the user broadly "
        "KNOWS about a topic, use vity_knowledge instead.)"
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
        "Retrieve the user's Profile Card: name, role and company, location, "
        "education, expertise, skills, interests, active goals and preferences. "
        "Fast. A summary is already injected at session start under 'About the "
        "user'; call this when the user asks to see their profile or it is missing."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

VITY_STORE_SCHEMA = {
    "name": "vity_store",
    "description": (
        "Store a new memory fact about the user in Vity's memory graph. "
        "Use for explicit user preferences, corrections, important decisions, "
        "or facts the user wants remembered across sessions. Call it ONCE per fact: "
        "a successful result means it is saved, so do not repeat the call."
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
        "Use only when the user explicitly asks to forget something. "
        "Always call with dry_run=true first to preview, then confirm with the user."
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
        "required": ["query"],
    },
}

VITY_KNOWLEDGE_SCHEMA = {
    "name": "vity_knowledge",
    "description": (
        "Query the user's consolidated KNOWLEDGE: topic-level summaries of what they "
        "know or have learned, distilled across many memories. Returns items with "
        "topic, category, and a synthesized summary. Use for 'what does the user know "
        "about X' or their expertise on a subject. Takes 1 to 2 seconds. (For a specific "
        "individual fact the user stated, use vity_recall instead.)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Subject to retrieve knowledge about."},
            "limit": {"type": "integer", "description": "Max items (default: 10, max: 100)."},
        },
        "required": ["query"],
    },
}

# Always registered: Hermes builds its tool routing once, before initialize(),
# so a tool added later is never routable. The handler checks whether Meeting
# Memory is enabled for the account and says so if it is not.
VITY_MEETING_RECALL_SCHEMA = {
    "name": "vity_meeting_recall",
    "description": (
        "Answer a question using the user's MEETING history: transcripts, decisions, "
        "action items, attendees. Use for questions about what was discussed or decided "
        "in meetings, who attended, or what is still open. Slower (about 3s: it "
        "synthesizes an answer). If it reports Meeting Memory is not enabled, do not retry."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The meeting question to answer."},
            "meeting_type": {"type": "string", "description": "Optional filter, e.g. '1:1', 'standup'."},
            "status": {"type": "string", "description": "Optional filter, e.g. 'open' for unresolved items."},
        },
        "required": ["question"],
    },
}

VITY_UPDATE_SCHEMA = {
    "name": "vity_update",
    "description": (
        "Edit an existing memory in place by id (keeps the id, unlike forget+store). "
        "Use to correct or refine a specific stored fact."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Id of the memory to edit."},
            "content": {"type": "string", "description": "The corrected text."},
        },
        "required": ["memory_id", "content"],
    },
}

VITY_DELETE_SCHEMA = {
    "name": "vity_delete",
    "description": (
        "Delete ONE memory by its id (precise, unlike vity_forget which matches a query). "
        "Use when you have the exact id of a memory to remove."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Id of the memory to delete."},
        },
        "required": ["memory_id"],
    },
}

# Always-on tools.
ALL_TOOL_SCHEMAS = [
    VITY_RECALL_SCHEMA,
    VITY_PROFILE_SCHEMA,
    VITY_STORE_SCHEMA,
    VITY_FORGET_SCHEMA,
    VITY_KNOWLEDGE_SCHEMA,
    VITY_MEETING_RECALL_SCHEMA,
]

# Edit/delete-by-id endpoints are not yet available on the public API, so
# vity_update/vity_delete are not advertised to the agent (the dispatch handlers
# already exist). Flip to True once the endpoints ship.
_ROUTER_CRUD_LIVE = False


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


def _load_config(hermes_home: str | None = None) -> dict:
    """Load Vity config from env vars + $HERMES_HOME/vity.json overrides.

    The API key is secret (env var only). Non-secret tunables may live in
    vity.json.

    ``hermes_home`` is the host-injected, profile-scoped HERMES_HOME (passed to
    ``initialize()``). When given we resolve ``vity.json`` under it instead of
    the global default, so per-profile installs read their own config. Callers
    without a session (``is_available()``, the CLI) pass nothing and fall back to
    the global resolver.
    """
    from pathlib import Path

    from hermes_constants import get_hermes_home

    config: Dict[str, Any] = {
        # MAXIMEM_API_KEY is the canonical env var.
        "api_key": (
            os.environ.get("MAXIMEM_API_KEY")
            or os.environ.get("VITY_API_KEY")  # backward compat
            or ""
        ),
        # Optional endpoint override for a self-hosted Maximem backend.
        # Empty = SDK default.
        "endpoint": (
            os.environ.get("MAXIMEM_ENDPOINT")
            or os.environ.get("MAXIMEM_API_URL")
            or ""
        ),
        "auto_recall": True,
        "auto_capture": True,
        "max_recall_tokens": _DEFAULT_MAX_RECALL_TOKENS,
        "min_prompt_length": _DEFAULT_MIN_PROMPT_LENGTH,
        "recall_timeout": _DEFAULT_RECALL_TIMEOUT,
        "recall_deadline": _DEFAULT_RECALL_DEADLINE,
        "recall_min_score": _DEFAULT_RECALL_MIN_SCORE,
        "profile_warmup": _DEFAULT_WARM_PROFILE,
    }

    base = Path(hermes_home) if hermes_home else get_hermes_home()
    config_path = base / "vity.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            for key, value in file_cfg.items():
                if value not in (None, ""):
                    config[key] = value
        except Exception as e:  # a broken file must not stop memory; say so instead of ignoring it
            logger.warning("Ignoring %s (invalid JSON, using defaults): %s", config_path, e)

    # Normalise types for tunables that may arrive as strings from JSON/env.
    config["auto_recall"] = _as_bool(config.get("auto_recall"), True)
    config["auto_capture"] = _as_bool(config.get("auto_capture"), True)
    config["profile_warmup"] = _as_bool(config.get("profile_warmup"), _DEFAULT_WARM_PROFILE)
    try:
        config["max_recall_tokens"] = int(config.get("max_recall_tokens") or _DEFAULT_MAX_RECALL_TOKENS)
    except (TypeError, ValueError):
        config["max_recall_tokens"] = _DEFAULT_MAX_RECALL_TOKENS
    try:
        config["min_prompt_length"] = int(config.get("min_prompt_length") or _DEFAULT_MIN_PROMPT_LENGTH)
    except (TypeError, ValueError):
        config["min_prompt_length"] = _DEFAULT_MIN_PROMPT_LENGTH
    try:
        config["recall_timeout"] = float(config.get("recall_timeout") or _DEFAULT_RECALL_TIMEOUT)
    except (TypeError, ValueError):
        config["recall_timeout"] = _DEFAULT_RECALL_TIMEOUT
    try:
        config["recall_deadline"] = float(config.get("recall_deadline") or _DEFAULT_RECALL_DEADLINE)
    except (TypeError, ValueError):
        config["recall_deadline"] = _DEFAULT_RECALL_DEADLINE
    try:
        config["recall_min_score"] = float(
            config.get("recall_min_score")
            if config.get("recall_min_score") is not None
            else _DEFAULT_RECALL_MIN_SCORE
        )
    except (TypeError, ValueError):
        config["recall_min_score"] = _DEFAULT_RECALL_MIN_SCORE

    return config


# Non-secret tunables offered in `hermes memory setup` (saved to vity.json).
_TUNABLE_FIELDS = [
    {"key": "auto_recall", "description": "Inject relevant memories before each turn",
     "type": "boolean", "default": True},
    {"key": "auto_capture", "description": "Save each conversation turn to memory",
     "type": "boolean", "default": True},
    {"key": "profile_warmup", "description": "Inject the user's Profile Card once per session",
     "type": "boolean", "default": _DEFAULT_WARM_PROFILE},
    {"key": "recall_min_score", "description": "Minimum relevance for an auto-injected memory",
     "type": "number", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "default": _DEFAULT_RECALL_MIN_SCORE},
    {"key": "recall_deadline", "description": "Maximum seconds pre-turn recall may take",
     "type": "number", "minimum": 0.5, "maximum": 10.0, "step": 0.5, "default": _DEFAULT_RECALL_DEADLINE},
    {"key": "max_recall_tokens", "description": "Size cap for injected recall context (tokens)",
     "type": "integer", "minimum": 100, "maximum": 8000, "default": _DEFAULT_MAX_RECALL_TOKENS},
]


def _is_placeholder(result: dict) -> bool:
    """True for the backend's synthesized non-memory results.

    ``search()`` sometimes returns a profile-summary blob (``type: "context"``)
    or an "empty profile" sentinel instead of discrete stored memories, e.g.
    for short/low-signal queries. These are not real hits, so we drop them
    before injecting recall context (otherwise a fresh/sparse account injects
    "memory profile is empty" as if it were a memory). Matches the CLI's
    ``hermes maximem_vity search`` filtering.
    """
    if result.get("type") == "context":
        return True
    content = (result.get("content") or "").lower()
    return "memory profile is empty" in content


# ---------------------------------------------------------------------------
# Main MemoryProvider Implementation
# ---------------------------------------------------------------------------

class VityMemoryProvider(MemoryProvider):
    """Maximem Vity memory with semantic memory graph and profile-based recall."""

    def __init__(self):
        self._client = None
        self._recall_client = None  # short-timeout client for auto-recall
        self._client_lock = threading.Lock()
        self._api_key = ""
        self._endpoint = ""  # optional self-hosted/local Maximem endpoint
        # Tunables. Resolved in initialize().
        self._auto_recall = True
        self._auto_capture = True
        self._max_recall_tokens = _DEFAULT_MAX_RECALL_TOKENS
        self._min_prompt_length = _DEFAULT_MIN_PROMPT_LENGTH
        self._recall_timeout = _DEFAULT_RECALL_TIMEOUT
        self._recall_deadline = _DEFAULT_RECALL_DEADLINE
        self._recall_min_score = _DEFAULT_RECALL_MIN_SCORE
        # Skip memory writes in non-primary contexts (cron/flush/subagent) so
        # automated runs don't corrupt the user's memory. Resolved in initialize().
        self._write_enabled = True
        # Background work (non-blocking pattern, REQUIRED by Hermes contract).
        # Recall runs synchronously per-turn in prefetch() against the current
        # message. Every write (turn capture, built-in memory mirroring) goes
        # through ONE queue drained by one writer thread: writes stay in order,
        # and sync_turn() never blocks the host's worker.
        self._prefetch_thread = None
        self._write_queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        self._pending_path: Optional[Path] = None  # undelivered writes, replayed next start
        self._atexit_registered = False
        # Recall health: consecutive failures, the pause they trigger, and a
        # one-time notice when the key is rejected or credits run out.
        self._recall_failures = 0
        self._recall_paused_until = 0.0
        self._service_issue = ""          # "", support.AUTH_ERROR or support.CREDITS_ERROR
        self._service_notice_shown = False
        # What the last prefetch injected, for the host's recall indicator.
        self._last_recall_count: Optional[int] = None
        # First-turn flag, exempts the very first prompt from the min-length skip.
        self._cold_start = True
        # Last SUBSTANTIAL user message, the anchor a short follow-up ("why?",
        # "tell me more") is enriched with before searching. Persists across a
        # chain of follow-ups; only a non-follow-up message overwrites it.
        self._prev_query = ""
        # Session-start profile. The profile text is cached across
        # sessions (TTL); the injected-flag is per session so it shows once.
        self._warm_profile = _DEFAULT_WARM_PROFILE
        self._profile_context = ""
        self._profile_loaded_at = 0.0
        self._profile_injected = False
        self._profile_fields = 0  # Profile Card fields in _profile_context (telemetry)
        # Whether Meeting Memory is enabled for this account: probed off-turn in
        # _warm_recall(), or on first use of the meeting tool. None = unknown.
        self._meetings_enabled: Optional[bool] = None

    # -- Analytics (best-effort, never fatal) --------------------------------

    def _track(self, event: str, props: Dict[str, Any] | None = None) -> None:
        """Send a usage telemetry event. No-op if opted out or no API key."""
        if _analytics is None:
            return
        try:
            _analytics.track(event, api_key=self._api_key, props=props)
        except Exception:  # analytics must never break a turn
            pass

    # -- Identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        # Provider id, also the plugin folder name and the value shown in the
        # Hermes Desktop "Memory Provider" dropdown (rendered as "Maximem Vity").
        return "maximem_vity"

    # -- Availability (no network calls!) ------------------------------------

    def is_available(self) -> bool:
        """Check config and dependencies only, NO network calls allowed here."""
        return not self.unavailable_reason()

    def unavailable_reason(self) -> str:
        """Why the provider can't run, shown by Hermes instead of a silent drop."""
        if not _load_config().get("api_key"):
            return ("MAXIMEM_API_KEY is not set. Run: hermes-maximem-vity install "
                    "--api-key mx_... (get a key at https://app.maximem.ai/api-keys)")
        import importlib.util
        try:
            sdk_present = importlib.util.find_spec("maximem_vity") is not None
        except (ImportError, ValueError):  # already imported without a spec
            sdk_present = "maximem_vity" in sys.modules
        if not sdk_present:
            return ("maximem-vity-sdk is not installed in Hermes' environment. "
                    "Run: hermes-maximem-vity install")
        return ""

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
            *_TUNABLE_FIELDS,
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
            except Exception as e:
                logger.warning("Rewriting %s, which was not valid JSON: %s", config_path, e)
        existing.update(values)
        atomic_json_write(config_path, existing, mode=0o600)

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """Setup-wizard hook (CONTRIBUTING.md recommends this for standalone plugins).

        Called by ``hermes memory setup`` after env/config collection: saves the
        non-secret tunables, then tests the connection so the user knows right
        away whether memory works.
        """
        tunable_keys = {f["key"] for f in _TUNABLE_FIELDS}
        non_secret = {k: v for k, v in (config or {}).items() if k in tunable_keys}
        if non_secret:
            self.save_config(non_secret, hermes_home)
        print(f"  {self._connection_summary(hermes_home)}")

    def get_status_config(self, provider_config: dict) -> dict:
        """What ``hermes memory status`` shows for this provider (live check)."""
        config = _load_config()
        return {
            "endpoint": config.get("endpoint") or "default",
            "api_key": "set" if config.get("api_key") else "missing",
            "connection": self._connection_summary(),
        }

    def _connection_summary(self, hermes_home: Optional[str] = None) -> str:
        """One-line live check: reads the Profile Card with the configured key."""
        config = _load_config(hermes_home)
        if not config.get("api_key"):
            return "✗ Not connected: MAXIMEM_API_KEY is not set"
        try:
            from maximem_vity import VityClient
            kwargs: Dict[str, Any] = {"api_key": config["api_key"], "channel": "hermes", "timeout": 10.0}
            if config.get("endpoint"):
                kwargs["endpoint"] = config["endpoint"]
            with VityClient(**kwargs) as client:
                fields = len(support.profile_rows(client.get_profile_card(), 1))
        except Exception as e:
            return f"✗ Not connected: {support.classify_error(e)[1]}"
        return f"✓ Connected to Maximem · Profile Card: {fields} fields"

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize Vity client for this session."""
        config = _load_config(kwargs.get("hermes_home"))
        self._api_key = config.get("api_key", "")
        self._endpoint = config.get("endpoint", "")
        self._auto_recall = config.get("auto_recall", True)
        self._auto_capture = config.get("auto_capture", True)
        self._max_recall_tokens = config.get("max_recall_tokens", _DEFAULT_MAX_RECALL_TOKENS)
        self._min_prompt_length = config.get("min_prompt_length", _DEFAULT_MIN_PROMPT_LENGTH)
        self._recall_timeout = config.get("recall_timeout", _DEFAULT_RECALL_TIMEOUT)
        self._recall_deadline = config.get("recall_deadline", _DEFAULT_RECALL_DEADLINE)
        self._recall_min_score = config.get("recall_min_score", _DEFAULT_RECALL_MIN_SCORE)
        self._warm_profile = config.get("profile_warmup", _DEFAULT_WARM_PROFILE)

        # Skip writes for non-primary agent contexts. A cron job or a subagent's
        # system-prompt-driven turns would otherwise be captured as if the user
        # said them, corrupting the user's memory profile.
        agent_context = (kwargs.get("agent_context") or "").strip().lower()
        platform = (kwargs.get("platform") or "").strip().lower()
        self._write_enabled = (
            agent_context not in {"cron", "flush", "subagent"} and platform != "cron"
        )

        logger.debug(
            "Vity initialized: session=%s auto_recall=%s auto_capture=%s write_enabled=%s",
            session_id, self._auto_recall, self._auto_capture, self._write_enabled,
        )

        # First turn of the session, exempt from the min-length recall skip.
        self._cold_start = True
        # Re-arm the one-time profile inject for this new session (the cached
        # profile text itself is preserved across sessions).
        self._profile_injected = False

        self._track(
            "provider_initialized",
            {
                "auto_recall": self._auto_recall,
                "auto_capture": self._auto_capture,
                "endpoint_type": "custom" if self._endpoint else "default",
                "write_enabled": self._write_enabled,
                "agent_context": agent_context or "primary",
            },
        )

        # A CLI exit can skip the host's provider shutdown; drain pending writes at
        # interpreter exit too.
        if not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self.shutdown)

        # Writes that could not be delivered last time (outage, crash, exit
        # mid-write) are replayed now, in their original order.
        if self._write_enabled and self._api_key:
            from hermes_constants import get_hermes_home
            home = Path(kwargs.get("hermes_home") or get_hermes_home())
            self._pending_path = home / support.PENDING_WRITES_FILE
            try:
                for job in support.take_pending_writes(self._pending_path):
                    self._enqueue_write(job)
            except Exception as e:
                logger.warning("Vity could not replay pending writes: %s", e)

        # Warm the recall path in the background so the first real turn doesn't
        # pay the cold-start tax (SDK import + client build + DNS + TLS), which
        # is what made the first prefetch take ~12s. Best-effort, off-turn.
        if self._auto_recall and self._api_key:
            self._prefetch_thread = threading.Thread(
                target=self._warm_recall, daemon=True, name="vity-warmup"
            )
            self._prefetch_thread.start()

    def _build_client(self, timeout: float | None = None):
        """Construct a VityClient, optionally with a custom HTTP timeout."""
        from maximem_vity import VityClient
        # Declare the integration channel explicitly. `channel` participates in V4 conversation
        # identity server-side (chat_id = "{channel}:{sessionKey}"), and the SDK no longer defaults
        # it to "hermes", so Hermes must self-identify here rather than rely on the SDK default.
        kwargs: Dict[str, Any] = {"api_key": self._api_key, "channel": "hermes", "agent_id": "hermes"}
        if self._endpoint:
            kwargs["endpoint"] = self._endpoint
        if timeout is not None:
            kwargs["timeout"] = timeout
        return VityClient(**kwargs)

    def _get_client(self):
        """Thread-safe lazy client for capture/store/tool calls (SDK default timeout)."""
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                self._client = self._build_client()
                return self._client
            except ImportError as e:
                raise RuntimeError(
                    "maximem-vity-sdk is not installed in Hermes' environment. "
                    "Run: hermes-maximem-vity install"
                ) from e

    def _get_recall_client(self):
        """Thread-safe lazy client for AUTO-RECALL, with a short HTTP timeout.

        Auto-recall runs before every turn; a slow or unhealthy Maximem endpoint
        must never block the user. This client aborts after ``recall_timeout``
        seconds so prefetch degrades to "no memory this turn" instead of hanging.
        Kept separate from the main client so capture/store keep the SDK default.
        """
        with self._client_lock:
            if self._recall_client is not None:
                return self._recall_client
            try:
                self._recall_client = self._build_client(timeout=self._recall_timeout)
                return self._recall_client
            except ImportError as e:
                raise RuntimeError(
                    "maximem-vity-sdk is not installed in Hermes' environment. "
                    "Run: hermes-maximem-vity install"
                ) from e

    def _warm_recall(self) -> None:
        """One-time background warm-up of the recall client and connection.

        Builds the short-timeout client and fires a tiny throwaway search so the
        SDK import, HTTP client, DNS, and TLS handshake are all paid off-turn.
        The first user turn then hits a warm connection (~2s) instead of a cold
        one (~12s). Fully best-effort: any failure is ignored.
        """
        try:
            self._get_recall_client().search(query="warmup", top_k=1)
        except Exception as e:
            self._note_service_issue(e)
            logger.debug("Vity recall warm-up skipped (non-fatal): %s", e)
        # Probe meeting-memory availability off-turn, so the meeting tool can
        # answer "not enabled" without a network round-trip later.
        try:
            self._meetings_enabled = self._get_client().meetings_enabled()
        except Exception as e:
            logger.debug("Vity meetings probe skipped (non-fatal): %s", e)
        # Off-turn: warm-load the general user profile for a one-time,
        # session-start injection.
        self._load_profile()

    def _load_profile(self) -> None:
        """Warm-load the user's Profile Card, off-turn.

        Best-effort. The card is a fast read of the user's resolved profile (no
        LLM). Cached across sessions within ``_PROFILE_TTL_SECONDS`` so
        back-to-back sessions reuse it. A failure or an empty card (a brand-new
        user whose profile is not built yet) leaves no profile; the session
        proceeds without one and the next session tries again.
        """
        if not self._warm_profile:
            return
        if self._profile_context and (
            time.monotonic() - self._profile_loaded_at
        ) < _PROFILE_TTL_SECONDS:
            return  # fresh cache, reuse across sessions
        client = self._get_client()
        if not hasattr(client, "get_profile_card"):
            logger.warning(_SDK_TOO_OLD)
            return
        try:
            card = client.get_profile_card()
        except Exception as e:
            self._note_service_issue(e)
            logger.debug("Vity profile warm-load skipped (non-fatal): %s", e)
            return
        context = support.render_profile_card(card)
        if not context:
            return
        self._profile_context = context
        self._profile_fields = context.count("\n") + 1
        self._profile_loaded_at = time.monotonic()

    # -- Retrieval (shared by auto-recall AND the vity_recall tool) -----------

    @staticmethod
    def _dedupe_search(client, query: str, top_k: int, min_score: float = 0.0) -> List[Dict[str, Any]]:
        """Semantic search over stored memories → deduped memories.

        Uses ``search()`` (true semantic retrieval), drops the backend's
        synthesized placeholders, and dedupes the near-identical hits it
        sometimes returns for one memory. Each memory keeps its ``created_at``
        and ``score`` so callers can label how old and how relevant it is.

        ``min_score`` is a relevance floor. It defaults to 0 (unfiltered) for
        explicit tool/CLI use; auto-recall passes a floor so weak matches don't
        get injected as if they were fact.
        """
        results = client.search(query=query, top_k=top_k, min_score=min_score) or []
        seen: set = set()
        memories: List[Dict[str, Any]] = []
        for r in results:
            if _is_placeholder(r):
                continue
            content = (r.get("content") or "").strip()
            if content and content not in seen:
                seen.add(content)
                memories.append({"content": content, "created_at": r.get("created_at"), "score": r.get("score")})
        return memories

    def _recall_for_injection(self, query: str, top_k: int = 10) -> tuple:
        """``(text, count)`` of memories to inject for auto-recall, via search().

        Each line carries the memory's age and relevance (``[3d ago · 82%]``) so
        the model can weigh stale against fresh facts. Bounded to roughly
        ``max_recall_tokens`` (about 4 chars per token), whole lines only.
        """
        client = self._get_recall_client()
        memories = self._dedupe_search(client, query, top_k, min_score=self._recall_min_score)
        budget = max(int(self._max_recall_tokens), 0) * 4
        lines: List[str] = []
        size = 0
        for memory in memories:
            line = support.format_memory_line(memory)
            if budget and size + len(line) + 1 > budget:
                if not lines:  # one oversized memory: keep its start, never nothing
                    lines.append(line[:budget].rstrip())
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines), len(lines)

    def _recall_within_deadline(self, query: str) -> tuple:
        """Run pre-turn recall with a hard wall-clock cap → ``(text, count, failed)``.

        search() may chain a recall() fallback (two HTTP requests), so the
        per-request recall_timeout alone can't bound the total wait. We run the
        recall on a worker thread and give up after ``recall_deadline`` seconds,
        degrading to "no memory this turn" rather than blocking the user. A
        timed-out worker is a daemon and its (discarded) result never lands.
        ``failed`` is True for an error or a timeout (it feeds the recall pause).
        """
        box: Dict[str, Any] = {"text": "", "count": 0, "error": None}

        def _run() -> None:
            try:
                box["text"], box["count"] = self._recall_for_injection(query)
            except Exception as e:
                box["error"] = e
                logger.debug("Vity prefetch retrieval failed (non-fatal): %s", e)

        worker = threading.Thread(target=_run, daemon=True, name="vity-recall")
        worker.start()
        worker.join(timeout=self._recall_deadline)
        if worker.is_alive():
            return "", 0, True
        if box["error"] is not None:
            self._note_service_issue(box["error"])
            return "", 0, True
        return box["text"], box["count"], False

    def _note_service_issue(self, exc: BaseException) -> None:
        """Remember a rejected key or exhausted credits, to tell the model once."""
        kind, _ = support.classify_error(exc)
        if kind in (support.AUTH_ERROR, support.CREDITS_ERROR):
            self._service_issue = kind

    def _record_recall_outcome(self, failed: bool) -> None:
        """Count consecutive failures; pause auto-recall after too many."""
        if not failed:
            self._recall_failures = 0
            return
        self._recall_failures += 1
        if self._recall_failures >= _RECALL_FAILURES_TO_PAUSE:
            self._recall_failures = 0
            self._recall_paused_until = time.monotonic() + _RECALL_PAUSE_SECONDS
            logger.warning(
                "Vity recall failed %d times in a row; pausing auto-recall for %.0fs",
                _RECALL_FAILURES_TO_PAUSE, _RECALL_PAUSE_SECONDS,
            )

    # -- System Prompt Block -------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# Vity Memory (Maximem AI)\n"
            "Active. Memories are scoped to the configured Maximem API key.\n\n"
            "## Rules\n"
            "1. If the user asks about something personal (facts, preferences, history, context) "
            "and it is not already in your injected memory context, retrieve before saying you "
            "don't know. Never claim ignorance without trying. Pick the right tool:\n"
            "   - `vity_recall` for a SPECIFIC stored fact/preference/decision the user told you.\n"
            "   - `vity_knowledge` for what the user broadly KNOWS or their expertise on a topic.\n"
            "   - `vity_profile` for who the user is (role, location, skills, goals, preferences).\n"
            "   - `vity_meeting_recall` for questions about meetings, decisions and attendees.\n"
            + (
                # Every turn is captured and Vity extracts the facts; storing them too saves each twice.
                "2. Conversation turns are saved automatically. Use `vity_store` only when the user "
                "explicitly asks you to remember something.\n"
                if self._auto_capture
                else "2. Use `vity_store` to save any new facts the user shares about themselves.\n"
            )
            + "3. Use `vity_forget` only when the user explicitly asks to delete a memory."
        )

    # -- Prefetch (automatic context injection before each turn) -------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories relevant to THIS message and inject them.

        Runs a synchronous search against the current user message every turn.
        We do NOT rely on a background warm-up: the host warms with the
        *previous* turn's message (a query behind), and ``recall()``'s
        synthesized profile misses specific stored items, the exact reason
        automatic recall silently returned nothing on sparse accounts. A live
        ``search()`` on the current message is what makes recall reliable.
        """
        t0 = time.perf_counter()
        self._last_recall_count = None
        if not self._auto_recall:
            return ""

        # Anaphoric follow-up ("why?", "tell me more"), prepend the last
        # substantial message so the search has a subject. Otherwise search the
        # message verbatim, and let THIS message become the anchor for future
        # follow-ups. Order matters: read the old anchor before overwriting it.
        is_followup = support.needs_prior_context(query)
        augment = is_followup and bool(self._prev_query)
        search_query = f"{self._prev_query} {query}".strip() if augment else query
        if query.strip() and not is_followup:
            self._prev_query = query.strip()

        # Skip trivially short prompts, but ONLY when we couldn't enrich them
        # with prior context (an augmented "why?" is worth searching).
        if (
            not augment
            and len(query.strip()) < self._min_prompt_length
            and not self._cold_start
        ):
            return ""

        was_cold = self._cold_start
        # A rejected key or a recall pause (repeated failures) skips the search
        # entirely, so an outage doesn't cost every turn the full deadline.
        paused = bool(self._service_issue) or time.monotonic() < self._recall_paused_until
        result, count, failed = ("", 0, False) if paused else self._recall_within_deadline(search_query)
        if not paused:
            self._record_recall_outcome(failed)
        self._cold_start = False

        # Inject the general profile ONCE per session, the first turn it's
        # ready (the background warm-load may finish after turn 1, so this is
        # not tied to cold_start). Silent background context, kept separate from
        # the per-turn memory block.
        inject_profile = (not self._profile_injected) and bool(self._profile_context)
        if inject_profile:
            self._profile_injected = True

        self._track(
            "recall_performed",
            {
                "duration_ms": round((time.perf_counter() - t0) * 1000),
                "retrieved": bool(result),
                "memories": count,
                "cold_start": was_cold,
                "paused": paused,
                "failed": failed,
                "profile_injected": inject_profile,
                "profile_fields": self._profile_fields if inject_profile else 0,
            },
        )

        blocks: List[str] = []
        notice = self._service_notice()
        if notice:
            blocks.append(notice)
        if inject_profile:
            blocks.append(
                "## About the user (background context; use only if relevant, do not "
                "recite verbatim; no need to call vity_profile for this)\n" + self._profile_context
            )
        if result:
            blocks.append(
                "## Vity Memory (already retrieved for this message; do not call "
                "vity_recall to look these up again)\n" + result
            )
        if result or inject_profile:
            self._last_recall_count = count
        return "\n\n".join(blocks)

    def _service_notice(self) -> str:
        """Tell the model ONCE per session that memory is paused, and why.

        Without it, a rejected key looks like "no memories" and the agent tells
        the user it knows nothing about them.
        """
        if not self._service_issue or self._service_notice_shown:
            return ""
        self._service_notice_shown = True
        reason = ("the Maximem API key was rejected" if self._service_issue == support.AUTH_ERROR
                  else "the Maximem account is out of credits")
        fix = ("hermes-maximem-vity install --api-key mx_..." if self._service_issue == support.AUTH_ERROR
               else "add credits at https://app.maximem.ai")
        return (
            f"## Vity Memory is unavailable\nLong-term memory is paused because {reason}. "
            "If the user asks about past context, say memory is unavailable right now "
            f"rather than that you know nothing about them. They can fix it: {fix}"
        )

    def recall_status(self):
        """What the last prefetch injected, for the host's recall indicator."""
        if RecallStatus is None or self._last_recall_count is None:
            return None
        return RecallStatus(_PROVIDER_LABEL, self._last_recall_count, glyph="")

    # -- Sync Turn (MUST be non-blocking!) -----------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages=None,
    ) -> None:
        """Queue this turn for capture (non-blocking; the writer thread sends it)."""
        # auto_capture off, or a non-primary context (cron/flush/subagent) whose
        # turns must not be written to the user's memory.
        if not self._auto_capture or not self._write_enabled:
            return
        # Hermes' own notices (compaction summaries, background-task results)
        # arrive as 'user' messages; they are not something the user said.
        if support.is_hermes_notice(user_content):
            return
        now = int(time.time() * 1000)
        self._enqueue_write({
            "kind": "capture",
            "session_key": session_id,
            "messages": [
                {"role": "user", "content": user_content, "timestamp": now},
                {"role": "assistant", "content": assistant_content, "timestamp": now + 1},
            ],
        })

    # -- Session lifecycle ---------------------------------------------------

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Handle a mid-process session change (/reset, /new, /branch, rewind, compression).

        On a genuine new conversation (``reset``) or a truncated transcript
        (``rewound``), re-arm the cold-start flag, drop the follow-up anchor and
        re-inject the profile. Context compression (``reason="compression"``)
        summarizes the turn that carried the profile, so the profile is
        re-injected there too; the conversation itself continues, so the
        follow-up anchor is kept. Writes use the per-turn ``session_id``.
        """
        if reset or rewound:
            self._cold_start = True
            # A new conversation must not carry the old topic's anchor into the
            # first follow-up of the new one.
            self._prev_query = ""
            self._profile_injected = False
        elif kwargs.get("reason") == "compression":
            self._profile_injected = False

    # -- Built-in memory mirroring -------------------------------------------

    def on_memory_write(self, action, target, content, metadata=None):
        """Mirror Hermes' built-in memory writes into Vity.

        - ``add``: store the fact.
        - ``replace``: store the new text, then remove the old one if it can be
          matched exactly (``metadata["old_text"]``), so recall doesn't return
          both the outdated and the current version.
        - ``remove``: remove the matching memory, same exact-match rule.
        """
        if not self._auto_capture or not self._write_enabled:
            return
        old_text = str((metadata or {}).get("old_text") or "").strip()
        memory_type = "preference" if target == "user" else "fact"
        if action in ("add", "replace") and content:
            self._enqueue_write({"kind": "store", "content": content, "memory_type": memory_type})
        if action in ("replace", "remove") and old_text:
            self._enqueue_write({"kind": "remove", "old_text": old_text})

    # -- Write queue -----------------------------------------------------------

    def _enqueue_write(self, job: Dict[str, Any]) -> None:
        """Queue a write for the single writer thread (started on first use)."""
        with self._client_lock:
            if self._writer_thread is None or not self._writer_thread.is_alive():
                self._writer_thread = threading.Thread(
                    target=self._writer_loop, daemon=True, name="vity-writer"
                )
                self._writer_thread.start()
        self._write_queue.put(job)

    def _writer_loop(self) -> None:
        """Drain the write queue in order until the shutdown sentinel."""
        while True:
            job = self._write_queue.get()
            try:
                if job is None:
                    return
                self._run_write(job)
            except Exception as e:  # a failing job must never kill the writer
                logger.warning("Vity write failed unexpectedly: %s", e)
            finally:
                self._write_queue.task_done()

    def _run_write(self, job: Dict[str, Any]) -> None:
        """Deliver one write, retrying once. A capture or store that still fails
        is saved to disk and replayed on the next start, so a stated fact is not
        lost to a network blip or an outage."""
        for attempt in (1, 2):
            try:
                self._deliver(job)
                if job["kind"] == "capture":
                    self._track("capture_result", {"status": "accepted" if attempt == 1 else "retry_accepted"})
                return
            except Exception as e:
                last_error = e
                if support.classify_error(e)[0] == support.INVALID_ERROR:
                    break  # the request itself is wrong: a retry can't succeed
                if attempt == 1:
                    time.sleep(1.0)
        kind, message = support.classify_error(last_error)
        logger.warning("Vity %s failed: %s", job["kind"], message)
        if job["kind"] == "capture":
            self._track("capture_result", {"status": "failed", "reason": kind})
        # Saved for replay only if a later attempt could succeed. Not for invalid
        # input (it would fail on every start), and not for a rejected key: if the
        # key is changed to another account, replaying would leak this account's
        # conversation into it.
        if job["kind"] in ("capture", "store") and kind not in (support.AUTH_ERROR, support.INVALID_ERROR):
            self._persist_pending([job])

    def _deliver(self, job: Dict[str, Any]) -> None:
        client = self._get_client()
        if job["kind"] == "capture":
            # Capture is async server-side: a non-error return means "accepted
            # for processing"; extraction into durable facts happens later.
            client.capture(messages=job["messages"], agent_id="hermes", session_key=job["session_key"])
        elif job["kind"] == "store":
            client.store(content=job["content"], memory_type=job["memory_type"])
        elif job["kind"] == "remove":
            self._remove_matching_memory(client, job["old_text"])

    def _remove_matching_memory(self, client, old_text: str) -> None:
        """Delete the stored copy of ``old_text``, only when it matches exactly.

        The API deletes by query, not by id, and a query can match more than the
        intended fact. So we preview what the delete would remove and proceed
        only if EVERY memory in that preview is confirmed (by a decrypted search)
        to be a near-verbatim copy of ``old_text``. Anything else is left alone.
        """
        preview = client.forget(query=old_text, dry_run=True) or {}
        doomed = {str(i) for i in (preview.get("ids") or [])}
        if not doomed:
            return
        confirmed = {
            str(r.get("id")) for r in (client.search(query=old_text, top_k=10) or [])
            if support.is_same_fact(r.get("content") or "", old_text)
        }
        if not doomed <= confirmed:
            logger.info("Vity skipped removing %d memories: not all were an exact match", len(doomed))
            return
        client.forget(query=old_text, dry_run=False)

    def _persist_pending(self, jobs: List[Dict[str, Any]]) -> None:
        if not jobs or self._pending_path is None:
            return
        try:
            support.save_pending_writes(self._pending_path, jobs)
        except Exception as e:
            logger.warning("Vity could not save %d undelivered writes: %s", len(jobs), e)

    # -- Tool Schemas + Dispatch ---------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Static on purpose: Hermes reads this ONCE, before initialize(), to build
        # its tool routing. Runtime capability (Meeting Memory) is checked in the
        # handler instead of by hiding the tool.
        schemas = list(ALL_TOOL_SCHEMAS)
        if _ROUTER_CRUD_LIVE:
            schemas.extend([VITY_UPDATE_SCHEMA, VITY_DELETE_SCHEMA])
        return schemas

    @staticmethod
    def _tool_failure(action: str, exc: BaseException) -> str:
        """Tool error the model can act on: names a rejected key, missing credits
        or an outage instead of returning a bare exception (or "no memories")."""
        kind, message = support.classify_error(exc)
        return tool_error(message if kind != "other" else f"{action} failed: {message}")

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        """Dispatch tool calls from the agent to Vity's API."""
        tool_start = time.perf_counter()

        try:
            client = self._get_client()
        except Exception as e:
            return tool_error(str(e))

        try:
            if tool_name == "vity_profile":
                if not hasattr(client, "get_profile_card"):
                    return tool_error(_SDK_TOO_OLD)
                try:
                    rows = support.profile_rows(client.get_profile_card(), support.PROFILE_TOOL_MAX_ITEMS)
                    if not rows:
                        return json.dumps({"result": "No profile yet. It is built automatically from conversations."})
                    return json.dumps({"result": {label: values for label, values in rows}, "source": "vity"})
                except Exception as e:
                    return self._tool_failure("Profile lookup", e)

            elif tool_name == "vity_recall":
                query = args.get("query", "")
                if not query:
                    return tool_error("Missing required parameter: query")
                top_k = min(int(args.get("top_k", 10)), 50)
                try:
                    memories = self._dedupe_search(client, query, top_k)
                    if memories:
                        return json.dumps({"result": [
                            {"memory": m["content"], "age": support.age_label(m["created_at"]) or None,
                             "relevance": round(m["score"], 2) if isinstance(m["score"], (int, float)) else None}
                            for m in memories
                        ]})
                    # Fall back to the recall/profile context if search is empty.
                    context = client.recall(
                        current_prompt=query,
                        max_tokens=top_k * 100,
                        strategy="hybrid",
                    )
                    if not context:
                        return json.dumps({"result": "No relevant memories found."})
                    return json.dumps({"result": context})
                except Exception as e:
                    return self._tool_failure("Memory search", e)

            elif tool_name == "vity_store":
                content = args.get("content", "")
                if not content:
                    return tool_error("Missing required parameter: content")
                memory_type = args.get("memory_type", "fact")
                try:
                    result = client.store(content=content, memory_type=memory_type)
                    return json.dumps({"result": "Memory stored successfully.", "id": (result or {}).get("id", "")})
                except Exception as e:
                    return self._tool_failure("Storing the memory", e)

            elif tool_name == "vity_forget":
                query = str(args.get("query") or "").strip()
                if not query:
                    return tool_error("Missing required parameter: query (say what to forget)")
                dry_run = args.get("dry_run", True)
                try:
                    result = client.forget(query=query, dry_run=dry_run)
                    count = result.get("count", 0)
                    if dry_run:
                        return json.dumps({"result": f"Would delete {count} memories. Call again with dry_run=false to confirm."})
                    return json.dumps({"result": f"Deleted {count} memories.", "count": count})
                except Exception as e:
                    return self._tool_failure("Forget", e)

            elif tool_name == "vity_knowledge":
                query = args.get("query", "")
                if not query:
                    return tool_error("Missing required parameter: query")
                limit = min(int(args.get("limit", 10)), 100)
                try:
                    # scope="chat" is the only live scope today (echo/all -> 501).
                    resp = client.knowledge(query=query, scope="chat", limit=limit)
                    items = (resp or {}).get("items", [])
                    if not items:
                        return json.dumps({"result": "No relevant knowledge found."})
                    # The backend can return several near-identical rows for one
                    # topic whose summaries differ by a few characters, so exact
                    # dedup misses them. Knowledge is already topic-consolidated, so collapse
                    # to one row per topic, items arrive rank-ordered, so the
                    # first is the best. Fall back to the summary as the key when
                    # a row carries no topic.
                    seen: set = set()
                    trimmed = []
                    for it in items:
                        topic = it.get("topic")
                        key = topic or it.get("summary")
                        if key in seen:
                            continue
                        seen.add(key)
                        trimmed.append({
                            "topic": topic,
                            "category": it.get("topic_category"),
                            "summary": it.get("summary"),
                        })
                    return json.dumps({"result": trimmed, "source": "vity"})
                except Exception as e:
                    return self._tool_failure("Knowledge query", e)

            elif tool_name == "vity_meeting_recall":
                question = args.get("question", "")
                if not question:
                    return tool_error("Missing required parameter: question")
                if self._meetings_enabled is None:  # warm-up probe hasn't finished
                    self._meetings_enabled = client.meetings_enabled()
                if not self._meetings_enabled:
                    return json.dumps({"result": (
                        "Meeting Memory is not enabled for this Maximem account, so there is "
                        "no meeting history to search. The user can enable it at "
                        "https://app.maximem.ai. Do not retry this tool."
                    )})
                try:
                    resp = client.meeting_ask(
                        question=question,
                        meeting_type=args.get("meeting_type", ""),
                        status=args.get("status", ""),
                    )
                    return json.dumps({"result": resp, "source": "vity_meetings"})
                except Exception as e:
                    return self._tool_failure("Meeting recall", e)

            elif tool_name == "vity_update":
                memory_id = args.get("memory_id", "")
                content = args.get("content", "")
                if not memory_id or not content:
                    return tool_error("Missing required parameter: memory_id and content")
                try:
                    client.update(memory_id, content=content)
                    return json.dumps({"result": "Memory updated.", "id": memory_id})
                except Exception as e:
                    return tool_error(f"Update failed: {e}")

            elif tool_name == "vity_delete":
                memory_id = args.get("memory_id", "")
                if not memory_id:
                    return tool_error("Missing required parameter: memory_id")
                try:
                    client.delete(memory_id)
                    return json.dumps({"result": "Memory deleted.", "id": memory_id})
                except Exception as e:
                    return tool_error(f"Delete failed: {e}")

            return tool_error(f"Unknown tool: {tool_name}")

        finally:
            duration_ms = round((time.perf_counter() - tool_start) * 1000)
            self._track("tool_called", {"tool": tool_name, "duration_ms": duration_ms})

    # -- Shutdown ------------------------------------------------------------

    def shutdown(self) -> None:
        """Flush background work before the process exits. Idempotent.

        Waits (bounded by ``_WRITE_DRAIN_TIMEOUT_S``) for queued writes; any
        write still not delivered is saved to disk and replayed next start.
        """
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=5.0)
        writer = self._writer_thread
        if writer is not None and writer.is_alive():
            self._write_queue.put(None)  # sentinel: stop after the queued writes
            writer.join(timeout=_WRITE_DRAIN_TIMEOUT_S)
        leftover: List[Dict[str, Any]] = []
        while True:
            try:
                job = self._write_queue.get_nowait()
            except queue.Empty:
                break
            if job is not None and job.get("kind") in ("capture", "store"):
                leftover.append(job)
        self._persist_pending(leftover)
        with self._client_lock:
            self._writer_thread = None
            for attr in ("_client", "_recall_client"):
                client = getattr(self, attr)
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Plugin Entry Point (required by Hermes discovery system)
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Called by Hermes memory plugin discovery. Registers Vity as a provider."""
    ctx.register_memory_provider(VityMemoryProvider())
