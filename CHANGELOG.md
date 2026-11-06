# Changelog

## 1.4.0 (unreleased)

### Changed

- The session-start profile and the `vity_profile` tool now use the user's **Profile Card**: name, role and company, location, education, expertise, skills, interests, active goals and preferences. It is read directly from the user's resolved profile, with no memory search or LLM call.
- Only confident, current values are included. The email address and speculative values are never injected, and the block is never cut mid-item.
- Requires `maximem-vity-sdk` 0.3.1 or newer. `hermes-maximem-vity install` upgrades an older SDK automatically.

### Added

- Hermes' recall indicator ("recalled N memories") on every platform, replacing the terminal-only timing line.
- Recalled memories are labelled with their age and relevance, for example `[3d ago · 82%]`.
- When the API key is rejected or credits run out, the agent is told once that memory is unavailable, and tool errors name the real cause.
- Auto-recall pauses for a minute after three failed recalls in a row, so an outage does not slow every turn.
- Writes that fail after a retry, or are still queued at exit, are kept in `$HERMES_HOME/vity_pending_writes.jsonl` and delivered on the next start. The file is owner-only, bounded to 200 entries, and safe to share between several Hermes processes.
- Mirroring of the built-in memory tool's `replace` and `remove`. The new text is always stored; the old copy in Vity is deleted only when the deletion would affect that memory and nothing else, otherwise it is left in place.
- `hermes memory status` shows a live connection check. If you use `hermes memory setup`, it now offers the recall settings and tests the connection. A missing API key or SDK is reported with the command that fixes it.
- `profile_fields` on the `recall_performed` telemetry event: how many Profile Card fields were injected.

### Fixed

- `vity_meeting_recall` could never be called inside Hermes: it was added after Hermes had already built its tool routing. It is now always registered and reports when Meeting Memory is not enabled.
- The Profile Card was lost after Hermes compressed a long conversation; it is now injected again.
- Capturing a turn could hold up Hermes' memory worker for up to 5 seconds; all writes now go through one non-blocking queue.
- A fact saved with Hermes' built-in memory just before Hermes exited (for example a one-shot `hermes chat -q`) was never mirrored into Vity. Shutdown now waits for in-flight writes (bounded), and an exit hook covers exits that skip the host's shutdown.
- Hermes' own system notices (for example compaction summaries) were saved as if the user had said them.
- Line breaks inside stored text could add extra lines to the injected memory block; stored text is now always rendered on one line.
- A write the API rejects as invalid (for example a fact over 10,000 characters) is dropped with a warning instead of being retried on every start.
- With an outdated SDK in Hermes' environment, `vity_profile` showed a raw error; it now names the fix, and recall and capture keep working.
- `hermes maximem_vity` commands print a one-line error on API failures instead of a traceback.
- An invalid `vity.json` is reported in the log instead of being silently ignored.
- `vity_forget` now requires a query.
- With auto-capture on, the agent was told to call `vity_store` for every fact the user shared, although every turn is already captured. That cost a tool call per turn and saved facts twice. It now stores only when the user asks it to remember something; with auto-capture off the rule is unchanged.

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
