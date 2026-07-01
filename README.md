# Vity Memory for Hermes Agent

**Vity by Maximem AI** — persistent, cross-session semantic memory for [Hermes Agent](https://github.com/NousResearch/hermes-agent), distributed as a standalone plugin.

Vity gives the agent a long-term memory graph (facts, preferences, episodes, knowledge, profile). It automatically recalls relevant context before each turn and captures the conversation after each turn, so the agent remembers users and projects across sessions.

> Built on the [`maximem-vity-sdk`](https://pypi.org/project/maximem-vity-sdk/) Python client.

---

## Install

First, get an API key at **[app.maximem.ai/api-keys](https://app.maximem.ai/api-keys)** (starts with `mx_`). Then:

```bash
pip install hermes-maximem-vity
hermes-maximem-vity install
```

`hermes-maximem-vity install` does everything in one step:

1. Copies the plugin into `~/.hermes/plugins/maximem_vity/`, where Hermes discovers it.
2. **Prompts for your API key** and saves it to `~/.hermes/.env` (no duplicates).
3. **Activates** the provider (`memory.provider: maximem_vity`).

It prints `✅ All set!` when finished. Start the agent with `hermes`.

> Already had Hermes (or the gateway) running during install? Restart it to load the newly-activated provider — gateway users: `hermes gateway restart`.

**Non-interactive / scripted installs** — pass the key as a flag to skip the prompt:

```bash
hermes-maximem-vity install --api-key mx_your_key
```

<details><summary>Hitting a PEP-668 "externally-managed-environment" or "pip: command not found" error?</summary>

System Python (e.g. Homebrew) blocks global `pip install`. Use **pipx** (recommended) or a virtual environment:

```bash
pipx install hermes-maximem-vity        # install pipx first if needed: brew install pipx
hermes-maximem-vity install
```

You don't need to match Python versions — the `maximem-vity-sdk` dependency is installed into Hermes' own environment automatically.

</details>

### Verify

```bash
hermes-maximem-vity status   # plugin installed ✓, SDK available to Hermes ✓
hermes memory status         # shows: maximem_vity ← active
hermes maximem_vity status           # API key set ✓, SDK installed ✓, connection ok ✓
```

### Update / remove

```bash
pip install -U hermes-maximem-vity && hermes-maximem-vity install   # update (always overwrites)
hermes-maximem-vity uninstall                                               # remove
```

### Troubleshooting

**`hermes maximem_vity status` still says `maximem-vity-sdk not installed` — even after `pip install`?**

Hermes runs inside its **own isolated environment**, which is usually *not* the Python that ran `pip install` (Anaconda or system Python). The SDK must live in Hermes' environment, and `hermes-maximem-vity install` puts it there for you — just re-run it:

```bash
hermes-maximem-vity install
hermes-maximem-vity status     # confirm: SDK available to Hermes ✓
```

---

## Configuration

**API key** (secret — stored in `~/.hermes/.env`):

| Env var             | Required | Description                                                          |
| ------------------- | -------- | -------------------------------------------------------------------- |
| `MAXIMEM_API_KEY` | ✅       | Your Maximem API key (`mx_…`). `VITY_API_KEY` is also accepted. |

> The API key owns the memory space — use a separate key per account that needs isolated memories.

**Change / update your key** — a plain re-install **keeps** the existing key (you'll see `already configured`). To replace it, pass the new key explicitly (it's written de-duplicated, so no stale copies are left behind):

```bash
hermes-maximem-vity install --api-key mx_your_new_key
```

Or edit `~/.hermes/.env` directly, then restart Hermes (gateway users: `hermes gateway restart`).

**Tunables** (optional, non-secret — `$HERMES_HOME/vity.json`, created on install):

| Key                   | Default  | Description                               |
| --------------------- | -------- | ----------------------------------------- |
| `auto_recall`       | `true` | Inject relevant memories before each turn |
| `auto_capture`      | `true` | Capture the conversation after each turn  |
| `max_recall_tokens` | `1000` | Size cap for the injected recall context  |
| `min_prompt_length` | `5`    | Skip recall for very short prompts        |
| `recall_timeout`    | `6.0`  | Max seconds to wait for pre-turn recall   |

**Self-hosted backend** (optional): set `MAXIMEM_ENDPOINT` (or `endpoint` in `vity.json`) to point at a non-default Maximem API URL.

---

## How it works

- **Recall before each turn** — a semantic search runs against your current message and, if it returns within a short timeout (`recall_timeout`, default 6s), the matches are injected as context; otherwise the turn proceeds with no memory rather than waiting.
- **Capture after each turn** — the user/assistant exchange is saved to long-term memory.
- **Memory mirroring** — when Hermes' built-in memory tool records a fact, it is also stored in Vity so it participates in semantic recall.

Recall runs synchronously on the pre-turn path but is bounded by `recall_timeout` (default 6s), so a slow or unhealthy backend degrades to no-memory-this-turn instead of hanging. Capture and memory-mirroring writes run on background threads, so they never block the reply.

---

## Agent tools

The plugin exposes four tools to the agent:

| Tool             | Parameters                                                                                                          | Purpose                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| `vity_recall`  | `query` (required), `top_k` (default 10, max 50)                                                                | Semantic search of stored memories.             |
| `vity_profile` | —                                                                                                                  | Retrieve the user's full stored memory profile. |
| `vity_store`   | `content` (required), `memory_type` (`fact`/`preference`/`emotion`/`episode`/`knowledge`/`profile`) | Save a new memory.                              |
| `vity_forget`  | `query`, `dry_run` (default `true`)                                                                           | Delete matching memories (previews first).      |

In chat, this is transparent: ask the agent to "remember that …" and it calls `vity_store`; ask "what do you know about …" and it calls `vity_recall`. No special commands are required.

---

## CLI

Manage memory directly from the terminal:

```bash
hermes maximem_vity status                          # config + live connection check
hermes maximem_vity search "favorite color"         # semantic search
hermes maximem_vity search "deadlines" --limit 20 --json
hermes maximem_vity store "I prefer dark mode" --type preference
hermes maximem_vity forget "old project"            # dry-run (preview)
hermes maximem_vity forget "old project" --yes      # confirm deletion
```

> To (re)activate the provider, use `hermes config set memory.provider maximem_vity`. Avoid the interactive `hermes memory setup` wizard — buffered/pasted terminal input can drop the selection and leave the provider unset.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Tests stub the Hermes host modules (`agent.memory_provider`, `tools.registry`, `hermes_constants`, `utils`) so they run without a full Hermes checkout — see `tests/conftest.py`.

## Layout

| Path                                                  | Purpose                                                                                                         |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `src/hermes_maximem_vity/installer.py`              | The `hermes-maximem-vity` console command (`install` / `uninstall` / `status`).                          |
| `src/hermes_maximem_vity/payload/provider.py`       | `VityMemoryProvider` + `register()` — copied to `~/.hermes/plugins/maximem_vity/__init__.py` on install. |
| `src/hermes_maximem_vity/payload/cli.py`            | The `hermes maximem_vity …` subcommands.                                                                      |
| `src/hermes_maximem_vity/payload/plugin.yaml`       | Plugin manifest (dependencies, required env).                                                                   |
| `src/hermes_maximem_vity/payload/vity.json.example` | Tunables template (seeded to `vity.json` on install).                                                          |
| `tests/`                                            | Unit tests + host-module stubs.                                                                                 |

## License

MIT — see [LICENSE](LICENSE).

## Support

- Documentation: https://docs.maximem.ai/vity
- API keys: https://app.maximem.ai/api-keys
