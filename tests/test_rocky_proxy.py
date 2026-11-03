import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ROCKY_API_KEY", "test")
import rocky_proxy  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]


def sse(*events: dict) -> list[str]:
    return [line for e in events for line in (f"data: {json.dumps(e)}", "")]


class RequestTests(unittest.TestCase):
    def test_tools_go_into_system_prompt(self):
        messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
        out = rocky_proxy.build_rocky_request({"model": "m", "tools": TOOLS, "messages": messages})
        self.assertNotIn("tools", out)
        self.assertTrue(out["input"][0]["content"].startswith("Be brief.\n\n# Tools"))
        self.assertIn('"name": "read"', out["input"][0]["content"])

    def test_tool_history_becomes_text(self):
        call = {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}
        out = rocky_proxy.build_rocky_request(
            {
                "model": "m",
                "tools": TOOLS,
                "messages": [
                    {"role": "user", "content": "open a.py"},
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": "c1", "content": "print(1)"},
                ],
            }
        )
        self.assertEqual(out["input"][0]["role"], "system")
        self.assertEqual(
            out["input"][2]["content"], '<tool_call>\n{"name": "read", "arguments": {"path": "a.py"}}\n</tool_call>'
        )
        self.assertEqual(out["input"][3], {"role": "user", "content": "<tool_response>\nprint(1)\n</tool_response>"})


class ParseTests(unittest.TestCase):
    def test_parses_calls_and_leading_text(self):
        text, calls = rocky_proxy.parse_tool_calls(
            'Reading.\n<tool_call>\n{"name": "read", "arguments": {"path": "a"}}\n</tool_call>\n'
            '<tool_call>{"name": "ls", "arguments": {}}'  # unclosed final tag
        )
        self.assertEqual(text, "Reading.")
        self.assertEqual([c["function"]["name"] for c in calls], ["read", "ls"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "a"})

    def test_bad_json_stays_text(self):
        raw = "<tool_call>not json</tool_call>"
        self.assertEqual(rocky_proxy.parse_tool_calls(raw), (raw, []))

    def test_safe_len_holds_partial_tag(self):
        self.assertEqual(rocky_proxy.safe_len("hello <tool"), 6)
        self.assertEqual(rocky_proxy.safe_len("a <tool_call> b"), 2)
        self.assertEqual(rocky_proxy.safe_len("a < b"), 5)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = rocky_proxy.app.test_client()

    def post(self, *upstream: MagicMock, **body):
        for up in upstream:
            up.__enter__.return_value = up
        with (
            patch.object(rocky_proxy.requests, "post", side_effect=upstream) as post,
            patch.object(rocky_proxy, "retry_delay", return_value=0),
        ):
            res = self.client.post("/v1/chat/completions", json={"model": "m", "tools": TOOLS, **body})
            res.get_data()  # streams call Rocky lazily, so read while the mock is active
        self.calls = post.call_count
        return res

    def test_buffered_tool_call(self):
        up = MagicMock(ok=True)
        up.json.return_value = {"id": "r1", "output_text": '<tool_call>{"name": "read", "arguments": {}}</tool_call>'}
        choice = self.post(up, messages=[{"role": "user", "content": "x"}]).get_json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "read")

    def test_streamed_tool_call(self):
        up = MagicMock(ok=True)
        up.iter_lines.return_value = sse(
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "response.output_text.delta", "delta": "Ok. <tool"},
            {"type": "response.output_text.delta", "delta": '_call>{"name": "read", "arguments": {}}</tool_call>'},
            {"type": "response.completed", "response": {}},
        )
        body = self.post(up, stream=True, messages=[{"role": "user", "content": "x"}]).get_data(as_text=True)
        chunks = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
        content = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        self.assertEqual(content, "Ok. ")
        self.assertEqual(chunks[-2]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "read")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_busy_stream_retries_until_output(self):
        busy = MagicMock(ok=True)
        busy.iter_lines.return_value = sse(
            {"type": "response.created", "response": {"id": "r1"}},
            {"type": "error", "code": "model_busy", "message": "The model is busy."},
        )
        limited = MagicMock(ok=False, status_code=429)
        limited.json.return_value = {"error": {"message": "slow down"}}
        ok = MagicMock(ok=True)
        ok.iter_lines.return_value = sse(
            {"type": "response.created", "response": {"id": "r2"}},
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed", "response": {}},
        )
        res = self.post(busy, limited, ok, stream=True, messages=[{"role": "user", "content": "x"}])
        lines = res.get_data(as_text=True).splitlines()
        chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
        self.assertEqual(self.calls, 3)
        self.assertEqual(sum(1 for c in chunks if c["choices"] and c["choices"][0]["delta"].get("role")), 1)
        self.assertEqual("".join(c["choices"][0]["delta"].get("content") or "" for c in chunks), "hi")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_buffered_retries_busy(self):
        busy = MagicMock(ok=False, status_code=503)
        ok = MagicMock(ok=True)
        ok.json.return_value = {"id": "r1", "output_text": "hi"}
        res = self.post(busy, ok, messages=[{"role": "user", "content": "x"}])
        self.assertEqual(self.calls, 2)
        self.assertEqual(res.get_json()["choices"][0]["message"]["content"], "hi")

    def test_client_error_is_not_retried(self):
        bad = MagicMock(ok=False, status_code=400)
        bad.json.return_value = {"error": {"message": "bad", "code": "invalid"}}
        res = self.post(bad, stream=True, messages=[{"role": "user", "content": "x"}])
        self.assertEqual(self.calls, 1)
        self.assertIn('"message": "bad"', res.get_data(as_text=True))

    def test_streamed_plain_text_with_tools(self):
        up = MagicMock(ok=True)
        up.iter_lines.return_value = sse(
            {"type": "response.output_text.delta", "delta": "a <"},
            {"type": "response.completed", "response": {}},
        )
        body = self.post(up, stream=True, messages=[{"role": "user", "content": "x"}]).get_data(as_text=True)
        chunks = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
        self.assertEqual("".join(c["choices"][0]["delta"].get("content") or "" for c in chunks), "a <")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
