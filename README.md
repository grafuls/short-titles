# short-titles

A [Hermes](https://github.com/NousResearch/hermes-agent) plugin that renames
Telegram DM topics to a single word, or two at most.

Hermes core already auto-titles sessions (3-7 words) and renames the Telegram
topic to match. This plugin watches for that title, asks the model to distill
it to ≤2 words, and renames the topic again via the Bot API — so instead of
"Debug tailscale spark-244c port 5555 unreachable" the topic reads "Tailscale
debug".

## How it behaves

- Fires once per new thread, after its first message produces a title — never
  retroactively on threads that existed before the plugin was enabled.
- Only touches threads Hermes titled itself (`title_source='llm'`). If you set
  a name yourself with `/title`, this plugin leaves it alone.
- If the LLM title never lands (e.g. a flaky provider), falls back to
  condensing the raw derived title after a grace period, then re-renames if
  the real title shows up later.
- Retries a failed rename up to 3 times, then leaves Hermes's own title in
  place — a dead/deleted topic id won't retry forever.
- Runs as a 30-second poll inside the Hermes gateway; no extra process.

## Install

```bash
git clone https://github.com/grafuls/short-titles.git ~/.hermes/plugins/short-titles
hermes plugins enable short-titles
```

No restart needed — `hermes plugins enable` hot-reloads the running gateway.
Needs your Telegram bot token already configured for Hermes
(`platforms.telegram.token` in `config.yaml`, or `TELEGRAM_BOT_TOKEN` in
`.env`/env) — the same one Hermes itself uses.

## Tuning

Constants at the top of `__init__.py`:

| Constant | Default | Meaning |
|---|---|---|
| `TICK_S` | 30s | Poll interval |
| `MIN_AGE_S` | 25s | Delay so core's own rename lands first |
| `MAX_ATTEMPTS` | 3 | Retries per title before giving up |
| `MAX_PER_TICK` | 5 | Rename burst cap per tick |
| `DERIVED_GRACE_S` | 180s | Fallback delay if the LLM title never arrives |

## Tests

Pure logic, zero dependencies — runs with any Python 3.10+:

```bash
python3 test_plugin.py
```

## Uninstall

```bash
hermes plugins disable short-titles
rm -rf ~/.hermes/plugins/short-titles
```
