# aside-antigravity-proxy

Use your Google Antigravity models inside [Aside](https://aside.studio): Gemini, plus the Claude and GPT-OSS models Antigravity exposes, as a provider you pick from Aside's model list.

Aside talks to models over the Anthropic Messages API. Antigravity speaks its own dialect. This is a small local server that sits between them: it takes Aside's requests, rewrites them for the Antigravity gateway, and rewrites the replies back. It signs those requests with the login the Antigravity CLI already made, so there's no second account to set up and no API key to paste.

Tool calls work, which is the part that matters. Aside's agent runs on tools. Skills come along with them, since Aside ships skills as instructions in the system prompt and the model reads them with the same file tools.

## Requirements

- macOS
- Python 3 (the system one is fine)
- [Antigravity CLI](https://antigravity.google) (`agy`), logged in. Run `agy` once and finish the browser login. The proxy reuses that session. If it isn't there, the installer stops and tells you.

## Install

```bash
git clone https://github.com/2JIHAN/aside-antigravity-proxy.git
cd aside-antigravity-proxy
./install.sh
```

### What it does

1. Checks that you're logged into the Antigravity CLI.
2. Picks a free port.
3. Registers a launchd agent, so the proxy comes back after a reboot.
4. Probes which Antigravity models answer and registers the ones that do.
5. Adds an **Antigravity** provider to Aside's `models.json`.

Restart Aside afterwards. It reads `models.json` at startup, so a running app won't notice the new provider until then.

## Update

```bash
./update.sh
```

Pulls the latest code, runs any one-time migrations you skipped, and reinstalls. It stops if you have local changes rather than clobbering them, and it's fine to jump several versions at once — migrations run in order from wherever you are.

Restart Aside afterwards if the model list changed.

## Use it

In the app, open **Settings → AI → Providers**. *Antigravity* is listed there, and its models appear in the model picker next to your other ones.

From the CLI:

```bash
aside exec --provider antigravity --model gemini-3.7-flash-high "Summarize this page"
```

## Models

Models are dynamically discovered and formatted across Gemini, Claude, and GPT-OSS families without static hardcoding. Effort suffixes (`-high`, `-medium`, `-low`, `-thinking`) are collapsed to show each model cleanly in the picker.

If you ever want to re-run a manual probe and sync with all Aside profiles:

```bash
./refresh-models.sh
```

## Options

| | |
|---|---|
| Different port | `./install.sh 9000` — the installer also moves off a port that's taken |
| Logs | `proxy.log`, `proxy.err.log` in this directory — trimmed by `./rotate-logs.sh` (`KEEP_LINES=500 ./rotate-logs.sh`) |
| Health check | `curl http://127.0.0.1:8317/health` |
| Uninstall | `./uninstall.sh` — removes the launchd daemon and the Aside provider entry, leaves your Antigravity login alone |

## Credentials

Nothing secret lives in this repository. The proxy reads your OAuth token from wherever the Antigravity CLI keeps it — its own token file on older versions, the login keychain from 1.1.7 on — and reads the client secret out of the installed `agy` binary. It borrows the CLI's login, so it borrows the CLI's identity too. Set `ANTIGRAVITY_CLIENT_SECRET` if you'd rather supply it yourself.

## Known limits

**macOS calls it an unidentified developer.** In System Settings → Login Items the proxy shows up as `aside-antigravity-proxy` from an unidentified developer. That's what an unsigned script looks like; getting rid of it would take an Apple developer certificate.

**Aside labels the provider "API", not "Subscription".** Aside reserves "Subscription" for a fixed list of built-in providers, and picks provider icons the same way. Neither is configurable from `models.json`, so the entry gets the generic treatment.

**Some Antigravity models are unreachable.** `gemini-3.1-pro-high` and the higher `gemini-3.5-flash` tiers are refused by the gateway even though `agy models` lists them. The probe drops them rather than leaving dead entries in your picker.

**macOS only.** The launchd agent and the paths are Mac-specific. Nothing else is, so a Linux port is mostly a matter of swapping launchd for systemd.
