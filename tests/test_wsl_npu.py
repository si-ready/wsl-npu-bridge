#!/usr/bin/env python3
"""
Tests for client/wsl_npu.py -- stdlib unittest only, no hardware.

A fake HTTP server stands in for both host services (Lemonade at
/api/v1, FastFlowLM at /v1) so the client's parsing, routing,
streaming, retry and error behaviour are all exercised offline.
Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))

import wsl_npu  # noqa: E402
from wsl_npu import NPU, NPUServiceError, NPUUnavailable  # noqa: E402

REQUESTS: list[dict] = []


class FakeHost(BaseHTTPRequestHandler):
    """One server playing both host services."""

    def log_message(self, *args):  # keep test output clean
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, deltas):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for d in deltas:
            chunk = {"choices": [{"delta": {"content": d}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self):
        if self.path == "/api/v1/models":
            self._json({"data": [
                {"id": "Llama-3.2-1B-Instruct-Hybrid"},
                {"id": "Whisper-Large-v3-Turbo"},
                {"id": "nomic-embed-text-v1-GGUF"},
            ]})
        elif self.path == "/v1/models":
            self._json({"data": [{"id": "llama3.2:1b"},
                                 {"id": "qwen3:0.6b"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        REQUESTS.append({"path": self.path, "body": raw,
                         "ctype": self.headers.get("Content-Type", "")})
        if self.path == "/api/v1/audio/transcriptions":
            self._json({"text": "fake transcript"})
            return
        payload = json.loads(raw or b"{}")
        if payload.get("model") == "boom":
            self._json({"error": {"message": "kaboom"}}, 500)
            return
        if self.path == "/api/v1/chat/completions":
            if payload.get("stream"):
                self._sse(["Hello", " world"])
            else:
                self._json({
                    "choices": [{"message": {"content": "fake reply"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                })
        elif self.path == "/v1/chat/completions":
            if payload.get("stream"):
                self._sse(["flm", " stream"])
            else:
                self._json({
                    "choices": [{"message": {"content": "flm reply"}}],
                    "usage": {"prompt_tokens": 51, "completion_tokens": 76,
                              "prefill_speed_tps": 103.2,
                              "decoding_speed_tps": 55.1,
                              "prefill_duration_ttft": 0.494},
                })
        elif self.path == "/api/v1/embeddings":
            self._json({"data": [{"embedding": [0.1, 0.2, 0.3]}]})
        else:
            self._json({"error": "not found"}, 404)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHost)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.npu = NPU(base_url=cls.base, flm_url=cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        REQUESTS.clear()

    # -- basics ---------------------------------------------------------
    def test_models(self):
        self.assertIn("Whisper-Large-v3-Turbo", self.npu.models())

    def test_chat_lemond(self):
        self.assertEqual(self.npu.chat("hi"), "fake reply")

    def test_chat_flm_default_model_substitution(self):
        # engine="flm" with the lemond default model must substitute
        # the FLM default tag rather than sending a nonsense model.
        self.assertEqual(self.npu.chat("hi", engine="flm"), "flm reply")
        sent = json.loads(REQUESTS[-1]["body"])
        self.assertEqual(sent["model"], wsl_npu.DEFAULT_FLM_MODEL)
        self.assertEqual(REQUESTS[-1]["path"], "/v1/chat/completions")

    def test_chat_timed_lemond(self):
        out = self.npu.chat_timed("hi")
        self.assertEqual(out["engine"], "lemond")
        self.assertEqual(out["completion_tokens"], 5)
        self.assertIsNotNone(out["tokens_per_s"])

    def test_chat_timed_flm_carries_engine_speeds(self):
        out = self.npu.chat_timed("hi", engine="flm")
        self.assertEqual(out["engine"], "flm")
        self.assertEqual(out["prefill_tps"], 103.2)
        self.assertEqual(out["decode_tps"], 55.1)
        self.assertEqual(out["ttft_s"], 0.494)

    def test_chat_stream_lemond(self):
        text = "".join(self.npu.chat_stream("hi"))
        self.assertEqual(text, "Hello world")

    def test_chat_stream_flm(self):
        text = "".join(self.npu.chat_stream("hi", engine="flm"))
        self.assertEqual(text, "flm stream")

    def test_transcribe_sends_multipart_with_filename(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False) as f:
            f.write(b"RIFF-fake")
            name = f.name
        try:
            self.assertEqual(self.npu.transcribe(name), "fake transcript")
        finally:
            Path(name).unlink()
        req = REQUESTS[-1]
        self.assertIn("multipart/form-data", req["ctype"])
        self.assertIn(Path(name).name.encode(), req["body"])
        self.assertIn(b"Whisper-Large-v3-Turbo", req["body"])

    def test_embed(self):
        self.assertEqual(self.npu.embed("x"), [0.1, 0.2, 0.3])

    def test_flm_models(self):
        self.assertEqual(self.npu.flm_models(), ["llama3.2:1b", "qwen3:0.6b"])

    # -- error behaviour --------------------------------------------------
    def test_http_500_raises_service_error_without_retry(self):
        with self.assertRaises(NPUServiceError) as ctx:
            self.npu.chat("hi", model="boom")
        self.assertIn("kaboom", str(ctx.exception))
        hits = [r for r in REQUESTS if r["path"].endswith("chat/completions")]
        self.assertEqual(len(hits), 1)  # a server verdict is never retried

    def test_dead_endpoint_raises_unavailable(self):
        dead = NPU.__new__(NPU)
        dead.base_url = "http://127.0.0.1:1"  # nothing listens there
        dead.flm_url = dead.base_url
        with self.assertRaises(NPUUnavailable):
            dead.models()

    def test_discovery_failure_message_lists_candidates(self):
        original = NPU._healthy
        NPU._healthy = staticmethod(lambda base: False)
        try:
            with self.assertRaises(NPUUnavailable) as ctx:
                NPU._discover()
        finally:
            NPU._healthy = original
        self.assertIn("Tried:", str(ctx.exception))

    # -- doctor -------------------------------------------------------------
    def test_doctor_structure(self):
        report = wsl_npu.doctor(base_url=self.base, flm_url=self.base)
        self.assertEqual(report["lemond"]["models_known"], 3)
        self.assertEqual(report["lemond"]["defaults"]["llm"], "ready")
        self.assertEqual(report["flm"]["models_listed"], 2)


class CandidateUrlTests(unittest.TestCase):
    def test_env_override_wins(self):
        import os
        old = os.environ.get("WSL_NPU_URL")
        os.environ["WSL_NPU_URL"] = "http://example.invalid:9999/"
        try:
            urls = wsl_npu._candidate_base_urls()
        finally:
            if old is None:
                del os.environ["WSL_NPU_URL"]
            else:
                os.environ["WSL_NPU_URL"] = old
        self.assertEqual(urls[0], "http://example.invalid:9999")
        self.assertIn(f"http://127.0.0.1:{wsl_npu.DEFAULT_PORT}", urls)


if __name__ == "__main__":
    unittest.main()
