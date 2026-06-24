# Vity Memory installed 🎉

`hermes-maximem-vity install` already did everything for you:

- ✅ Copied the plugin into `~/.hermes/plugins/vity/`
- ✅ Installed `maximem-vity-sdk` into Hermes' **own** environment
- ✅ Activated the provider (`memory.provider: vity`)

If the installer printed **✅ All set!**, just start the agent:

```bash
hermes
```

## If your API key wasn't set

The installer prompts for it (or pass `--api-key mx_…`). To add it now:

```bash
echo 'MAXIMEM_API_KEY=mx_...' >> ~/.hermes/.env
```

Get a key at https://app.maximem.ai/api-keys

Running a gateway? Restart it to pick up the change: `hermes gateway restart`.

## Verify

```bash
hermes memory status     # should show vity active
hermes vity status       # config + live connection check (connection: ok ✓)
hermes vity search "anything"
```

## Re-activate (if ever needed)

```bash
hermes config set memory.provider vity
```

Avoid the interactive `hermes memory setup` wizard — buffered/pasted terminal
input can silently drop the selection and leave the provider unset.
