# Vity Memory installed 🎉

`hermes-maximem-vity install` already did everything for you:

- ✅ Copied the plugin into `~/.hermes/plugins/maximem_vity/`
- ✅ Installed `maximem-vity-sdk` into Hermes' **own** environment
- ✅ Activated the provider (`memory.provider: maximem_vity`)

If the installer printed **✅ All set!**, just start the agent:

```bash
hermes
```

## Set or change your API key

The installer prompts for the key. To set it — **or to replace an existing one** — pass it explicitly (written de-duplicated, so no stale copies remain):

```bash
hermes-maximem-vity install --api-key mx_your_key
```

Note: a plain re-install **keeps** your current key (`already configured`) — you must pass `--api-key` to change it. Get a key at https://app.maximem.ai/api-keys

Gateway users: restart to pick up the change — `hermes gateway restart`.

## Verify

```bash
hermes memory status     # should show maximem_vity active
hermes maximem_vity status       # config + live connection check (connection: ok ✓)
hermes maximem_vity search "anything"
```

## Re-activate (if ever needed)

```bash
hermes config set memory.provider maximem_vity
```

Avoid the interactive `hermes memory setup` wizard — buffered/pasted terminal
input can silently drop the selection and leave the provider unset.
