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

## Notes

- Rocky logs all requests and rate limits each key. Check your course policy before running agent loops.
- Rocky errors are passed through unchanged, and the proxy logs Rocky's `x-request-id` for troubleshooting.
