# rocky-proxy

A local Flask proxy that exposes Kent State's [Rocky](https://rocky.cs.kent.edu) API (`/v1/responses`) as an OpenAI-compatible Chat Completions API. This lets text-based harnesses like [Aider](https://aider.chat) use a Rocky key.

## Endpoints

| Proxy                       | Rocky                 |
| --------------------------- | --------------------- |
| `GET /v1/models`            | `GET /v1/models`      |
| `POST /v1/chat/completions` | `POST /v1/responses`  |

Buffered and streamed (SSE) completions, text, and base64 image input are supported. Rocky has no tool calling, so requests with `tools`, `functions`, or `tool` messages return a 400. Unsupported fields like `stop` and `seed` are dropped. Requests are sent with `store: false`.

## Setup

Save your key as a user environment variable (hidden prompt):

```powershell
$k = Read-Host "Rocky key" -AsSecureString
[Environment]::SetEnvironmentVariable("ROCKY_API_KEY", [System.Net.NetworkCredential]::new("", $k).Password, "User")
```

On Windows the proxy reads the key from the user registry if the current shell does not have it yet. On other platforms, export `ROCKY_API_KEY`.

Run the proxy (binds to `127.0.0.1:8000`):

```powershell
uv run rocky_proxy.py
```

Or with pip: `pip install flask requests` then `python rocky_proxy.py`.

| Variable        | Default                           |
| --------------- | --------------------------------- |
| `ROCKY_API_KEY` | required                          |
| `ROCKY_BASE`    | `https://rocky.cs.kent.edu/v1`    |
| `PORT`          | `8000`                            |

Clients can send any API key; the proxy always uses `ROCKY_API_KEY`.

## Aider

```powershell
uv tool install --python 3.12 aider-chat
Copy-Item examples\.aider.conf.yml, examples\.aider.model.metadata.json $HOME
aider
```

The examples target `qwen3.8-27b`. If Rocky's model changes, check `GET /v1/models` and update the model id and limits in both files.

## Discord bot

`discord_bot.py` talks to Rocky directly (the proxy is not needed). It replies when mentioned, when someone replies to it, in DMs, and to every message in `BOT_CHANNELS`. The last `BOT_HISTORY` channel messages are sent as context, labeled with each author's name and whether they are a person or a bot.

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications), add a bot, and enable the **Message Content** intent.
2. Invite it with the `bot` scope and the Send Messages and Read Message History permissions.
3. Save the token the same way as the Rocky key, as `DISCORD_BOT_TOKEN`.
4. Run `uv run discord_bot.py`.

| Variable            | Default                                   |
| ------------------- | ----------------------------------------- |
| `DISCORD_BOT_TOKEN` | required                                  |
| `ROCKY_MODEL`       | first model from `GET /v1/models`         |
| `BOT_CHANNELS`      | none (comma-separated channel IDs)        |
| `BOT_PERSONA`       | `You are friendly, helpful, and concise.` |
| `BOT_HISTORY`       | `20` messages                             |
| `BOT_MAX_BOT_TURNS` | `5` replies to bots before a human speaks |
| `BOT_COOLDOWN`      | `5` seconds per channel                   |

Loop guards: the bot never replies to itself, stops replying to other bots after `BOT_MAX_BOT_TURNS` in a row until a human posts, ignores bot messages during the cooldown, and backs off for Rocky's rate-limit reset time on errors. Mentions in model output are disabled so it cannot ping `@everyone` or roles.

## Notes

- Rocky logs all requests and rate limits each key. Check your course policy before running agent loops or exposing the bot to a public server, since other people's messages are sent under your key.
- Rocky errors are passed through unchanged, and the proxy logs Rocky's `x-request-id` for troubleshooting.
