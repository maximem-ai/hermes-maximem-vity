# Vity Memory for Hermes Agent

Vity by Maximem AI provides persistent, cross-session semantic memory for [Hermes Agent](https://github.com/NousResearch/hermes-agent), distributed as a standalone plugin.

Vity gives the agent a long-term memory graph covering facts, preferences, episodes, knowledge, and profile. It recalls relevant context before each turn and captures the conversation after each turn, so the agent remembers users and projects across sessions.

Built on the [`maximem-vity-sdk`](https://pypi.org/project/maximem-vity-sdk/) Python client (0.3.0 or newer).

## Install

First, get an API key at [app.maximem.ai/api-keys](https://app.maximem.ai/api-keys). It starts with `mx_`. Then:

```bash
pip install hermes-maximem-vity
hermes-maximem-vity install
```

`hermes-maximem-vity install` does everything in one step:

1. Copies the plugin into `~/.hermes/plugins/maximem_vity/`, where Hermes discovers it.
2. Prompts for your API key and saves it to `~/.hermes/.env` without duplicates.
3. Activates the provider by setting `memory.provider: maximem_vity`.

It prints a confirmation when finished. Start the agent with `hermes`.

If Hermes or the gateway was already running during install, restart it to load the newly activated provider. Gateway users run `hermes gateway restart`.

For non-interactive or scripted installs, pass the key as a flag to skip the prompt:

```bash
hermes-maximem-vity install --api-key mx_your_key
```

<details><summary>Hitting a PEP 668 "externally-managed-environment" or "pip: command not found" error?</summary>

System Python (for example Homebrew) blocks global `pip install`. Use pipx (recommended) or a virtual environment:

```bash
pipx install hermes-maximem-vity        # install pipx first if needed: brew install pipx
hermes-maximem-vity install
```

You do not need to match Python versions. The `maximem-vity-sdk` dependency is installed into the Hermes environment automatically.

</details>

### Verify

```bash
hermes-maximem-vity status   # plugin installed, SDK available to Hermes
hermes memory status         # shows maximem_vity as active
hermes maximem_vity status   # API key set, SDK installed, connection ok
```

### Update

```bash
pip install -U hermes-maximem-vity
hermes-maximem-vity install
```

### Remove

```bash
hermes-maximem-vity uninstall
```

### Troubleshooting

**`hermes maximem_vity status` still reports `maximem-vity-sdk not installed`, even after `pip install`.**

Hermes runs inside its own isolated environment, which is usually not the Python that ran `pip install` (such as Anaconda or system Python). The SDK must live in the Hermes environment, and `hermes-maximem-vity install` puts it there. Re-run it:

```bash
hermes-maximem-vity install
hermes-maximem-vity status     # confirm the SDK is available to Hermes
```

**Windows: `hermes-maximem-vity` is not recognized as a command.**

`pip install --user` on Windows places the console script in a `Scripts` folder that may not be on `PATH`. Add it for the session in PowerShell, then run install:

```powershell
$env:Path += ";$(python -m site --user-base)\Python$($(python -c 'import sys;print(f"{sys.version_info.major}{sys.version_info.minor}")'))\Scripts"
hermes-maximem-vity install
```

Alternatively, call it by full path once: `& "$(python -m site --user-base)\Python313\Scripts\hermes-maximem-vity.exe" install`. To make it permanent, add that `Scripts` folder to your user `PATH` in Environment Variables.

## Configuration

API key (secret, stored in `~/.hermes/.env`):

| Env var | Required | Description |
| --- | --- | --- |
| `MAXIMEM_API_KEY` | Yes | Your Maximem API key (`mx_...`). `VITY_API_KEY` is also accepted. |

The API key defines the memory space. Use a separate key for each account that needs isolated memories.

To change the key, a plain re-install keeps the existing one. To replace it, pass the new key explicitly (it is written de-duplicated, so no stale copies remain):

```bash
hermes-maximem-vity install --api-key mx_your_new_key
```

You can also edit `~/.hermes/.env` directly, then restart Hermes (gateway users run `hermes gateway restart`).

Tunables (optional, non-secret, stored in `$HERMES_HOME/vity.json`, created on install):

| Key | Default | Description |
| --- | --- | --- |
| `auto_recall` | `true` | Inject relevant memories before each turn. |
| `auto_capture` | `true` | Capture the conversation after each turn. |
| `max_recall_tokens` | `1000` | Size cap for the injected recall context. |
| `min_prompt_length` | `5` | Skip recall for very short prompts. |
| `recall_timeout` | `6.0` | Maximum seconds to wait for pre-turn recall. |
| `recall_min_score` | `0.3` | Minimum relevance for an auto-injected memory. |
| `profile_warmup` | `true` | Load a short user profile at session start and inject it once. |

For a self-hosted backend, set `MAXIMEM_ENDPOINT`, or `endpoint` in `vity.json`, to a non-default Maximem API URL.

### Usage telemetry

The plugin sends anonymous usage events (install, session start, recall latency and hit/miss, tool name and latency, capture outcome) to the Maximem API, authenticated with your API key. Events never include prompts, queries, responses or memory content. To turn this off, set `MAXIMEM_ANALYTICS=0` in `~/.hermes/.env`.

## How it works

- **Recall before each turn.** A semantic search runs against the current message. If it returns within `recall_timeout` (default 6 seconds), the matches are injected as context; otherwise the turn proceeds with no memory rather than waiting. Only matches above `recall_min_score` are injected, so weak matches are not presented as fact.
- **Follow-up resolution.** A short follow-up such as "why?" or "tell me more" has no subject of its own to search on. The plugin detects these and prepends the last substantial message, so the search still finds the right memories. Self-contained messages are searched verbatim, so a clean topic change is never diluted.
- **Profile at session start.** When `profile_warmup` is on, a short "who is this user" summary is loaded off-turn and injected once at the start of a session as background context. It is cached across sessions for an hour to avoid repeated calls.
- **Capture after each turn.** The user and assistant exchange is saved to long-term memory on a background thread.
- **Memory mirroring.** When the Hermes built-in memory tool records a fact, it is also stored in Vity so it participates in semantic recall.

Recall runs synchronously on the pre-turn path but is bounded by `recall_timeout`, so a slow or unhealthy backend degrades to no memory for that turn instead of hanging. Capture, mirroring, and profile warm-load run on background threads and never block the reply.

## Agent tools

The plugin exposes these tools to the agent:

| Tool | Parameters | Purpose |
| --- | --- | --- |
| `vity_recall` | `query` (required), `top_k` (default 10, max 50) | Retrieve specific stored memories. |
| `vity_knowledge` | `query` (required), `limit` (default 10, max 100) | Retrieve consolidated, topic-level knowledge. |
| `vity_profile` | none | Retrieve the user's full stored memory profile. |
| `vity_store` | `content` (required), `memory_type` (`fact`, `preference`, `emotion`, `episode`, `knowledge`, `profile`) | Save a new memory. |
| `vity_forget` | `query`, `dry_run` (default `true`) | Delete matching memories, previewing first. |

`vity_recall` and `vity_knowledge` are complementary: use recall for a specific fact the user stated, and knowledge for what the user broadly knows about a topic. When Meeting Memory is enabled for the account, an additional `vity_meeting_recall` tool is offered for questions about meetings, decisions, and attendees.

In chat this is transparent. Ask the agent to "remember that ..." and it calls `vity_store`; ask "what do you know about ..." and it calls `vity_recall` or `vity_knowledge`. No special commands are required.

## CLI

Manage memory directly from the terminal:

```bash
hermes maximem_vity status                          # config and live connection check
hermes maximem_vity search "favorite color"         # semantic search
hermes maximem_vity search "deadlines" --limit 20 --json
hermes maximem_vity store "I prefer dark mode" --type preference
hermes maximem_vity forget "old project"            # dry run (preview)
hermes maximem_vity forget "old project" --yes      # confirm deletion
```

To reactivate the provider, run `hermes config set memory.provider maximem_vity`. Avoid the interactive `hermes memory setup` wizard, since buffered or pasted terminal input can drop the selection and leave the provider unset.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Tests stub the Hermes host modules (`agent.memory_provider`, `tools.registry`, `hermes_constants`, `utils`) so they run without a full Hermes checkout. See `tests/conftest.py`.

## Layout

| Path | Purpose |
| --- | --- |
| `src/hermes_maximem_vity/installer.py` | The `hermes-maximem-vity` console command (`install`, `uninstall`, `status`). |
| `src/hermes_maximem_vity/payload/provider.py` | `VityMemoryProvider` and `register()`, copied to `~/.hermes/plugins/maximem_vity/__init__.py` on install. |
| `src/hermes_maximem_vity/payload/cli.py` | The `hermes maximem_vity ...` subcommands. |
| `src/hermes_maximem_vity/payload/plugin.yaml` | Plugin manifest (dependencies, required env). |
| `src/hermes_maximem_vity/payload/vity.json.example` | Tunables template, seeded to `vity.json` on install. |
| `tests/` | Unit tests and host-module stubs. |

## License

MIT. See [LICENSE](LICENSE).

## Support

- Documentation: https://docs.maximem.ai/vity
- API keys: https://app.maximem.ai/api-keys
