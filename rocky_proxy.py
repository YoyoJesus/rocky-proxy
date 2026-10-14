"""Local proxy that exposes Kent State's Rocky API as OpenAI Chat Completions."""

import json
import os
import time
from collections.abc import Iterator
from typing import Any

import requests
from flask import Flask, Response, jsonify, request, stream_with_context


def load_key() -> str:
    if key := os.environ.get("ROCKY_API_KEY"):
        return key
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as reg:
                return winreg.QueryValueEx(reg, "ROCKY_API_KEY")[0]
        except FileNotFoundError:
            pass
    raise SystemExit("ROCKY_API_KEY is not set.")


ROCKY_BASE = os.environ.get("ROCKY_BASE", "https://rocky.cs.kent.edu/v1")
ROCKY_KEY = load_key()
TIMEOUT = 600

PARAM_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "frequency_penalty": "frequency_penalty",
    "presence_penalty": "presence_penalty",
    "max_tokens": "max_output_tokens",
    "max_completion_tokens": "max_output_tokens",
}

app = Flask(__name__)


def rocky_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ROCKY_KEY}"}


def error(message: str, status: int = 400, code: str = "unsupported") -> tuple[Response, int]:
    return jsonify({"error": {"message": message, "type": "invalid_request_error", "code": code}}), status


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if p.get("type") == "text")
    return ""


def convert_message(msg: dict) -> dict:
    role = msg.get("role")
    content = msg.get("content")
    if role not in ("system", "developer", "user", "assistant"):
        raise ValueError(f"Rocky does not support '{role}' messages (no tool calling).")
    if role != "user" or isinstance(content, str):
        return {"role": role, "content": text_of(content)}

    blocks = []
    for part in content or []:
        match part.get("type"):
            case "text":
                blocks.append({"type": "input_text", "text": part["text"]})
            case "image_url":
                url = part["image_url"]["url"] if isinstance(part["image_url"], dict) else part["image_url"]
                blocks.append({"type": "input_image", "image_url": url, "detail": "auto"})
            case other:
                raise ValueError(f"Unsupported content type '{other}'.")
    return {"role": role, "content": blocks}


def build_rocky_request(body: dict) -> dict:
    if body.get("tools") or body.get("functions"):
        raise ValueError("Rocky does not support tool calling. Use a harness that works from plain text.")
    if body.get("n", 1) != 1:
        raise ValueError("Only n=1 is supported.")

    messages = [convert_message(m) for m in body.get("messages", [])]
    if not any(m["role"] == "user" for m in messages):
        raise ValueError("At least one user message is required.")

    out = {"model": body["model"], "input": messages, "store": False}
    for src, dst in PARAM_MAP.items():
        if body.get(src) is not None:
            out[dst] = body[src]
    return out


def to_usage(usage: dict | None) -> dict | None:
    if not usage:
        return None
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def finish_reason(resp: dict) -> str:
    return "length" if resp.get("status") == "incomplete" else "stop"


def upstream_error(r: requests.Response) -> tuple[Response, int]:
    try:
        payload = r.json()
    except ValueError:
        payload = {"error": {"message": r.text or r.reason, "type": "upstream_error", "code": str(r.status_code)}}
    app.logger.warning("Rocky %s (x-request-id=%s): %s", r.status_code, r.headers.get("x-request-id"), payload)
    return jsonify(payload), r.status_code


def iter_sse(r: requests.Response) -> Iterator[dict]:
    data: list[str] = []
    for line in r.iter_lines(decode_unicode=True):
        if line is None:
            continue
        if line == "":
            if data:
                yield json.loads("\n".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield json.loads("\n".join(data))


def chunk(cid: str, created: int, model: str, delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
    payload: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["choices"] = []
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/v1/models")
def models():
    r = requests.get(f"{ROCKY_BASE}/models", headers=rocky_headers(), timeout=30)
    if not r.ok:
        return upstream_error(r)
    return jsonify(r.json())


@app.post("/v1/chat/completions")
def chat_completions():
    body = request.get_json(force=True)
    try:
        rocky_body = build_rocky_request(body)
    except (ValueError, KeyError) as e:
        return error(str(e))

    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    rocky_body["stream"] = stream

    r = requests.post(
        f"{ROCKY_BASE}/responses", headers=rocky_headers(), json=rocky_body, stream=stream, timeout=TIMEOUT
    )
    if not r.ok:
        return upstream_error(r)

    if not stream:
        resp = r.json()
        return jsonify(
            {
                "id": "chatcmpl-" + resp["id"],
                "object": "chat.completion",
                "created": resp.get("created_at", int(time.time())),
                "model": resp.get("model", body["model"]),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": resp.get("output_text", "")},
                        "finish_reason": finish_reason(resp),
                    }
                ],
                "usage": to_usage(resp.get("usage")),
            }
        )

    def generate() -> Iterator[str]:
        cid, created, model = "chatcmpl-pending", int(time.time()), body["model"]
        with r:
            for event in iter_sse(r):
                match event.get("type"):
                    case "response.created":
                        resp = event.get("response", {})
                        cid = "chatcmpl-" + resp.get("id", "unknown")
                        model = resp.get("model", model)
                        yield chunk(cid, created, model, {"role": "assistant", "content": ""})
                    case "response.output_text.delta":
                        yield chunk(cid, created, model, {"content": event.get("delta", "")})
                    case "response.completed" | "response.incomplete":
                        resp = event.get("response", {})
                        yield chunk(cid, created, model, {}, finish_reason(resp))
                        if include_usage:
                            yield chunk(cid, created, model, {}, usage=to_usage(resp.get("usage")))
                        yield "data: [DONE]\n\n"
                        return
                    case "error" | "response.failed":
                        app.logger.warning("Rocky stream error: %s", event)
                        err = event.get("error") or event.get("response", {}).get("error") or event
                        yield f"data: {json.dumps({'error': err})}\n\n"
                        return
        yield f"data: {json.dumps({'error': {'message': 'Rocky stream ended without completion.'}})}\n\n"

    return Response(
        stream_with_context(generate()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), threaded=True)
