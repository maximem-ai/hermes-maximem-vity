"""Telemetry helper: proxy-through-router model, hashed identity, no leakage.

The invariants that must never regress:
  1. No PostHog project key ships in the client (it lives on the server).
  2. Events go to the Maximem telemetry endpoint, not PostHog directly.
  3. The raw API key never appears in the request BODY (it authenticates via the
     Authorization header, exactly like memory calls).
  4. No prompts / queries / memory content are ever sent.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ANALYTICS = (
    Path(__file__).resolve().parent.parent
    / "src" / "hermes_maximem_vity" / "payload" / "analytics.py"
)


@pytest.fixture
def analytics(monkeypatch):
    """Load analytics.py standalone (as it runs beside the installed provider)."""
    monkeypatch.delenv("MAXIMEM_ENDPOINT", raising=False)
    monkeypatch.delenv("MAXIMEM_API_URL", raising=False)
    monkeypatch.delenv("MAXIMEM_ANALYTICS", raising=False)
    spec = importlib.util.spec_from_file_location("vity_analytics_test", _ANALYTICS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_posthog_key_shipped_in_client(analytics):
    # The whole point: no baked-in PostHog project key on the client anymore.
    assert not hasattr(analytics, "_DEFAULT_KEY")
    source = _ANALYTICS.read_text()
    assert "phc_" not in source


def test_skips_without_api_key(analytics, monkeypatch):
    # No key => cannot authenticate to the endpoint => nothing leaves the process.
    sent = []
    monkeypatch.setattr(analytics, "_send", lambda url, key, body: sent.append((url, key, body)))
    analytics.track("recall_performed", api_key="", props={"retrieved": True})
    assert sent == []


def test_opt_out_disables(analytics, monkeypatch):
    monkeypatch.setenv("MAXIMEM_ANALYTICS", "off")
    assert analytics.enabled() is False
    sent = []
    monkeypatch.setattr(analytics, "_send", lambda url, key, body: sent.append((url, key, body)))
    analytics.track("recall_performed", api_key="mx_secret", props={"retrieved": True})
    assert sent == []


def test_distinct_id_hashes_the_key(analytics):
    did = analytics.distinct_id("mx_super_secret_key")
    assert "mx_super_secret_key" not in did
    assert did != "mx_super_secret_key"
    assert len(did) == 16
    assert analytics.distinct_id("") == "anon"


def test_sends_to_router_telemetry_not_posthog(analytics, monkeypatch):
    sent = []
    monkeypatch.setattr(analytics, "_send", lambda url, key, body: sent.append((url, key, body)))
    analytics.track("recall_performed", api_key="mx_key", props={"retrieved": True}, block=True)

    assert len(sent) == 1
    url, key, _ = sent[0]
    assert url == "https://agenticrouter-prod.maximem.ai/v1/telemetry"
    assert "posthog" not in url
    assert key == "mx_key"   # passed to _send to become the Authorization header


def test_respects_custom_endpoint(analytics, monkeypatch):
    monkeypatch.setenv("MAXIMEM_ENDPOINT", "https://staging.maximem.ai/")
    sent = []
    monkeypatch.setattr(analytics, "_send", lambda url, key, body: sent.append(url))
    analytics.track("x", api_key="mx_key", block=True)
    assert sent == ["https://staging.maximem.ai/v1/telemetry"]


def test_body_never_contains_key_or_phc(analytics, monkeypatch):
    sent = []
    monkeypatch.setattr(analytics, "_send", lambda url, key, body: sent.append((key, body)))
    analytics.track(
        "recall_performed",
        api_key="mx_super_secret_key",
        props={"duration_ms": 1200, "retrieved": True, "cold_start": False},
        block=True,
    )

    assert len(sent) == 1
    key, raw = sent[0]
    assert b"mx_super_secret_key" not in raw   # raw key never in the body
    assert b"phc_" not in raw                  # no PostHog key anywhere

    body = json.loads(raw)
    assert body["event"] == "recall_performed"
    assert "api_key" not in body               # no key field at all in the body
    assert body["distinct_id"] == analytics.distinct_id("mx_super_secret_key")
    assert body["properties"]["retrieved"] is True
    assert body["properties"]["plugin_version"]
