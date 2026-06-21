# Vity Memory installed 🎉

Two steps to activate Vity as your Hermes memory provider:

1. **Set your Maximem API key** (skip if you entered it during install):

   ```bash
   echo 'MAXIMEM_API_KEY=mx_...' >> ~/.hermes/.env
   ```

2. **Activate Vity** (installs the SDK and sets `memory.provider`):

   ```bash
   hermes memory setup vity
   ```

Then restart the gateway if you run one:

```bash
hermes gateway restart
```

## Verify

```bash
hermes memory status     # should show vity active
hermes vity status       # config + live connection check
hermes vity search "anything"
```

Get an API key at https://maximem.ai/dashboard
