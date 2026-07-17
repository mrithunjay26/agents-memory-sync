import json
import unittest
from unittest.mock import patch

import dispatch


class _FakeResponse:
    def __init__(self, lines, status_code=200):
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b"boom"


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self._status = status_code

    def stream(self, method, url, headers=None, json=None):
        return _FakeStream(_FakeResponse(self._lines, self._status))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _sse(*events):
    lines = []
    for event in events:
        lines.append("data: " + json.dumps(event))
    lines.append("data: [DONE]")
    return lines


class DispatchLocalOpenAITestCase(unittest.IsolatedAsyncioTestCase):
    async def test_streams_deltas_into_final_result_with_measured_usage(self):
        events = [
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": ", world"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
        ]
        logs = []
        with patch("httpx.AsyncClient", return_value=_FakeClient(_sse(*events))):
            result, tokens = await dispatch.dispatch_local_openai(
                "http://localhost:11434", "gemma3:4b", {}, "hi", logs.append
            )
        self.assertEqual(result, "Hello, world")
        self.assertEqual(tokens, 13)

    async def test_estimates_tokens_when_endpoint_omits_usage(self):
        events = [{"choices": [{"delta": {"content": "short reply"}}]}]
        with patch("httpx.AsyncClient", return_value=_FakeClient(_sse(*events))):
            result, tokens = await dispatch.dispatch_local_openai(
                "http://localhost:11434", "gemma3:4b", {}, "hi", lambda _e: None
            )
        self.assertEqual(result, "short reply")
        self.assertEqual(tokens, (len("short reply") + 3) // 4)

    async def test_non_200_response_raises_with_body_preview(self):
        with patch("httpx.AsyncClient", return_value=_FakeClient([], status_code=500)):
            with self.assertRaisesRegex(RuntimeError, "500"):
                await dispatch.dispatch_local_openai(
                    "http://localhost:11434", "gemma3:4b", {}, "hi", lambda _e: None
                )


if __name__ == "__main__":
    unittest.main()
