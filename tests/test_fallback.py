"""Local fallback: when Jev fails, a Jev-compatible local server (e.g. Polaris) answers, but it can never approve."""
import json, os, subprocess, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
JEVO = os.path.join(ROOT, "scripts", "jevo.py")
sys.path.insert(0, os.path.join(ROOT, "lib"))
import jevlib  # noqa: E402
from test_flow import APPROVE, sh  # noqa: E402

DEAD = "http://127.0.0.1:9"  # discard port: connection refused
ANSWERS = {k: v for qset in APPROVE.values() for k, v in qset.items()}


class Local(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.answer(body["questions"])

    def do_GET(self):  # what urllib turns a followed 302/303 into
        self.answer(["done"])

    def answer(self, questions):
        Local.seen.append(dict(path=self.path, auth=self.headers.get("Authorization")))
        out = json.dumps({"model": "polaris-test", "answers": {q: ANSWERS[q] for q in questions}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class FakeJev(BaseHTTPRequestHandler):
    """Plays a misbehaving Jev: `reply` is a (status, headers, body) tuple."""
    reply = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        status, headers, body = FakeJev.reply
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        data = json.dumps(body).encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Fallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Local)
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

        cls.jev = HTTPServer(("127.0.0.1", 0), FakeJev)
        cls.jev_url = "http://127.0.0.1:%d" % cls.jev.server_port
        threading.Thread(target=cls.jev.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.jev.shutdown()

    def setUp(self):
        Local.seen.clear()
        self.env = dict(os.environ)
        os.environ["TYPESAFE_API_KEY"] = "test-key"
        self.base, jevlib.BASE = jevlib.BASE, DEAD

    def tearDown(self):
        os.environ.clear(); os.environ.update(self.env)
        jevlib.BASE = self.base

    def ask(self):
        return jevlib.system_one({"x": 1}, {"done": {"type": "noul", "instructions": "?"}}, timeout=5)

    def test_jev_down_uses_local_fallback_without_the_key(self):
        os.environ["JEVO_FALLBACK_URLS"] = "%s,%s" % (DEAD, self.url)
        out = self.ask()
        self.assertEqual((out["_backend"], out["answers"]["done"]["noul"]), (self.url, 0.95))
        self.assertIn("transport", out["_jev_error"])
        self.assertEqual(jevlib.cost(dict(out, usage={"input_tokens": 1000})), 0.0)
        self.assertEqual(Local.seen, [dict(path="/v1/systemone", auth=None)])

    def test_no_key_also_falls_back(self):
        os.environ["JEVO_FALLBACK_URLS"] = self.url
        os.environ["TYPESAFE_API_KEY"] = ""
        os.environ["CLAUDE_PROJECT_DIR"] = tempfile.mkdtemp()
        pi_key, jevlib.PI_KEY_FILE = jevlib.PI_KEY_FILE, "/nonexistent"
        try:
            self.assertEqual(self.ask()["_backend"], self.url)
        finally:
            jevlib.PI_KEY_FILE = pi_key

    def test_empty_setting_disables_fallback(self):
        os.environ["JEVO_FALLBACK_URLS"] = ""
        with self.assertRaises(jevlib.JevError):
            self.ask()
        self.assertEqual(Local.seen, [])

    def test_every_backend_down_fails_closed(self):
        os.environ["JEVO_FALLBACK_URLS"] = DEAD
        with self.assertRaisesRegex(jevlib.JevError, "fallback"):
            self.ask()

    def test_default_is_local_polaris_port(self):
        os.environ.pop("JEVO_FALLBACK_URLS", None)
        self.assertEqual(jevlib.fallback_urls(), ["http://127.0.0.1:8765"])

    def test_jev_redirect_is_refused_so_key_never_follows(self):
        FakeJev.reply = (303, {"Location": self.url + "/v1/systemone"}, {})
        jevlib.BASE = self.jev_url
        os.environ["JEVO_FALLBACK_URLS"] = self.url
        out = self.ask()
        self.assertEqual((out["_backend"], out["_jev_error"]), (self.url, "jev: http_303"))
        self.assertEqual([r["auth"] for r in Local.seen], [None])

    def test_malformed_jev_answers_fall_back(self):
        os.environ["JEVO_FALLBACK_URLS"] = self.url
        jevlib.BASE = self.jev_url
        for body in ([], None, {"answers": {}}, {"answers": {"done": {"noul": "yes"}}},
                     {"answers": {"done": {"noul": True}}}, {"answers": {"done": {"noul": float("nan")}}},
                     {"answers": {"done": {"noul": 1.5}}}):
            FakeJev.reply = (200, {"Content-Type": "application/json"}, body)
            self.assertEqual(self.ask()["_backend"], self.url, body)

    def test_malformed_choice_and_score_fall_back(self):
        os.environ["JEVO_FALLBACK_URLS"] = self.url
        jevlib.BASE = self.jev_url
        qs = {"verdict": {"type": "choice", "criteria": {"approve": "a", "fix": "f"}},
              "severity": {"type": "score", "criteria": ["none", "minor", "major"]}}
        ok = {"verdict": {"choice": "approve", "confidence": 0.9}, "severity": {"score": 0.5}}
        for bad in ({"verdict": {"choice": [], "confidence": 0.9}}, {"verdict": {"choice": {}, "confidence": 0.9}},
                    {"verdict": {"choice": "ship", "confidence": 0.9}}, {"verdict": {"choice": "fix", "confidence": 2}},
                    {"severity": {"score": float("inf")}}, {"severity": {"score": 3}}):
            FakeJev.reply = (200, {}, {"answers": dict(ok, **bad)})
            self.assertEqual(jevlib.system_one({}, qs, timeout=5)["_backend"], self.url, bad)
        FakeJev.reply = (200, {}, {"answers": ok})
        self.assertEqual(jevlib.system_one({}, qs, timeout=5)["_backend"], "jev")

    def test_billed_malformed_reply_costs_even_when_everything_fails(self):
        os.environ["JEVO_FALLBACK_URLS"] = ""
        jevlib.BASE = self.jev_url
        FakeJev.reply = (200, {}, {"answers": {}, "usage": {"input_tokens": 1000}})
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import jevo
        ask = jevo.Asker()
        with self.assertRaises(jevlib.JevError):
            ask("qa", {"acceptance": "x", "evidence": "y"})
        self.assertAlmostEqual(ask.cost, 1000 * jevlib.PRICE_PER_INPUT_TOKEN)

    def test_billed_malformed_jev_reply_still_costs(self):
        os.environ["JEVO_FALLBACK_URLS"] = self.url
        jevlib.BASE = self.jev_url
        FakeJev.reply = (200, {}, {"answers": {}, "usage": {"input_tokens": 1000}})
        self.assertAlmostEqual(jevlib.cost(self.ask()), 1000 * jevlib.PRICE_PER_INPUT_TOKEN)

    def test_fallback_approval_escalates_end_to_end(self):
        repo = tempfile.mkdtemp(prefix="jevo-fb-")
        sh(repo, "git", "init", "-q", "-b", "main")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=repo, TYPESAFE_BASE_URL=DEAD, JEVO_FALLBACK_URLS=self.url)
        run = lambda *a: subprocess.run([sys.executable, JEVO, *a], cwd=repo, capture_output=True, text=True, env=env)
        run("slice", "new", "--id", "S1", "--acceptance", "add.py defines add", "--allow", "add.py", "--gate", "true")
        open(os.path.join(repo, "add.py"), "w").write("def add(a, b):\n    return a + b\n")
        p = run("check", "--slice", "S1")
        out = json.loads(p.stdout)
        self.assertEqual((p.returncode, out["decision"], out["backend"]), (3, "escalate", self.url))
        self.assertIn("frontier", out["reason"])
        row = json.loads(open(os.path.join(repo, ".jev-orchestrator", "ledger.jsonl")).read().splitlines()[-1])
        self.assertEqual((row["event"], row["backend"], row["cost_usd"]), ("check", self.url, 0.0))


if __name__ == "__main__":
    unittest.main()
