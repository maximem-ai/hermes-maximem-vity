# Vity Memory Provider for Hermes Agent

**Vity by Maximem AI** — cross-session semantic memory for [Hermes Agent](https://github.com/NousResearch/hermes-agent), shipped as a **standalone plugin** (not bundled with core Hermes).

Vity gives the agent a persistent memory graph (facts, preferences, emotions, episodes, knowledge, profile) with profile-based recall and low-latency context injection over the Maximem REST API.

> Powered by the [`maximem-vity-sdk`](https://pypi.org/project/maximem-vity-sdk/) Python client.
> This plugin mirrors Maximem's official [OpenClaw plugin](https://www.npmjs.com/package/@maximem/memory-plugin): `before_agent_start → recall`, `agent_end → capture`.

---

## Install

```bash
pip install hermes-maximem-vity
hermes-maximem-vity install                    # copies plugin in + activates it
echo 'MAXIMEM_API_KEY=mx_...' >> ~/.hermes/.env # your key
```

That's it:
1. **`pip install hermes-maximem-vity`** — installs the plugin and its `maximem-vity-sdk` dependency.
2. **`hermes-maximem-vity install`** — copies the provider into `~/.hermes/plugins/vity/` (where Hermes auto-discovers it) **and sets `memory.provider: vity`** for you.
3. **Add your API key** to `~/.hermes/.env`.

> **Don't use the interactive `hermes memory setup` wizard to activate** — pasted/buffered terminal input can silently drop the selection, leaving the provider at "none". `hermes-maximem-vity install` activates it deterministically. If you ever need to re-activate manually, run:
> ```bash
> hermes config set memory.provider vity
> ```

Get an API key at [app.maximem.ai/api-keys](https://app.maximem.ai/api-keys) (starts with `mx_`).

**Verify:**
```bash
hermes memory status     # vity ← active
hermes vity status       # API key set ✓, connection ok ✓
```

**Update / remove:**
```bash
pip install -U hermes-maximem-vity && hermes-maximem-vity install --force   # update
hermes-maximem-vity uninstall                                               # remove
```

---

## Configuration

**Secret** — env var only (`~/.hermes/.env`):

| Key | Env var | Required | Description |
|---|---|---|---|
| `api_key` | `MAXIMEM_API_KEY` | ✅ | Maximem API key (`mx_...`). `VITY_API_KEY` also accepted for back-compat. |

**Tunables** — non-secret, in `$HERMES_HOME/vity.json` (a starter `vity.json` is created on install from `vity.json.example`):

| Key | Default | Description |
|---|---|---|
| `auto_recall` | `true` | Inject memories before each turn |
| `auto_capture` | `true` | Capture the conversation after each turn |
| `max_recall_tokens` | `1000` | Token budget for recalled context |
| `min_prompt_length` | `5` | Skip recall for trivially short prompts |

> **The API key owns the memory space.** Vity does not derive memory identity from gateway users, sessions, or channels — use a separate API key per user/account that needs isolated memories.

---

## How it works

- **Warm-up recall** — on session start, `initialize()` kicks off a background recall with a broad profile query so the first message already has context (cold-start wait with a blocking fallback).
- **Per-turn prefetch** — before each turn, recalled context is injected as a `## Vity Memory` block.
- **Per-turn capture** — after each turn, the exchange is captured into long-term memory in the background.
- **Built-in memory mirroring** — when Hermes' built-in `memory` tool writes a fact, `on_memory_write()` mirrors it into Vity so it joins semantic recall (parity with OpenClaw's `/remember`).

All recall/capture work runs on daemon threads — the agent loop never blocks on the network.

---

## Tools exposed to the agent

| Tool | Parameters | Purpose |
|---|---|---|
| `vity_recall` | `query` (required), `top_k` (default 10, max 50) | Semantic search of the memory graph. |
| `vity_profile` | — | Retrieve the user's full stored memory profile. |
| `vity_store` | `content` (required), `memory_type` (`fact`/`preference`/`emotion`/`episode`/`knowledge`/`profile`) | Save a new memory fact. |
| `vity_forget` | `query`, `dry_run` (default `true`) | Delete matching memories (previews first). |

---

## CLI (`hermes vity ...`)

The terminal analog of `openclaw maximem ...`:

```bash
hermes vity status                          # config + live connection check
hermes vity search "favorite color"         # semantic search
hermes vity search "deadlines" --limit 20 --json
hermes vity store "I prefer dark mode" --type preference
hermes vity forget "old project"            # dry-run (preview)
hermes vity forget "old project" --yes      # confirm deletion
```

---

## Slash commands

Hermes has no generic plugin-registered in-chat slash-command API (unlike OpenClaw's `/remember` / `/recall`). The equivalent here is:

- **Agent-driven** — the agent autonomously calls `vity_store`/`vity_recall`, so "remember that …" in chat works without a literal command.
- **Built-in memory mirroring** — `on_memory_write()` propagates Hermes' built-in memory writes into Vity.
- **CLI** — `hermes vity …` covers terminal-side management.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Tests stub the Hermes host modules (`agent.memory_provider`, `tools.registry`, `hermes_constants`, `utils`) so they run without a full Hermes checkout — see `tests/conftest.py`.

---

## Layout

| Path | Purpose |
|---|---|
| `src/hermes_maximem_vity/installer.py` | The `hermes-maximem-vity` console command (`install` / `uninstall` / `status`). |
| `src/hermes_maximem_vity/payload/provider.py` | `VityMemoryProvider` + `register()` — copied to `~/.hermes/plugins/vity/__init__.py` on install. |
| `src/hermes_maximem_vity/payload/cli.py` | `hermes vity ...` subcommands. |
| `src/hermes_maximem_vity/payload/plugin.yaml` | Plugin manifest (deps, required env). |
| `src/hermes_maximem_vity/payload/vity.json.example` | Non-secret tunables template (seeded to `vity.json` on install). |
| `src/hermes_maximem_vity/payload/after-install.md` | Post-install notes. |
| `tests/` | Unit tests + host-module stubs. |

## License

MIT — see [LICENSE](LICENSE).

## Support

- Docs: https://docs.maximem.ai/vity
- API keys: https://app.maximem.ai/api-keys
