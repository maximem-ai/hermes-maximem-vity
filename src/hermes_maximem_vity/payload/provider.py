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
import time
import os
import sys
import threading
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

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
_PROFILE_MAX_CHARS = 1200
# Card values below this confidence are left out: injected context reads to the
# agent as fact. Manual overrides (the user's own edits) are always kept.
_PROFILE_MIN_CONFIDENCE = 0.6
# Per-field item cap: the session-start block stays compact; the vity_profile
# tool (an explicit request) shows more.
_PROFILE_INJECT_MAX_ITEMS = 5
_PROFILE_TOOL_MAX_ITEMS = 20
# Shutdown waits up to this long (total) for in-flight memory writes. A store()
# does classification + embedding server-side and can take several seconds;
# without the wait, a fact saved just before exit was silently lost.
_WRITE_DRAIN_TIMEOUT_S = 15.0


def _card_values(entry: Any, max_items: int) -> List[str]:
    """Confident values of one Profile Card field (a single value or a list)."""
    entries = entry if isinstance(entry, list) else [entry]
    values: List[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        value = str(e.get("value") or "").strip()
        confident = e.get("is_override") or (e.get("confidence") or 0) >= _PROFILE_MIN_CONFIDENCE
        if value and confident and value not in values:
            values.append(value)
    return values[:max_items]


def _profile_rows(card: Any, max_items: int) -> List[tuple]:
    """The Profile Card as ``(label, values)`` rows, current state only.

    Never includes the email (not useful context, and personal data) or
    ``candidates`` (speculative or ended values, never current state).
    """
    if not isinstance(card, dict):
        return []
    section = lambda name: card.get(name) if isinstance(card.get(name), dict) else {}
    identity, pro = section("identity"), section("professional")
    personal, knowledge, goals = section("personal"), section("knowledge"), section("goals")

    role = _card_values(pro.get("current_role"), 1)
    company = _card_values(pro.get("current_company"), 1)
    work = [f"{role[0]} at {company[0]}"] if role and company else role or company

    rows = [
        ("Name", _card_values(identity.get("name"), 1)),
        ("Work", work),
        ("Location", _card_values(personal.get("location"), 1)),
        ("Education", _card_values(personal.get("education"), max_items)),
        ("Expertise", _card_values(knowledge.get("expertise"), max_items)),
        ("Domains", _card_values(knowledge.get("top_domains"), max_items)),
        ("Skills", _card_values(knowledge.get("skills"), max_items)),
        ("Interests", _card_values(personal.get("interests"), max_items)),
        ("Active goals", _card_values(goals.get("active"), max_items)),
        ("Preferences", _card_values(card.get("preferences"), max_items)),
    ]
    return [(label, values) for label, values in rows if values]


def _render_profile_card(card: Any) -> str:
    """The Profile Card as compact third-person lines for injection, or "".

    Whole lines only: a line that would push past ``_PROFILE_MAX_CHARS`` is
    dropped rather than cut mid-item.
    """
    lines: List[str] = []
    size = 0
    for label, values in _profile_rows(card, _PROFILE_INJECT_MAX_ITEMS):
        line = f"- {label}: {'; '.join(values)}"
        if size + len(line) + 1 > _PROFILE_MAX_CHARS:
            continue
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Anaphoric follow-up detection
# ---------------------------------------------------------------------------
# prefetch searches on the current message alone. A self-contained message
# ("what's my sister's birthday?") searches fine. A follow-up that leans on the
# prior turn ("tell me more", "why?", "what about that?") has no searchable
# subject of its own, so we prepend the last substantial message. Kept to plain
# token rules (no NLP dependency) and deliberately conservative: only SHORT
# messages that actually look anaphoric trigger augmentation, so clean topic
# changes are never diluted with stale terms.
_ANAPHORIC_TOKENS = frozenset({
    "it", "its", "that", "this", "these", "those", "them", "they", "their",
    "he", "him", "his", "she", "her", "one", "ones", "there", "same", "such",
})
_FOLLOWUP_STARTS = (
    "tell me more", "what about", "how about", "what else", "and what",
    "go on", "and then", "and?", "what other",
)
_BARE_INTERROGATIVES = frozenset({
    "why", "how", "really", "more", "continue", "elaborate", "explain", "meaning",
})
# Only messages this short are candidates for augmentation. Longer messages are
# assumed to stand on their own.
_ANAPHORA_MAX_WORDS = 6


def _needs_prior_context(query: str) -> bool:
    """True when ``query`` is a short follow-up that can't be searched alone."""
    q = query.strip().lower().rstrip("?.!,")
    words = q.split()
    n = len(words)
    if n == 0 or n > _ANAPHORA_MAX_WORDS:
        return False
    if n == 1 and words[0] in _BARE_INTERROGATIVES:
        return True
    if any(q.startswith(p) for p in _FOLLOWUP_STARTS):
        return True
    return any(w in _ANAPHORIC_TOKENS for w in words)


# ---------------------------------------------------------------------------
# Tool Schemas (what the agent sees and can call)
# ---------------------------------------------------------------------------

VITY_RECALL_SCHEMA = {
    "name": "vity_recall",
    "description": (
        "Retrieve SPECIFIC stored memories about the user: individual facts, "
        "preferences, past decisions, project context. Returns discrete, verbatim "
        "items from the memory graph. Use this when you need a particular thing the "
        "user told you. (For what the user broadly KNOWS about a topic, use "
        "vity_knowledge instead.)"
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
        "Use for a quick snapshot of who the user is."
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

VITY_KNOWLEDGE_SCHEMA = {
    "name": "vity_knowledge",
    "description": (
        "Query the user's consolidated KNOWLEDGE: topic-level summaries of what they "
        "know or have learned, distilled across many memories. Returns items with "
        "topic, category, and a synthesized summary. Use for 'what does the user know "
        "about X' or their expertise on a subject. (For a specific individual fact the "
        "user stated, use vity_recall instead.)"
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

# Offered only when Meeting Memory is enabled for the account, gated at runtime
# by meetings_enabled().
VITY_MEETING_RECALL_SCHEMA = {
    "name": "vity_meeting_recall",
    "description": (
        "Answer a question using the user's MEETING history: transcripts, decisions, "
        "action items, attendees. Use for questions about what was discussed or decided "
        "in meetings, who attended, or what is still open."
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
        except Exception:
            pass

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
        # Background threads (non-blocking pattern, REQUIRED by Hermes contract).
        # Recall runs synchronously per-turn in prefetch() against the current
        # message; only capture/sync runs in the background.
        self._prefetch_thread = None
        self._sync_thread = None
        # In-flight memory writes (built-in memory mirroring), drained at shutdown.
        self._write_threads: List[threading.Thread] = []
        self._atexit_registered = False
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
        # Timing: measure Vity API time vs Hermes LLM time
        self._vity_prefetch_ms: float = 0.0  # how long prefetch() waited for Vity
        self._vity_tool_ms: float = 0.0      # accumulated time inside handle_tool_call() this turn
        self._prefetch_done_at: float = 0.0  # perf_counter timestamp when prefetch returned
        self._vity_retrieved = False         # did this turn actually pull memory?
        # Whether Meeting Memory is enabled for this account. Probed off-turn in
        # _warm_recall() so it costs nothing on the critical path; defaults False
        # so the meeting tool is hidden until the account confirms support.
        self._meetings_enabled = False

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
        """Check config only, NO network calls allowed here."""
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
        # said them, corrupting the user's memory profile. Mirrors the in-tree
        # honcho/supermemory providers.
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
        # interpreter exit too (same pattern as the bundled hindsight provider).
        if not self._atexit_registered:
            self._atexit_registered = True
            atexit.register(self.shutdown)

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
            except ImportError:
                raise RuntimeError(
                    "maximem-vity-sdk not installed. "
                    "Run: pip install maximem-vity-sdk"
                )

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
            except ImportError:
                raise RuntimeError(
                    "maximem-vity-sdk not installed. "
                    "Run: pip install maximem-vity-sdk"
                )

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
            logger.debug("Vity recall warm-up skipped (non-fatal): %s", e)
        # Probe meeting-memory availability off-turn. meetings_enabled() degrades
        # to False on any error, so this never raises and the meeting tool stays
        # hidden until the account genuinely supports it.
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
        import time
        if not self._warm_profile:
            return
        if self._profile_context and (
            time.monotonic() - self._profile_loaded_at
        ) < _PROFILE_TTL_SECONDS:
            return  # fresh cache, reuse across sessions
        try:
            card = self._get_client().get_profile_card()
        except Exception as e:
            logger.debug("Vity profile warm-load skipped (non-fatal): %s", e)
            return
        context = _render_profile_card(card)
        if not context:
            return
        self._profile_context = context
        self._profile_fields = context.count("\n") + 1
        self._profile_loaded_at = time.monotonic()

    # -- Retrieval (shared by auto-recall AND the vity_recall tool) -----------

    @staticmethod
    def _dedupe_search(client, query: str, top_k: int, min_score: float = 0.0) -> List[str]:
        """Semantic search over stored memories → deduped list of contents.

        Uses ``search()`` (true semantic retrieval), drops the backend's
        synthesized placeholders, and dedupes the near-identical hits it
        sometimes returns for one memory. This is the retrieval that actually
        surfaces specific stored items (the same call ``hermes maximem_vity search``
        makes), as opposed to ``recall()``, whose synthesized profile reliably
        misses them.

        ``min_score`` is a relevance floor. It defaults to 0 (unfiltered) for
        explicit tool/CLI use; auto-recall passes a floor so weak matches don't
        get injected as if they were fact.
        """
        results = client.search(query=query, top_k=top_k, min_score=min_score) or []
        seen: set = set()
        memories: List[str] = []
        for r in results:
            if _is_placeholder(r):
                continue
            content = (r.get("content") or "").strip()
            if content and content not in seen:
                seen.add(content)
                memories.append(content)
        return memories

    def _recall_for_injection(self, query: str, top_k: int = 10) -> str:
        """Memories to inject for auto-recall, or "", via search(), short timeout.

        Sparse/cold accounts are handled by the SDK's own fallback: when the
        ``/search`` endpoint is empty, ``search()`` transparently pulls the
        ``sources`` from ``/recall`` (real memories, not the synthesized profile
        blob, which ``_dedupe_search`` filters out). That keeps auto-recall to at
        most two HTTP calls, safely inside the pre-turn deadline. If it's still
        empty or slow, we inject nothing this turn rather than block the user.
        """
        client = self._get_recall_client()
        memories = self._dedupe_search(client, query, top_k, min_score=self._recall_min_score)
        if not memories:
            return ""
        text = "\n".join(f"- {m}" for m in memories)
        # Bound the injected context to ~max_recall_tokens (approx 4 chars/token)
        # so a large memory set can't crowd out the conversation.
        budget = max(int(self._max_recall_tokens), 0) * 4
        if budget and len(text) > budget:
            text = text[:budget].rstrip()
        return text

    def _recall_within_deadline(self, query: str) -> str:
        """Run pre-turn recall with a hard wall-clock cap.

        search() may chain a recall() fallback (two HTTP requests), so the
        per-request recall_timeout alone can't bound the total wait. We run the
        recall on a worker thread and give up after ``recall_deadline`` seconds,
        degrading to "no memory this turn" rather than blocking the user. A
        timed-out worker is a daemon and its (discarded) result never lands.
        """
        box: Dict[str, str] = {"text": ""}

        def _run() -> None:
            try:
                box["text"] = self._recall_for_injection(query)
            except Exception as e:
                # Slow/unhealthy endpoint, degrade to no memory this turn.
                logger.debug("Vity prefetch retrieval failed (non-fatal): %s", e)

        worker = threading.Thread(target=_run, daemon=True, name="vity-recall")
        worker.start()
        worker.join(timeout=self._recall_deadline)
        return box["text"]

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
            "2. Use `vity_store` to save any new facts the user shares about themselves.\n"
            "3. Use `vity_forget` only when the user explicitly asks to delete a memory."
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
        import time as _time
        _t0 = _time.perf_counter()

        if not self._auto_recall:
            return ""

        # Anaphoric follow-up ("why?", "tell me more"), prepend the last
        # substantial message so the search has a subject. Otherwise search the
        # message verbatim, and let THIS message become the anchor for future
        # follow-ups. Order matters: read the old anchor before overwriting it.
        is_followup = _needs_prior_context(query)
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
        result = self._recall_within_deadline(search_query)

        self._cold_start = False

        # Inject the general profile ONCE per session, the first turn it's
        # ready (the background warm-load may finish after turn 1, so this is
        # not tied to cold_start). Silent background context, kept separate from
        # the per-turn memory block.
        inject_profile = (not self._profile_injected) and bool(self._profile_context)
        if inject_profile:
            self._profile_injected = True

        self._vity_prefetch_ms = (_time.perf_counter() - _t0) * 1000
        self._vity_tool_ms = 0.0
        self._prefetch_done_at = _time.perf_counter()
        self._vity_retrieved = bool(result) or inject_profile

        self._track(
            "recall_performed",
            {
                "duration_ms": round(self._vity_prefetch_ms),
                "retrieved": bool(result),
                "cold_start": was_cold,
                "profile_injected": inject_profile,
                "profile_fields": self._profile_fields if inject_profile else 0,
            },
        )

        blocks: List[str] = []
        if inject_profile:
            blocks.append(
                "## About the user (background context; use only if relevant, "
                "do not recite verbatim)\n" + self._profile_context
            )
        if result:
            blocks.append(f"## Vity Memory\n{result}")
        return "\n\n".join(blocks)

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

        # Timing breakdown for the CLI feed, only when Vity actually retrieved.
        if self._prefetch_done_at > 0:
            total_turn_ms = (time.perf_counter() - self._prefetch_done_at) * 1000
            vity_total_ms = self._vity_prefetch_ms + self._vity_tool_ms
            hermes_llm_ms = max(0.0, total_turn_ms - self._vity_tool_ms)
            # Only render the timing line on a real terminal, on non-TTY
            # sinks (pipes, logs, messaging gateways) the ANSI escapes show up
            # as literal `?[2m…?[0m` garble.
            if self._vity_retrieved and sys.stdout.isatty():
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

        # auto_capture off, or a non-primary context (cron/flush/subagent) whose
        # turns must not be written to the user's memory.
        if not self._auto_capture or not self._write_enabled:
            return

        turn_messages = [
            {"role": "user", "content": user_content, "timestamp": now},
            {"role": "assistant", "content": assistant_content, "timestamp": now + 1},
        ]

        def _sync():
            try:
                client = self._get_client()
                # Capture is async server-side: the backend enqueues the turn and
                # extracts durable facts through several LLM stages later, so a
                # non-error return means "accepted for processing" (counts come
                # back None).
                result = client.capture(
                    messages=turn_messages,
                    agent_id="hermes",
                    session_key=session_id,
                ) or {}
                logger.debug("Vity capture accepted (async, extraction deferred): %s", result)
                self._track("capture_result", {"status": "accepted"})

                # Forward-compatible observability: once the router returns a
                # request_id AND maps the status route, poll it so a dropped
                # extraction is visible instead of silent. Until then request_id
                # is absent / capture_status 404s, and this block is a no-op;
                # behaviour identical to before. Never blocks the user (bg thread).
                request_id = result.get("request_id") or result.get("requestId")
                if request_id:
                    try:
                        status = client.capture_status(request_id)
                        logger.debug("Vity capture status %s: %s", request_id, status)
                    except Exception as se:
                        logger.debug("Vity capture status unavailable (%s)", se)
            except Exception as e:
                logger.warning("Vity sync failed, retrying once: %s", e)
                # One retry, a transient network blip should not silently lose a
                # turn. Still best-effort: a second failure is logged and dropped.
                try:
                    self._get_client().capture(
                        messages=turn_messages, agent_id="hermes", session_key=session_id
                    )
                    logger.debug("Vity capture retry accepted")
                    self._track("capture_result", {"status": "retry_accepted"})
                except Exception as e2:
                    logger.warning("Vity sync retry also failed, turn not captured: %s", e2)
                    self._track("capture_result", {"status": "failed"})

        # Wait for previous sync before starting a new one.
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="vity-sync")
        self._sync_thread.start()

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
        """Handle a mid-process session change (/reset, /new, /branch, rewind).

        The only per-session state we cache is the cold-start flag, which exempts
        a session's first message from the min-length recall skip. On a genuine
        new conversation (``reset``) or a truncated transcript (``rewound``),
        re-arm it so the fresh first turn still gets recall. Writes use the
        per-turn ``session_id`` passed to sync_turn(), so nothing else to reset.
        """
        if reset or rewound:
            self._cold_start = True
            # A new conversation must not carry the old topic's anchor into the
            # first follow-up of the new one.
            self._prev_query = ""
            # Re-inject the profile once in the fresh conversation (cached text
            # is kept; only the per-session flag resets).
            self._profile_injected = False

    # -- Built-in memory mirroring -------------------------------------------

    def on_memory_write(self, action, target, content, metadata=None):
        """Mirror Hermes' built-in memory writes into Vity.

        When the user (or agent) writes to Hermes' built-in memory, the same
        fact is stored in Vity so it participates in semantic recall. Only
        mirrors 'add'/'replace'.
        """
        if (
            not self._auto_capture
            or not self._write_enabled
            or action not in {"add", "replace"}
            or not content
        ):
            return

        def _mirror():
            try:
                client = self._get_client()
                memory_type = "preference" if target == "user" else "fact"
                client.store(content=content, memory_type=memory_type)
            except Exception as e:
                logger.debug("Vity on_memory_write mirror failed: %s", e)

        thread = threading.Thread(target=_mirror, daemon=True, name="vity-memwrite")
        self._write_threads = [t for t in self._write_threads if t.is_alive()] + [thread]
        thread.start()

    # -- Tool Schemas + Dispatch ---------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Base tools are always on. Router-pending tools are added only when the
        # capability is confirmed, so the agent is never handed a tool that 404s.
        schemas = list(ALL_TOOL_SCHEMAS)
        if self._meetings_enabled:
            schemas.append(VITY_MEETING_RECALL_SCHEMA)
        if _ROUTER_CRUD_LIVE:
            schemas.extend([VITY_UPDATE_SCHEMA, VITY_DELETE_SCHEMA])
        return schemas

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
                    rows = _profile_rows(client.get_profile_card(), _PROFILE_TOOL_MAX_ITEMS)
                    if not rows:
                        return json.dumps({"result": "No profile yet. It is built automatically from conversations."})
                    self._vity_retrieved = True
                    return json.dumps({"result": {label: values for label, values in rows}, "source": "vity"})
                except Exception as e:
                    return tool_error(f"Failed to fetch profile: {e}")

            elif tool_name == "vity_recall":
                query = args.get("query", "")
                if not query:
                    return tool_error("Missing required parameter: query")
                top_k = min(int(args.get("top_k", 10)), 50)
                try:
                    memories = self._dedupe_search(client, query, top_k)
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
                    return json.dumps({"result": "Memory stored successfully.", "id": (result or {}).get("id", "")})
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
                    self._vity_retrieved = True
                    return json.dumps({"result": trimmed, "source": "vity"})
                except Exception as e:
                    return tool_error(f"Knowledge query failed: {e}")

            elif tool_name == "vity_meeting_recall":
                question = args.get("question", "")
                if not question:
                    return tool_error("Missing required parameter: question")
                try:
                    resp = client.meeting_ask(
                        question=question,
                        meeting_type=args.get("meeting_type", ""),
                        status=args.get("status", ""),
                    )
                    self._vity_retrieved = True
                    return json.dumps({"result": resp, "source": "vity_meetings"})
                except Exception as e:
                    return tool_error(f"Meeting recall failed: {e}")

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
            _dur = (_t.perf_counter() - _tool_start) * 1000
            self._vity_tool_ms += _dur
            self._track("tool_called", {"tool": tool_name, "duration_ms": round(_dur)})

    # -- Shutdown ------------------------------------------------------------

    def shutdown(self) -> None:
        """Flush background threads before process exits. Idempotent."""
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # Memory writes carry user-stated facts: wait for them (bounded) so a
        # fact saved just before exit is not lost.
        deadline = time.monotonic() + _WRITE_DRAIN_TIMEOUT_S
        for t in self._write_threads:
            if t.is_alive():
                t.join(timeout=max(0.0, deadline - time.monotonic()))
        self._write_threads = [t for t in self._write_threads if t.is_alive()]
        with self._client_lock:
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
