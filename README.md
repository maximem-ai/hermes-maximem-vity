# Vity memory for Hermes Agent

`hermes-maximem-vity` is a plugin that gives [Hermes Agent](https://github.com/NousResearch/hermes-agent) long-term memory across sessions, using Vity by Maximem AI.

## What you get

- Before each turn, the plugin searches your memories and gives the agent the ones that match your message.
- After each turn, the plugin saves the exchange so Vity can extract facts and preferences from it.
- At the start of a session, the agent receives a short Profile Card: who you are, what you do and what you care about.
- The agent can bring in recent work from your other AI apps (ChatGPT, Claude) when you continue it in Hermes.
- The agent has tools to search, save and delete memories, and you have a command line for the same tasks.
- If the memory service is slow or down, the turn continues without memory. It does not wait.

## Requirements

- Hermes Agent, with the `hermes` command on your `PATH`.
- Python 3.10 or newer for the `pip install` step.
- A Maximem account. The installer signs you in with your browser and creates an API key for Hermes. You can also paste a key from [app.maximem.ai/api-keys](https://app.maximem.ai/api-keys). It starts with `mx_`.
- `maximem-vity-sdk` 0.3.1 or newer (below 1.0). The installer puts it in the environment that Hermes runs in. You do not install it by hand.

## Install

```bash
pip install hermes-maximem-vity
hermes-maximem-vity install
```

The `install` command does these steps:

1. Copies the plugin files into `$HERMES_HOME/plugins/maximem_vity/`. The default `$HERMES_HOME` is `~/.hermes`, or `%LOCALAPPDATA%\hermes` on Windows. The file `provider.py` is installed as `__init__.py`, which is the name Hermes loads.
2. Removes an old `plugins/vity` folder from earlier versions, if it exists.
3. Installs `maximem-vity-sdk` into the Python environment that Hermes uses. If a compatible SDK is already there, it does nothing.
4. Gets your API key, by browser sign-in or one you paste, and saves it as `MAXIMEM_API_KEY` in `$HERMES_HOME/.env`, without duplicate lines.
5. Runs `hermes config set memory.provider maximem_vity` to turn the provider on.

When all three of SDK, key and activation succeed, it prints a message that starts with `All set`. If one fails, it prints the exact command to finish that step by hand.

### Set your API key

On a terminal, the installer asks how to get the key:

- **Press Enter** to sign in with your browser. The terminal shows a confirmation code; type it on the sign-in page and approve. The installer creates a key named "Hermes (Vity setup, <computer name>)" and saves it. Signing in again on the same computer replaces that key, so old ones don't pile up.
- **Paste a key** you created at app.maximem.ai/api-keys.
- Press Ctrl+C to skip.

To sign in again later and replace the key, or to sign in when a key is already saved:

```bash
hermes-maximem-vity install --login
```

In a script, pass a key as a flag:

```bash
hermes-maximem-vity install --api-key mx_...
```

A plain re-install keeps the key you already have. You can also edit `$HERMES_HOME/.env` yourself and add the line `MAXIMEM_API_KEY=mx_...`. The key decides which memory space is used. Use a separate key for each account that needs separate memories.

If Hermes or the gateway was running during the install, restart it. Gateway users run `hermes gateway restart`.

### Install with pipx or a virtual environment

If `pip install` fails with `externally-managed-environment` (PEP 668), or `pip` is not found, use pipx or a virtual environment:

```bash
pipx install hermes-maximem-vity
hermes-maximem-vity install
```

The Python that runs `pip` does not need to match the Python that runs Hermes.

## Check that it works

```bash
hermes-maximem-vity status      # plugin files installed, SDK usable by Hermes
hermes memory status            # shows maximem_vity as the provider and a live connection check
hermes maximem_vity status      # key, SDK, settings and connection
```

Then save a memory and find it again:

```bash
hermes maximem_vity store "I prefer dark mode" --type preference
hermes maximem_vity search "dark mode"
```

New facts that come from captured conversations are extracted on the server after the turn is accepted. They may not be searchable right away.

## How it works

### Recall before each turn

The plugin searches your stored memories using the current message and injects up to 10 matches into the prompt under the heading "Vity Memory".

- Only matches with a relevance of at least `recall_min_score` are injected, so weak matches are not presented as facts.
- Each line shows its age and relevance, for example `- [3d ago · 82%] User prefers dark mode`.
- The block is limited to about `max_recall_tokens` tokens (about 4 characters per token). Whole lines are kept, except that a single memory that is too long is cut at the limit.
- Messages shorter than `min_prompt_length` characters are not searched. The first message of a session is always searched.
- A short follow-up such as "why?" or "tell me more" has no subject of its own. The plugin adds your last full message to the search. A message that stands on its own is searched as written.
- If the search takes longer than `recall_deadline` seconds, the turn continues without memory.
- After three failed recalls in a row, auto-recall pauses for 60 seconds.
- If the API key is rejected or the account is out of credits, auto-recall stops until Hermes restarts. The agent is told once that memory is unavailable, so it does not claim to know nothing about you.
- Hermes shows a "recalled N memories" line when it supports recall indicators. On an older Hermes the line is not shown.

### Capture after each turn

When `auto_capture` is on, each user and assistant exchange goes into a queue and is sent to Vity in the background. The reply is never held up by it.

- Capture does not run in automated contexts: cron jobs, subagents and Hermes' own flush runs. This keeps automated text out of your memory.
- Hermes system notices, such as context-compaction summaries and background-task results, are not saved as if you said them.
- Each captured message keeps the time Hermes gives it. Without one, the time the turn was queued is used.
- When the agent's built-in memory tool adds a fact, the plugin also stores it in Vity. When the built-in tool replaces or removes a fact, the plugin removes the old copy from Vity only if it is an exact match for the old text. Anything less certain is left in place. Mirroring is also off when `auto_capture` is off.
- With `auto_capture` on, the agent calls `vity_store` only when you ask it to remember something, because every turn is already captured. With `auto_capture` off, the agent stores new facts itself.

A write that fails is retried once. If it still fails, or it is still queued when Hermes exits, a captured turn or stored fact is saved to `$HERMES_HOME/vity_pending_writes.jsonl` and sent on the next start. The file is readable only by you and holds at most 200 entries. Writes rejected as invalid, and writes rejected because of a bad API key, are dropped and not saved.

### Profile Card at session start

When `profile_warmup` is on, the plugin reads your Profile Card in the background and injects it once per session as background context under "About the user".

- The card can include name, work (role and company), location, education, expertise, domains, skills, interests, active goals and preferences.
- Only values with a confidence of 0.6 or higher are included, plus values you edited yourself.
- Your email address and speculative or ended values are never included.
- The injected block has at most 5 values per field and about 1,200 characters. A line that does not fit is dropped whole.
- A new account may not have a card yet. In that case nothing is injected, and the next session tries again.
- The card is kept in memory for one hour, so sessions started soon after each other reuse it.
- After Hermes compresses a long conversation, or after a reset or rewind, the card is injected again.

The profile is loaded only when `auto_recall` is on. The `vity_profile` tool works either way.

### Live context from your other AI apps

When `live_context` is on, the plugin asks Vity on every turn whether your message continues work you did in another AI app, such as ChatGPT or Claude. Vity decides which turns qualify: the first message of a session, and any message that names an app, for example "continue my ChatGPT chat". Other turns get nothing back.

- A strong match on the first message injects that chat under "Live work from the user's other app". The normal memory search is skipped for that turn.
- A weaker match is injected next to the normal memory results.
- The injected text is marked as data, not instructions.
- The check has its own limit of 3 seconds, or `recall_deadline` if that is lower. On a timeout or error, the normal search runs.
- It is skipped when auto-recall is off, paused, or the key was rejected.
- If the SDK in Hermes does not support live context, the check is skipped and everything else works.

### Dates and ages

A memory's age counts from when it was said, not from when Vity stored it. A chat you imported today shows its real age. If the SDK does not provide the original time, the stored time is used.

Ages are shown as `just now`, `5m ago`, `3h ago`, `3d ago`, `2mo ago` (30-day months) and `1y ago`. The `vity_recall` tool returns the same value as `age`, and the relevance as a number from 0 to 1.

## Settings

Settings are optional. Put them in `$HERMES_HOME/vity.json`, as a JSON object:

```json
{
  "auto_recall": true,
  "auto_capture": true,
  "max_recall_tokens": 1000,
  "min_prompt_length": 5,
  "recall_timeout": 6.0,
  "recall_deadline": 3.0,
  "recall_min_score": 0.3,
  "profile_warmup": true,
  "live_context": true
}
```

The installer does not create this file where the plugin reads it. It only puts a copy of `vity.json.example` inside the plugin folder, and the plugin does not read that copy. To change settings, create `$HERMES_HOME/vity.json` yourself (copy the example), or use `hermes memory setup`. An invalid file is ignored, and a warning is written to the Hermes log. Restart Hermes after changes.

| Key | Default | Meaning |
| --- | --- | --- |
| `auto_recall` | `true` | Inject memories before each turn. |
| `auto_capture` | `true` | Save each turn after it finishes. Also controls mirroring of the built-in memory tool. |
| `max_recall_tokens` | `1000` | Size limit for the injected memory block. |
| `min_prompt_length` | `5` | Do not search messages shorter than this many characters. The first message is exempt. |
| `recall_timeout` | `6.0` | Seconds before one recall request is abandoned. |
| `recall_deadline` | `3.0` | Seconds the whole pre-turn recall may take. With the defaults this limit is reached first. |
| `recall_min_score` | `0.3` | Minimum relevance (0 to 1) for an injected memory. The `vity_recall` tool does not apply it. |
| `profile_warmup` | `true` | Inject the Profile Card once per session. Needs `auto_recall`. |
| `live_context` | `true` | Bring in live work from other AI apps. |
| `endpoint` | empty | Use a different Maximem API URL, for a self-hosted backend. Empty means the SDK default. |

A value of `null` or an empty string is ignored and the default is used. `hermes memory setup` offers `auto_recall`, `auto_capture`, `profile_warmup`, `live_context`, `recall_min_score`, `recall_deadline` and `max_recall_tokens`, and then tests the connection.

### Environment variables

Set these in `$HERMES_HOME/.env` or in the shell.

| Variable | Meaning |
| --- | --- |
| `MAXIMEM_API_KEY` | Your API key (`mx_...`). Required. `VITY_API_KEY` is also accepted. |
| `MAXIMEM_ENDPOINT` | Maximem API URL. `MAXIMEM_API_URL` is also accepted. `endpoint` in `vity.json` takes priority. |
| `MAXIMEM_ANALYTICS` | Set to `0`, `false`, `off` or `no` to turn off telemetry. |
| `HERMES_HOME` | Where Hermes keeps its files. Read by the installer. Default `~/.hermes`. |

## Tools the agent can call

You do not need special commands. Ask the agent to "remember that ..." and it calls `vity_store`. Ask "what do you know about ..." and it calls `vity_recall` or `vity_knowledge`.

| Tool | Parameters | What it does |
| --- | --- | --- |
| `vity_recall` | `query` (required), `top_k` (default 10, max 50) | Finds specific stored memories. Returns each memory with its age and relevance. If search finds nothing, it falls back to a profile-based recall. |
| `vity_knowledge` | `query` (required), `limit` (default 10, max 100) | Returns topic-level knowledge built from many memories, one item per topic. |
| `vity_profile` | none | Returns your Profile Card, up to 20 values per field. |
| `vity_store` | `content` (required), `memory_type` (`fact`, `preference`, `emotion`, `episode`, `knowledge`, `profile`; default `fact`) | Saves one memory. |
| `vity_forget` | `query` (required), `dry_run` (default `true`) | Deletes memories that match a query. With `dry_run` true it only reports how many would be deleted. |
| `vity_meeting_recall` | `question` (required), `meeting_type`, `status` | Answers questions about your meetings, decisions and attendees. Needs Meeting Memory on your account. Otherwise it says so. |

Use `vity_recall` for a specific fact you stated. Use `vity_knowledge` for what you know about a topic.

`vity_meeting_recall` is always listed, because Hermes reads the tool list once at startup. The plugin checks whether Meeting Memory is enabled when the tool is called.

The code also contains `vity_update` and `vity_delete`, which edit or delete one memory by id. They are switched off in this release and the agent is not offered them.

## Commands

The `hermes maximem_vity` commands are available while `maximem_vity` is the active memory provider.

| Command | What it does |
| --- | --- |
| `hermes maximem_vity status` | Shows key, SDK, endpoint and main settings, then checks the connection. Running `hermes maximem_vity` alone does the same. |
| `hermes maximem_vity search "<query>"` | Semantic search. Options: `--limit N` (default 10), `--json` for raw output. |
| `hermes maximem_vity store "<text>"` | Saves a memory. Option: `--type` (same values as `vity_store`, default `fact`). |
| `hermes maximem_vity forget "<query>"` | Shows how many memories match. Add `--yes` to delete them. |

The installer command has its own subcommands:

| Command | What it does |
| --- | --- |
| `hermes-maximem-vity install` | Installs and activates the plugin. Options: `--login` (browser sign-in) or `--api-key mx_...`. |
| `hermes-maximem-vity status` | Shows whether the plugin files are installed and whether Hermes can use the SDK. |
| `hermes-maximem-vity uninstall` | Deletes the plugin folder. It does not change `memory.provider` or `$HERMES_HOME/.env`. |

To turn the provider on again, run:

```bash
hermes config set memory.provider maximem_vity
```

Use this instead of choosing the provider in the `hermes memory setup` wizard. Pasted or buffered input can make the wizard drop the choice and leave the provider unset.

## Privacy and telemetry

What the plugin sends to the Maximem API, using your API key:

- The text of your message, for the recall search and the live-context check.
- Each captured user and assistant message, and facts you or the agent store.
- Delete requests, and the text used to find a memory to delete.

The API key decides which memory space is used.

Usage telemetry is separate. It is on by default and is sent to the same Maximem API address as your memory calls. It never contains message text, queries, responses or memory content. Each event carries the plugin version and an account identifier. The identifier is the first 16 characters of a SHA-256 hash of your API key, not the key. The key travels only in the request header.

| Event | Fields |
| --- | --- |
| `plugin_installed` | Whether the SDK step, key step and activation step succeeded. |
| `provider_initialized` | `auto_recall`, `auto_capture`, whether a custom endpoint is used, whether writes are enabled, the agent context. |
| `recall_performed` | Duration, whether anything was found, number of memories, first-turn flag, paused flag, failed flag, whether the profile was injected and how many fields, the live-context result (`hit`, `soft`, `named` or other). |
| `capture_result` | `accepted`, `retry_accepted` or `failed`, and the error type for a failure. |
| `tool_called` | Tool name and duration. |

Events are sent from a background thread with a 3-second timeout. A failure is ignored and never affects a turn. Without an API key, no event is sent.

To turn telemetry off, add this line to `$HERMES_HOME/.env` and restart Hermes:

```
MAXIMEM_ANALYTICS=0
```

The installer reads only the shell environment for this setting. To skip the `plugin_installed` event, run `MAXIMEM_ANALYTICS=0 hermes-maximem-vity install`.

## Troubleshooting

| Symptom or message | Cause and fix |
| --- | --- |
| `maximem-vity-sdk is not installed in Hermes' environment` | Hermes runs in its own environment, which is usually not the one `pip` used. Run `hermes-maximem-vity install`, then `hermes-maximem-vity status`. If it cannot find Hermes, it prints the `pip install` command to run with the Hermes interpreter. |
| `SDK: missing or older than 0.3.1` | An old SDK is installed in Hermes' environment. Run `hermes-maximem-vity install`. |
| `The Profile Card needs maximem-vity-sdk 0.3.1 or newer` | A partial upgrade left an old SDK in Hermes. Run `hermes-maximem-vity install`. Recall and capture keep working. |
| `MAXIMEM_API_KEY is not set` | No key was found. Run `hermes-maximem-vity install --login`. |
| `Maximem rejected the API key` | The key is wrong or revoked. Run `hermes-maximem-vity install --login` (or `--api-key mx_...` with a valid key) and restart Hermes. |
| `The Maximem account is out of credits` | Add credits at [app.maximem.ai](https://app.maximem.ai), then restart Hermes. |
| `Vity memory is rate-limited right now` or `The Vity memory service is unreachable right now` | A temporary service problem. Recall skips the turn, and capture retries and then saves the write for the next start. |
| No `recalled N memories` line | Your Hermes may not support recall indicators, or nothing matched. Check `recall_min_score`. |
| Memory seems to stop for a minute | Three recalls failed in a row, so auto-recall paused for 60 seconds. |
| `Meeting Memory is not enabled for this Maximem account` | The account does not have Meeting Memory. Enable it at [app.maximem.ai](https://app.maximem.ai). |
| `No profile yet` | The Profile Card is built from your conversations. A new account has none. |
| A setting in `vity.json` has no effect | The file must be at `$HERMES_HOME/vity.json`, not in the plugin folder. Check the Hermes log for an invalid JSON warning. Restart Hermes. |
| `hermes-maximem-vity` is not recognized (Windows) | The Python `Scripts` folder is not on `PATH`. See below. |

On Windows, `pip install --user` puts the command in a `Scripts` folder that may not be on `PATH`. For the current PowerShell session:

```powershell
$env:Path += ";$(python -m site --user-base)\Python$(python -c 'import sys;print(f"{sys.version_info.major}{sys.version_info.minor}")')\Scripts"
hermes-maximem-vity install
```

To keep the change, add that `Scripts` folder to your user `PATH` in Environment Variables.

## Upgrade

```bash
pip install -U hermes-maximem-vity
hermes-maximem-vity install
```

Run `install` again after every upgrade. It refreshes the plugin files that Hermes loads and upgrades the SDK in Hermes' environment when needed. Your API key is kept. Restart Hermes afterwards.

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest -q
```

The tests run offline. They replace the Hermes modules (`agent.memory_provider`, `tools.registry`, `hermes_constants`, `utils`) with stubs, so a Hermes checkout is not needed. See `tests/conftest.py`.

| Path | Purpose |
| --- | --- |
| `src/hermes_maximem_vity/installer.py` | The `hermes-maximem-vity` command (`install`, `uninstall`, `status`). |
| `src/hermes_maximem_vity/payload/provider.py` | The memory provider and tool definitions. Installed as `__init__.py`. |
| `src/hermes_maximem_vity/payload/vity_support.py` | Helpers: Profile Card rendering, age labels, follow-up detection, fact matching, the pending-writes file. |
| `src/hermes_maximem_vity/payload/cli.py` | The `hermes maximem_vity` commands. |
| `src/hermes_maximem_vity/payload/analytics.py` | Usage telemetry. |
| `src/hermes_maximem_vity/payload/plugin.yaml` | Plugin manifest: dependencies and required environment. |
| `src/hermes_maximem_vity/payload/vity.json.example` | Template for `vity.json`. |
| `tests/` | Unit tests and host stubs. |

See the [changelog](https://github.com/maximem-ai/hermes-maximem-vity/blob/main/CHANGELOG.md) for release notes.

## License

MIT. See [LICENSE](https://github.com/maximem-ai/hermes-maximem-vity/blob/main/LICENSE).

## More information

- Documentation: https://docs.maximem.ai/vity
- API keys: https://app.maximem.ai/api-keys
