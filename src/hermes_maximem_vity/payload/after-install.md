# Vity Memory is installed

`hermes-maximem-vity install` completed these steps:

- Copied the plugin into `~/.hermes/plugins/maximem_vity/`
- Installed `maximem-vity-sdk` (0.3.1 or newer) into the Hermes environment
- Activated the provider (`memory.provider: maximem_vity`)

If the installer reported **All set**, start the agent:

```bash
hermes
```

If Hermes or the gateway was running during install, restart it to load the provider. Gateway users run `hermes gateway restart`.

## Set or change your API key

The installer prompts for the key. To set or replace it, pass it explicitly. It is written without duplicates, so no stale copies remain:

```bash
hermes-maximem-vity install --api-key mx_your_key
```

A plain re-install keeps your current key. Get a key at https://app.maximem.ai/api-keys. Gateway users restart afterwards with `hermes gateway restart`.

## Verify

```bash
hermes memory status                 # shows maximem_vity as active
hermes maximem_vity status           # config and live connection check
hermes maximem_vity search "anything"
```

## Re-activate

```bash
hermes config set memory.provider maximem_vity
```

Avoid the interactive `hermes memory setup` wizard. Buffered or pasted terminal input can drop the selection and leave the provider unset.

## Usage telemetry

The plugin sends anonymous usage events (latency, counts, outcomes) and never sends prompts, responses or memory content. To opt out, add `MAXIMEM_ANALYTICS=0` to `~/.hermes/.env`.
