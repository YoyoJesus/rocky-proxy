"""Local proxy that exposes Kent State's Rocky API as OpenAI Chat Completions."""

import json
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

from secret_env import load_secret

ROCKY_BASE = os.environ.get("ROCKY_BASE", "https://rocky.cs.kent.edu/v1")
ROCKY_KEY = load_secret("ROCKY_API_KEY")
TIMEOUT = 600
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_DELAY = 60
KEEPALIVE = 15
STARTED = ("response.output_text.delta", "response.completed", "response.incomplete")

PARAM_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "frequency_penalty": "frequency_penalty",
    "presence_penalty": "presence_penalty",
    "max_tokens": "max_output_tokens",
    "max_completion_tokens": "max_output_tokens",
}

TOOL_TAG = "<tool_call>"
TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
TOOL_PROMPT = """# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tools}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{{"name": <function-name>, "arguments": <args-json-object>}}
</tool_call>
Write any text before the calls. After the calls, stop and wait for the results in <tool_response> tags."""

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


def tool_call_text(call: dict) -> str:
    fn = call.get("function", {})
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        args = fn.get("arguments")
    return f"{TOOL_TAG}\n{json.dumps({'name': fn.get('name'), 'arguments': args})}\n</tool_call>"


def convert_message(msg: dict) -> dict:
    role = msg.get("role")
    content = msg.get("content")
    if role == "tool":
        return {"role": "user", "content": f"<tool_response>\n{text_of(content)}\n</tool_response>"}
    if role not in ("system", "developer", "user", "assistant"):
        raise ValueError(f"Rocky does not support '{role}' messages.")
    if role == "assistant" and msg.get("tool_calls"):
        parts = [text_of(content)] + [tool_call_text(c) for c in msg["tool_calls"]]
        return {"role": role, "content": "\n".join(p for p in parts if p)}
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
    if body.get("functions"):
        raise ValueError("Legacy 'functions' are not supported; send 'tools' instead.")
    if body.get("n", 1) != 1:
        raise ValueError("Only n=1 is supported.")

    messages = [convert_message(m) for m in body.get("messages", [])]
    if not any(m["role"] == "user" for m in messages):
        raise ValueError("At least one user message is required.")

    # Rocky has no native tool calling, so tools are described in the prompt and parsed back out of the text.
    if tools := [t["function"] for t in body.get("tools") or [] if t.get("type") == "function"]:
        prompt = TOOL_PROMPT.format(tools="\n".join(json.dumps(t) for t in tools))
        if messages[0]["role"] in ("system", "developer"):
            messages[0]["content"] = f"{messages[0]['content']}\n\n{prompt}"
        else:
            messages.insert(0, {"role": "system", "content": prompt})

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


def parse_tool_calls(text: str) -> tuple[str, list[dict]]:
    calls = []
    for m in TOOL_CALL.finditer(text):
        try:
            data = json.loads(m.group(1))
            name, args = data["name"], data.get("arguments", {})
        except (ValueError, KeyError, TypeError):
            continue
        calls.append(
            {
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)},
            }
        )
    if not calls:
        return text, []
    return text[: text.find(TOOL_TAG)].strip(), calls


def safe_len(text: str) -> int:
    """Length of text that can be streamed without risking part of a <tool_call> tag."""
    if (i := text.find(TOOL_TAG)) >= 0:
        return i
    for k in range(min(len(TOOL_TAG) - 1, len(text)), 0, -1):
        if TOOL_TAG.startswith(text[-k:]):
            return len(text) - k
    return len(text)


def finish_reason(resp: dict) -> str:
    return "length" if resp.get("status") == "incomplete" else "stop"


def upstream_payload(r: requests.Response) -> dict:
    try:
        payload = r.json()
    except ValueError:
        payload = {"error": {"message": r.text or r.reason, "type": "upstream_error", "code": str(r.status_code)}}
    app.logger.warning("Rocky %s (x-request-id=%s): %s", r.status_code, r.headers.get("x-request-id"), payload)
    return payload


def upstream_error(r: requests.Response) -> tuple[Response, int]:
    return jsonify(upstream_payload(r)), r.status_code


def event_error(event: dict) -> dict:
    return event.get("error") or event.get("response", {}).get("error") or event


def should_retry(r: requests.Response) -> bool:
    if r.status_code in RETRY_STATUS:
        return True
    try:
        return r.json()["error"]["code"] == "model_busy"
    except (ValueError, KeyError, TypeError):
        return False


def retry_delay(attempt: int) -> int:
    return min(5 * 2**attempt, MAX_RETRY_DELAY)


def stream_events(rocky_body: dict, out: queue.Queue, stop: threading.Event) -> None:
    """Put Rocky stream events on `out`, retrying busy, rate-limited or dropped attempts until output starts.

    Retries continue while the client is connected (`stop` unset). None marks the end.
    """
    attempt, started = 0, False
    try:
        while not stop.is_set():
            reason = ""
            try:
                with requests.post(
                    f"{ROCKY_BASE}/responses", headers=rocky_headers(), json=rocky_body, stream=True, timeout=TIMEOUT
                ) as r:
                    if not r.ok:
                        if not should_retry(r):
                            out.put({"type": "error", "error": upstream_payload(r).get("error")})
                            return
                        reason = f"HTTP {r.status_code}"
                    else:
                        head = []
                        for event in iter_sse(r):
                            if started:
                                out.put(event)
                            elif event.get("type") in ("error", "response.failed") and (
                                event_error(event).get("code") == "model_busy"
                            ):
                                reason = "model_busy"
                                break
                            elif event.get("type") in STARTED:
                                started = True
                                for e in [*head, event]:
                                    out.put(e)
                            else:
                                head.append(event)
                        if not reason:
                            for e in head if not started else []:
                                out.put(e)
                            return
            except requests.RequestException as e:
                if started:
                    out.put({"type": "error", "error": {"message": f"Rocky connection lost: {e}"}})
                    return
                reason = repr(e)
            delay = retry_delay(attempt)
            attempt += 1
            app.logger.warning("Rocky unavailable (%s), retry %d in %ds", reason, attempt, delay)
            stop.wait(delay)
    finally:
        out.put(None)


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
    use_tools = bool(body.get("tools"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    rocky_body["stream"] = stream

    if not stream:
        # shortcut: buffered requests cannot detect a client disconnect, so retries are capped; streams retry forever.
        for attempt in range(10):
            r = requests.post(f"{ROCKY_BASE}/responses", headers=rocky_headers(), json=rocky_body, timeout=TIMEOUT)
            if r.ok or not should_retry(r):
                break
            app.logger.warning("Rocky unavailable (HTTP %s), retry %d", r.status_code, attempt + 1)
            time.sleep(retry_delay(attempt))
        if not r.ok:
            return upstream_error(r)
        resp = r.json()
        text, calls = parse_tool_calls(resp.get("output_text", "")) if use_tools else (resp.get("output_text", ""), [])
        message: dict[str, Any] = {"role": "assistant", "content": text or (None if calls else "")}
        if calls:
            message["tool_calls"] = calls
        return jsonify(
            {
                "id": "chatcmpl-" + resp["id"],
                "object": "chat.completion",
                "created": resp.get("created_at", int(time.time())),
                "model": resp.get("model", body["model"]),
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if calls else finish_reason(resp),
                    }
                ],
                "usage": to_usage(resp.get("usage")),
            }
        )

    def generate() -> Iterator[str]:
        cid, created, model = "chatcmpl-pending", int(time.time()), body["model"]
        text, sent = "", 0
        events: queue.Queue = queue.Queue()
        stop = threading.Event()
        threading.Thread(target=stream_events, args=(rocky_body, events, stop), daemon=True).start()
        try:
            while True:
                try:
                    event = events.get(timeout=KEEPALIVE)
                except queue.Empty:
                    # Comments keep the connection alive while Rocky is busy; a failed write means the client left.
                    yield ": waiting for Rocky\n\n"
                    continue
                if event is None:
                    break
                match event.get("type"):
                    case "response.created":
                        resp = event.get("response", {})
                        cid = "chatcmpl-" + resp.get("id", "unknown")
                        model = resp.get("model", model)
                        yield chunk(cid, created, model, {"role": "assistant", "content": ""})
                    case "response.output_text.delta":
                        if not use_tools:
                            yield chunk(cid, created, model, {"content": event.get("delta", "")})
                            continue
                        text += event.get("delta", "")
                        if (n := safe_len(text)) > sent:
                            yield chunk(cid, created, model, {"content": text[sent:n]})
                            sent = n
                    case "response.completed" | "response.incomplete":
                        resp = event.get("response", {})
                        _, calls = parse_tool_calls(text) if use_tools else ("", [])
                        if calls:
                            delta = {"tool_calls": [{"index": i, **c} for i, c in enumerate(calls)]}
                            yield chunk(cid, created, model, delta)
                            yield chunk(cid, created, model, {}, "tool_calls")
                        else:
                            if sent < len(text):
                                yield chunk(cid, created, model, {"content": text[sent:]})
                            yield chunk(cid, created, model, {}, finish_reason(resp))
                        if include_usage:
                            yield chunk(cid, created, model, {}, usage=to_usage(resp.get("usage")))
                        yield "data: [DONE]\n\n"
                        return
                    case "error" | "response.failed":
                        app.logger.warning("Rocky stream error: %s", event)
                        yield f"data: {json.dumps({'error': event_error(event)})}\n\n"
                        return
        finally:
            stop.set()
        yield f"data: {json.dumps({'error': {'message': 'Rocky stream ended without completion.'}})}\n\n"

    return Response(
        stream_with_context(generate()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), threaded=True)
