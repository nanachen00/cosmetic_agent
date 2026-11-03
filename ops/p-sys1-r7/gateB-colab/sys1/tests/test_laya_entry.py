"""laya_entry.Adapter on a fake ASGI app (no laya, no uvicorn, no port).  Run from gateB-colab/:  python3 sys1/tests/test_laya_entry.py"""
import asyncio, json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import laya_entry as LE  # noqa: E402


def fake_laya(headers):
    async def app(scope, receive, send):
        body = json.dumps({"model": "multilingual", "answers": {"safety": {"type": "noul", "noul": 0.9}}, "usage": {"input_tokens": 3}}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())] + headers})
        await send({"type": "http.response.body", "body": body[:10], "more_body": True})
        await send({"type": "http.response.body", "body": body[10:]})
    return app


def call(app, method, path):
    out = []
    async def receive(): return {"type": "http.request", "body": b"{}", "more_body": False}
    async def send(m): out.append(m)
    asyncio.run(app({"type": "http", "method": method, "path": path, "headers": []}, receive, send))
    start = out[0]; body = b"".join(m.get("body", b"") for m in out[1:])
    return start["status"], dict((k.lower(), v) for k, v in start["headers"]), body


class Adapter(unittest.TestCase):
    def test_models_answered_locally(self):
        st, h, b = call(LE.Adapter(fake_laya([]), ["multilingual", "typed-decisions"], {"backend": "laya"}), "GET", "/v1/models")
        self.assertEqual(st, 200); self.assertEqual([m["name"] for m in json.loads(b)["models"]], ["multilingual", "typed-decisions"])
        self.assertEqual(int(h[b"content-length"]), len(b))

    def test_latency_from_header(self):
        for hdr, want in (([(b"X-Inference-Time-Ms", b"12.5")], 12.5), ([(b"server-timing", b"inference;dur=7.25")], 7.25)):
            st, h, b = call(LE.Adapter(fake_laya(hdr), ["multilingual"]), "POST", "/v1/systemone")
            j = json.loads(b); self.assertEqual(j["latency_ms"], want); self.assertEqual(j["answers"]["safety"]["noul"], 0.9)
            self.assertEqual(int(h[b"content-length"]), len(b))

    def test_no_header_leaves_body(self):
        st, h, b = call(LE.Adapter(fake_laya([]), ["multilingual"]), "POST", "/v1/systemone")
        self.assertNotIn("latency_ms", json.loads(b))

    def test_other_paths_pass_through(self):
        st, h, b = call(LE.Adapter(fake_laya([(b"x-inference-time-ms", b"3")]), ["m"]), "POST", "/v1/route")
        self.assertNotIn("latency_ms", json.loads(b))

    def test_port_guard(self):
        for p in ("8000", "8231x", "8240", "8010"):
            with self.assertRaises(SystemExit): LE.main(["--port", p])


if __name__ == "__main__":
    unittest.main(verbosity=1)
