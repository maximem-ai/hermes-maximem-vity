# Changelog

## 1.3.0

### Added

- `vity_knowledge` tool: topic-level knowledge the user has built up, complementary to `vity_recall`.
- `vity_meeting_recall` tool, offered automatically when Meeting Memory is enabled for the account.
- Session-start profile: a short "who is this user" summary is loaded in the background and injected once per session (`profile_warmup`).
- Follow-up resolution: short follow-ups such as "why?" or "tell me more" are searched together with the previous message.
- `recall_min_score` (default `0.3`) keeps weak matches out of auto-recall, and `recall_deadline` (default `3.0` seconds) caps the total pre-turn recall time.
- Anonymous usage telemetry, sent through the Maximem API. Opt out with `MAXIMEM_ANALYTICS=0`.

### Changed

- Requires `maximem-vity-sdk` 0.3.0 or newer. `hermes-maximem-vity install` upgrades an older SDK in the Hermes environment automatically.
- Conversations are captured with a stable per-session identity, so the conversation summaries for a session stay together.
- A failed capture is retried once before it is dropped.
