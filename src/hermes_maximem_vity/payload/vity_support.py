"""Stateless helpers for the Vity memory provider.

Pure functions only (no Hermes imports, no network): Profile Card rendering,
follow-up detection, capture filtering, recall labels, fact matching, error
messages, and the on-disk queue of writes that could not be delivered. Kept
apart from provider.py so each piece is small and unit-testable on its own.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Profile Card rendering
# ---------------------------------------------------------------------------

PROFILE_MAX_CHARS = 1200
# Card values below this confidence are left out: injected context reads to the
# agent as fact. Manual overrides (the user's own edits) are always kept.
PROFILE_MIN_CONFIDENCE = 0.6
# Per-field item cap: the session-start block stays compact; the vity_profile
# tool (an explicit request) shows more.
PROFILE_INJECT_MAX_ITEMS = 5
PROFILE_TOOL_MAX_ITEMS = 20


def one_line(text: Any) -> str:
    """Collapse all whitespace (incl. newlines) so stored text can't forge extra
    lines like "- Name: ..." in the injected block."""
    return " ".join(str(text or "").split())


def card_values(entry: Any, max_items: int) -> List[str]:
    """Confident values of one Profile Card field (a single value or a list)."""
    entries = entry if isinstance(entry, list) else [entry]
    values: List[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        value = one_line(e.get("value"))
        confident = e.get("is_override") or (e.get("confidence") or 0) >= PROFILE_MIN_CONFIDENCE
        if value and confident and value not in values:
            values.append(value)
    return values[:max_items]


def profile_rows(card: Any, max_items: int) -> List[Tuple[str, List[str]]]:
    """The Profile Card as ``(label, values)`` rows, current state only.

    Never includes the email (not useful context, and personal data) or
    ``candidates`` (speculative or ended values, never current state).
    """
    if not isinstance(card, dict):
        return []

    def section(name: str) -> Dict[str, Any]:
        value = card.get(name)
        return value if isinstance(value, dict) else {}

    identity, pro = section("identity"), section("professional")
    personal, knowledge, goals = section("personal"), section("knowledge"), section("goals")

    role = card_values(pro.get("current_role"), 1)
    company = card_values(pro.get("current_company"), 1)
    work = [f"{role[0]} at {company[0]}"] if role and company else role or company

    rows = [
        ("Name", card_values(identity.get("name"), 1)),
        ("Work", work),
        ("Location", card_values(personal.get("location"), 1)),
        ("Education", card_values(personal.get("education"), max_items)),
        ("Expertise", card_values(knowledge.get("expertise"), max_items)),
        ("Domains", card_values(knowledge.get("top_domains"), max_items)),
        ("Skills", card_values(knowledge.get("skills"), max_items)),
        ("Interests", card_values(personal.get("interests"), max_items)),
        ("Active goals", card_values(goals.get("active"), max_items)),
        ("Preferences", card_values(card.get("preferences"), max_items)),
    ]
    return [(label, values) for label, values in rows if values]


def render_profile_card(card: Any, max_chars: int = PROFILE_MAX_CHARS) -> str:
    """The Profile Card as compact third-person lines for injection, or "".

    Whole lines only: a line that would push past ``max_chars`` is dropped
    rather than cut mid-item.
    """
    lines: List[str] = []
    size = 0
    for label, values in profile_rows(card, PROFILE_INJECT_MAX_ITEMS):
        line = f"- {label}: {'; '.join(values)}"
        if size + len(line) + 1 > max_chars:
            continue
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Anaphoric follow-up detection
# ---------------------------------------------------------------------------
# A follow-up that leans on the prior turn ("tell me more", "why?") has no
# searchable subject of its own, so the caller prepends the last substantial
# message. Deliberately conservative: only SHORT messages that look anaphoric.

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
_ANAPHORA_MAX_WORDS = 6


def needs_prior_context(query: str) -> bool:
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
# Capture filtering
# ---------------------------------------------------------------------------
# Hermes delivers its own machine notices (compaction summaries, background-task
# completions) through the user-message channel. They are execution metadata,
# not something the user said, and must never become durable memory. Anchored
# at the start so a person quoting one of these mid-sentence is still captured.
_HERMES_NOTICE_RE = re.compile(
    r"^\s*(?:"
    r"\[ASYNC (?:DELEGATION )?(?:BATCH )?COMPLETE[^\]]*\]|"
    r"\[CONTEXT COMPACTION[^\]]*\]|"
    r"\[CONTEXT SUMMARY\]:?|"
    r"\[PRIOR CONTEXT[^\]]*\]|"
    r"\[Your active task list was preserved across context compression\]|"
    r"\[IMPORTANT: Background process \d+ matched watch pattern[^\n]*|"
    r"A background fan-out of \d+ subagent\(s\) you dispatched earlier has finished\.|"
    r"A background subagent you dispatched earlier has finished\."
    r")",
    re.IGNORECASE,
)


def is_hermes_notice(text: str) -> bool:
    """True for a Hermes-generated notice that arrived as a 'user' message."""
    return bool(_HERMES_NOTICE_RE.match(text or ""))


# ---------------------------------------------------------------------------
# Recall labels
# ---------------------------------------------------------------------------

def age_label(created_at_ms: Any, now_s: Optional[float] = None) -> str:
    """Human age of a memory ("3d ago"), or "" when the timestamp is unknown."""
    try:
        created_s = float(created_at_ms) / 1000.0
    except (TypeError, ValueError):
        return ""
    if created_s <= 0:
        return ""
    seconds = max(0.0, (time.time() if now_s is None else now_s) - created_s)
    for unit, size in (("y", 365 * 86400), ("mo", 30 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def format_memory_line(memory: Dict[str, Any], now_s: Optional[float] = None) -> str:
    """One injected memory: ``- [3d ago · 82%] content`` (labels when known)."""
    labels = []
    age = age_label(memory.get("created_at"), now_s)
    if age:
        labels.append(age)
    score = memory.get("score")
    if isinstance(score, (int, float)) and 0 < score <= 1:
        labels.append(f"{round(score * 100)}%")
    prefix = f"[{' · '.join(labels)}] " if labels else ""
    return f"- {prefix}{one_line(memory['content'])}"


# ---------------------------------------------------------------------------
# Fact matching (for mirroring built-in memory 'replace' / 'remove')
# ---------------------------------------------------------------------------

# Words that carry no fact: the subject (the backend rewrites "I" to "User") and
# filler. Everything else must match exactly for two facts to count as the same.
_FILLER_WORDS = frozenset({
    "i", "im", "my", "me", "user", "users", "the", "a", "an", "is", "am", "are", "was",
    "be", "to", "of", "and", "that", "it", "with", "for", "in", "on", "at",
})


def _fact_words(text: str) -> frozenset:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    # Naive stemming so "drink"/"drinks" and "theme"/"themes" compare equal.
    return frozenset(w[:-1] if len(w) > 3 and w.endswith("s") else w
                     for w in words if w not in _FILLER_WORDS)


def is_same_fact(stored: str, old_text: str) -> bool:
    """True when ``stored`` states exactly the same fact as ``old_text``.

    Strict on purpose: it gates a deletion. Both must contain the same
    meaningful words, ignoring the subject ("I drink" vs "User drinks"), filler
    and plural endings. A character-similarity ratio is NOT safe here: "editor
    is Zed" and "editor is VS Code" differ by one short word and would match.
    """
    a, b = _fact_words(stored), _fact_words(old_text)
    return bool(a) and a == b


# ---------------------------------------------------------------------------
# User-facing error messages
# ---------------------------------------------------------------------------

AUTH_ERROR = "auth"
CREDITS_ERROR = "credits"
INVALID_ERROR = "invalid"  # the request itself is wrong; retrying can never succeed

_ERROR_MESSAGES = {
    "VityAuthError": (
        AUTH_ERROR,
        "Maximem rejected the API key, so Vity memory is unavailable. "
        "Fix it with: hermes-maximem-vity install --api-key mx_...",
    ),
    "VityInsufficientCreditsError": (
        CREDITS_ERROR,
        "The Maximem account is out of credits, so Vity memory is unavailable. "
        "Add credits at https://app.maximem.ai",
    ),
    "VityRateLimitError": ("rate_limit", "Vity memory is rate-limited right now. Retry shortly."),
    "VityUnavailableError": ("unavailable", "The Vity memory service is unreachable right now."),
}


def classify_error(exc: BaseException) -> Tuple[str, str]:
    """``(kind, user-facing message)`` for an SDK error. Matched by class name so
    this module needs no SDK import."""
    for cls in type(exc).__mro__:
        if cls.__name__ == "VityInvalidInputError":
            return INVALID_ERROR, str(exc)
        if cls.__name__ in _ERROR_MESSAGES:
            return _ERROR_MESSAGES[cls.__name__]
    return "other", str(exc)


# ---------------------------------------------------------------------------
# Writes that could not be delivered
# ---------------------------------------------------------------------------
# A capture or store that still fails after a retry (outage, process exit) is
# appended here and replayed on the next start, so a fact the user stated is
# not silently lost. The file lives in HERMES_HOME, is owner-only, and is
# bounded so a long outage can't grow it without limit.

PENDING_WRITES_FILE = "vity_pending_writes.jsonl"
PENDING_WRITES_MAX = 200
_LOCK_WAIT_S = 5.0
_LOCK_STALE_S = 30.0  # a lock older than this was left by a crashed process


@contextmanager
def _file_lock(path: Path):
    """Cross-process lock beside ``path`` (atomic O_EXCL create; works on Windows).

    Several Hermes processes (CLI, gateway) share one pending file. Without the
    lock, a write landing while another process takes the file can be lost.
    A stale lock (crashed holder) is broken after ``_LOCK_STALE_S``; if the lock
    still can't be had within ``_LOCK_WAIT_S`` we proceed without it rather
    than block shutdown (best-effort buffer).
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + _LOCK_WAIT_S
    acquired = False
    while not acquired:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            acquired = True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
    try:
        yield
    finally:
        if acquired:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass


def save_pending_writes(path: Path, jobs: List[Dict[str, Any]]) -> None:
    """Append ``jobs`` to the pending file (owner-only), keeping the newest
    ``PENDING_WRITES_MAX`` entries."""
    if not jobs:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        kept = (load_pending_writes(path) + list(jobs))[-PENDING_WRITES_MAX:]
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(job) + "\n" for job in kept))
        os.replace(tmp, path)


def load_pending_writes(path: Path) -> List[Dict[str, Any]]:
    """Read a pending file, skipping any corrupt line."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    jobs = []
    for line in text.splitlines():
        try:
            job = json.loads(line)
        except ValueError:
            continue
        if isinstance(job, dict) and job.get("kind"):
            jobs.append(job)
    return jobs


def take_pending_writes(path: Path) -> List[Dict[str, Any]]:
    """Read and remove the pending writes (the caller re-queues them).

    Under the lock, so when several Hermes processes start together exactly
    one of them gets each write and none is delivered twice.
    """
    with _file_lock(path):
        jobs = load_pending_writes(path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return jobs[-PENDING_WRITES_MAX:]
