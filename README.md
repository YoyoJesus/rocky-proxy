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

`discord_bot.py` talks to Rocky directly (the proxy is not needed). It replies when mentioned, when addressed by name (any word in `BOT_NAMES`, case-insensitive), when someone replies to it, in DMs, and to every message in `BOT_CHANNELS`. The last `BOT_HISTORY` channel messages are sent as context, labeled with each author's name and whether they are a person or a bot.

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications), add a bot, and enable the **Message Content** intent.
2. Invite it with the `bot` scope and the Send Messages and Read Message History permissions.
3. Save the token the same way as the Rocky key, as `DISCORD_BOT_TOKEN`.
4. Run `uv run discord_bot.py`.

| Variable            | Default                                   |
| ------------------- | ----------------------------------------- |
| `DISCORD_BOT_TOKEN` | required                                  |
| `ROCKY_MODEL`       | first model from `GET /v1/models`         |
| `BOT_NAMES`         | `rocky` (comma-separated, empty disables) |
| `BOT_CHANNELS`      | none (comma-separated channel IDs)        |
| `BOT_PERSONA`       | `You are friendly, helpful, and concise.` |
| `BOT_PERSONA_FILE`  | `personality.md` beside the bot script; replaces `BOT_PERSONA` when present |
| `BOT_HISTORY`       | `20` messages                             |
| `BOT_MAX_BOT_TURNS` | `5` replies to bots before a human speaks |
| `BOT_COOLDOWN`      | `5` seconds per channel                   |
| `BOT_CODE_MAX_LINES` | `2` lines allowed in a code block        |
| `BOT_LIVE_MEETINGS` | `1`; `0` stops adding the current HacKSU schedule (hacksu.com, cached 1 hour) |
| `BOT_CREATOR_ID`    | Discord user ID labeled as the creator    |
| `BOT_IGNORE_IDS`    | Discord user IDs that never trigger a reply (default: ducksu) |
| `BOT_FACTS_FILE`    | `bot_facts.md` beside the bot script      |

Loop guards: the bot never replies to itself or to users in `BOT_IGNORE_IDS` (the ducksu bot by default), stops replying to other bots after `BOT_MAX_BOT_TURNS` in a row until a human posts, ignores bot messages during the cooldown, and backs off for Rocky's rate-limit reset time on errors. Mentions in model output are disabled so it cannot ping `@everyone` or roles.

The bot does not write code. It is instructed to explain programming concepts and point to course resources instead, and any fenced code block longer than `BOT_CODE_MAX_LINES` non-blank lines is replaced with a notice before sending. Short setup commands quoted from the knowledge references still get through.

`personality.md`, if present, is a longer personality and behavior guide that replaces `BOT_PERSONA` (up to 12,000 characters, re-read on each reply). Long guides add tokens to every request.

`bot_facts.md` holds short trusted facts that are added to every reply's instructions: the bot's identity, its creator, nicknames, the separate Discord servers, and tutoring guidance. It is re-read on each reply, so edits apply without a restart. Everything in it can be repeated in any server, so keep it public. Messages from `BOT_CREATOR_ID` are labeled as the creator by user ID, so a display name cannot impersonate them. The instructions also name the current server and channel.

## Kent State CS and HacKSU knowledge

The Discord bot searches a local SQLite full-text index before asking Rocky to answer. It sends
matching excerpts with source links and fetch dates alongside the conversation. No embeddings,
extra API key, external database, or model tool calling is needed.
The initial sources cover the CS department and catalog, faculty and staff profiles, HacKSU, Kent Hack Enough (`khe.io`),
and Kent Hack It (`ctf.hacksu.com`), including its public Kali setup guide.
It also includes HacKSU's GitHub organization and selected public repository READMEs for
club/event websites, lessons and workshop slides.

From the repository root, import the public pages in `knowledge/sources.json`:

```powershell
uv run knowledge_base.py refresh
uv run knowledge_base.py status
uv run knowledge_base.py search "HacKSU meetings"
uv run discord_bot.py
```

Add public HTML or plain-text Markdown URLs to [knowledge/sources.json](knowledge/sources.json), or write curated
Markdown notes under [knowledge/](knowledge/README.md), then refresh again. Only explicitly
listed web pages are fetched; linked pages are not crawled. Source selection notes are in
[knowledge/SOURCES.md](knowledge/SOURCES.md). The generated `data/knowledge.sqlite3` stays local
and is ignored by Git. Importing knowledge does not need Discord or Rocky credentials.
Kent State, HacKSU (including subdomains), `khe.io`, GitHub and GitHub raw-file hosts are allowed
by default. For another trusted public host, pass
`--allow-host example.org` on refresh or set `KNOWLEDGE_ALLOWED_HOSTS` to a comma-separated list.
Each redirect must also remain on an allowed public HTTPS host.
Hosts must resolve to public addresses, except exact hostnames in `KNOWLEDGE_PRIVATE_HOSTS` (comma-separated),
which may resolve to private LAN addresses (never loopback or link-local). `web.cs.kent.edu`, which hosts the
class schedule and only resolves on Kent State's network, is included by default.

Refresh downloads snapshots; the bot does not browse live. Run refresh periodically (for example,
daily before starting the bot), especially around meeting changes and registration. A failed
download preserves the last good indexed version. Review refresh output for failures and check
fetch dates with `status`. No scheduled task is installed. The bot reads the database on each
answer, so a refresh does not require a restart.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_KNOWLEDGE_DB` | `data/knowledge.sqlite3` beside the bot script | Database to search |
| `BOT_KNOWLEDGE_LIMIT` | `5` | Maximum matching excerpts; `0` disables retrieval |
| `BOT_KNOWLEDGE_MAX_CHARS` | `12000` | Maximum reference context characters |

The importer accepts `--db`, `--sources`, and `--knowledge-dir` overrides on `refresh`;
`search` and `status` accept `--db`. If you use a custom database, configure the bot to use the
same path. On Windows, `tzdata` supplies the Eastern time zone used for date-aware instructions.

The bot is instructed to cite sources, distinguish past events and catalog years, and acknowledge
missing information. Keyword retrieval can miss paraphrases, and citations and answer accuracy
still depend on the model. Try representative questions with `search` when adding sources.
Public knowledge is shared across all bot conversations: private Discord messages and channel
archives are not imported. Only add information suitable for that audience.

Run offline checks with `uv run python -m unittest discover -s tests -v`.

## Notes

- Rocky logs all requests and rate limits each key. Check your course policy before running agent loops or exposing the bot to a public server, since other people's messages are sent under your key.
- Rocky errors are passed through unchanged, and the proxy logs Rocky's `x-request-id` for troubleshooting.
