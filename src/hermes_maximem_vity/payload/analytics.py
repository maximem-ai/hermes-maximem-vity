"""Fire-and-forget telemetry for the Vity plugin, stdlib only, never fatal.

Events are sent to the Maximem telemetry endpoint (the same router the plugin
already uses for memory calls), authenticated with the user's own Maximem API
key. The PostHog project key lives ONLY on the server, which forwards events to
PostHog. Nothing analytics-related ships to a user machine beyond the user's own
key, which is already there for memory calls.

Every send is best-effort: telemetry must NEVER slow down a turn, raise into the
agent, or leak memory content. We emit only an event name plus a small ALLOWLIST
of metadata (latency, counts, config flags, outcome), never prompts, queries,
memory content, responses, or the raw API key in the body.

Config (env vars):
  MAXIMEM_ENDPOINT   Maximem base URL (default https://agenticrouter-prod.maximem.ai).
                     MAXIMEM_API_URL is also accepted.
  MAXIMEM_ANALYTICS  Set to 0/false/off/no to opt out entirely.

distinct_id is sha256(api_key)[:16], stable per Maximem account and never the key
itself. Without an API key we cannot authenticate to the endpoint, so the event
is skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.request

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "1.4.0"

# Telemetry is proxied through the Maximem router, which holds the PostHog key
# server-side. No project key is shipped to user machines.
_DEFAULT_ENDPOINT = "https://agenticrouter-prod.maximem.ai"
_TELEMETRY_PATH = "/v1/telemetry"

# Read env at call time (not import time) so tests and per-process config work.


def _endpoint() -> str:
    return (
        os.environ.get("MAXIMEM_ENDPOINT")
        or os.environ.get("MAXIMEM_API_URL")
        or _DEFAULT_ENDPOINT
    ).rstrip("/")


def _opted_out() -> bool:
    return os.environ.get("MAXIMEM_ANALYTICS", "").strip().lower() in {"0", "false", "off", "no"}


def enabled() -> bool:
    """True unless the user opted out.

    An API key is still required at send time to authenticate to the endpoint;
    ``track`` skips when it is absent.
    """
    return not _opted_out()


def distinct_id(api_key: str) -> str:
    """Stable, non-reversible per-account id. Never the raw key."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else "anon"


def _send(url: str, api_key: str, payload: bytes) -> None:
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                # The user's own Maximem key authenticates the event, exactly as
                # it does for memory calls. It travels in the header, never the body.
                "Authorization": f"Bearer {api_key}",
                # The API edge rejects urllib's default "Python-urllib/x.y"
                # agent with 403, which would silently drop every event.
                "User-Agent": f"hermes-maximem-vity/{PLUGIN_VERSION}",
            },
        )
        urllib.request.urlopen(req, timeout=3).close()  # noqa: S310 (fixed host)
    except Exception as e:  # telemetry must never affect the user
        logger.debug("vity telemetry send failed (non-fatal): %s", e)


def track(event: str, api_key: str = "", props: dict | None = None, block: bool = False) -> None:
    """Send one telemetry event to the Maximem endpoint. Best-effort, never raises.

    Requires the user's Maximem API key to authenticate; without it the event is
    skipped (an unauthenticated event cannot be attributed and would be rejected).

    ``block=True`` sends inline, for short-lived processes (the installer) that
    exit before a daemon thread would flush. The default fires on a daemon thread
    so it never sits on the agent's critical path.
    """
    if not enabled() or not api_key:
        return
    url = f"{_endpoint()}{_TELEMETRY_PATH}"
    body = json.dumps({
        "event": event,
        "distinct_id": distinct_id(api_key),
        "properties": {
            "plugin_version": PLUGIN_VERSION,
            "$lib": "hermes-maximem-vity",
            **(props or {}),
        },
    }).encode()
    if block:
        _send(url, api_key, body)
    else:
        threading.Thread(
            target=_send, args=(url, api_key, body), daemon=True, name="vity-telemetry"
        ).start()
