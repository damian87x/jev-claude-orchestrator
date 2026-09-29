"""Closeout regressions F1-F4. No network: OPENER, sleep and the ledger are mocked."""
import http.client, io, json, os, re, ssl, sys, unittest, urllib.error
from email.message import Message
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "lib"), os.path.join(ROOT, "scripts")]
import jevlib, jevo, stages  # noqa: E402

FALLBACK = "http://fallback.invalid"
Q = {"q": {"type": "noul"}}
GOOD = b'{"answers": {"q": {"noul": 0.5}}}'


class Resp:
    def __init__(self, data=GOOD, exc=None):
        self.data, self.exc = data, exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        if self.exc:
            raise self.exc
        return self.data


def http_429(retry_after):
    h = Message()
    h["retry-after"] = retry_after
    return urllib.error.HTTPError("http://x", 429, "slow", h, io.BytesIO(b""))


def disconnect(n):
    raise http.client.RemoteDisconnected("gone")


class Harness(unittest.TestCase):
    def run_system_one(self, primary):
        """primary(attempt) -> Resp, or raises. Returns (result, primary_calls, fallback_calls, sleeps, fallback_auth)."""
        calls = {"p": 0, "f": 0}
        auth, sleeps = [], []

        def opener(req, timeout=None):
            if req.full_url.startswith(FALLBACK):
                calls["f"] += 1
                auth.append(req.get_header("Authorization"))
                return Resp()
            calls["p"] += 1
            return primary(calls["p"])

        def sleep(s):
            if not 0 <= s <= 30:
                raise AssertionError("unsafe sleep %r" % s)
            sleeps.append(s)

        with mock.patch.object(jevlib.OPENER, "open", opener), mock.patch.object(jevlib, "api_key", lambda: "k"), \
                mock.patch.object(jevlib, "fallback_urls", lambda: [FALLBACK]), \
                mock.patch.object(jevlib.time, "sleep", sleep):
            res = jevlib.system_one({}, Q, timeout=30, qset="triage")
        return res, calls["p"], calls["f"], sleeps, auth


class F1(unittest.TestCase):
    def test_product_scope_skip_top_model_fallback(self):
        text = open(os.path.join(ROOT, "skills", "conductor-max", "SKILL.md")).read()
        flat = re.sub(r"\s+", " ", text)
        self.assertIn("`needs_human`, product, scope, security and authority escalations "
                      "skip the fallback and go straight to the human.", flat)


class M2(unittest.TestCase):
    def test_exit_2_wording_covers_disallowed_stage(self):
        text = open(os.path.join(ROOT, "skills", "conductor-max", "SKILL.md")).read()
        flat = re.sub(r"\s+", " ", text)
        self.assertNotIn("Only when no backend answers", flat)
        self.assertIn("exits 2 when Jev fails and the stage is not allowed a fallback "
                      "(QA and review by default) or no fallback answers", flat)


class M1(unittest.TestCase):
    """cost() must treat unusable usage metadata as zero, so a valid fallback answer is never discarded."""
    BAD = ["1000", True, -1, float("nan"), float("inf"), None, [5], 10 ** 400, {"x": 1}]

    def test_unusable_usage_is_zero_cost(self):
        for v in self.BAD:
            with self.subTest(v=repr(v)[:20]):
                self.assertEqual(jevlib.cost({"usage": {"input_tokens": v}}), 0.0)
                self.assertEqual(jevlib.cost({"_backend": FALLBACK, "_jev_usage": {"input_tokens": v}}), 0.0)
        for res in [{"usage": "abc"}, {"usage": None}, {"usage": [1]}, {}, {"_backend": FALLBACK, "_jev_usage": "x"}]:
            self.assertEqual(jevlib.cost(res), 0.0)

    def test_valid_usage_cost_kept(self):
        self.assertAlmostEqual(jevlib.cost({"usage": {"input_tokens": 1000}}), 1000 * jevlib.PRICE_PER_INPUT_TOKEN)
        self.assertAlmostEqual(jevlib.cost({"_backend": FALLBACK, "_jev_usage": {"input_tokens": 2.0}}),
                               2.0 * jevlib.PRICE_PER_INPUT_TOKEN)

    def ask(self, v, fallback_ok=True):
        def opener(req, timeout=None):
            if req.full_url.startswith(FALLBACK):
                if not fallback_ok:
                    raise urllib.error.URLError("down")
                return Resp()
            return Resp(json.dumps({"answers": {"q": {"noul": "bad"}}, "usage": {"input_tokens": v}}).encode())
        with mock.patch.object(jevlib.OPENER, "open", opener), mock.patch.object(jevlib, "api_key", lambda: "k"), \
                mock.patch.object(jevlib, "fallback_urls", lambda: [FALLBACK]), \
                mock.patch.dict(stages.QUESTIONS, {"triage": Q}):
            ask = jevo.Asker()
            return ask, ask("triage", {})

    def test_asker_keeps_fallback_answer_and_backend(self):
        for v in self.BAD:
            with self.subTest(v=repr(v)[:20]):
                ask, answers = self.ask(v)
                self.assertEqual((answers, ask.backend, ask.cost), ({"q": {"noul": 0.5}}, FALLBACK, 0.0))

    def test_all_backends_failing_still_raises_jev_error(self):
        for v in self.BAD:
            with self.subTest(v=repr(v)[:20]):
                with self.assertRaises(jevlib.JevError):
                    self.ask(v, fallback_ok=False)


class F2(Harness):
    def test_unusable_retry_after_reaches_fallback_without_sleep(self):
        for v in ["Wed, 21 Oct 2015 07:28:00 GMT", "garbage", "-1", "nan", "inf", "1e309", "999999999"]:
            with self.subTest(retry_after=v):
                def primary(n, v=v):
                    raise http_429(v)
                res, p, f, sleeps, auth = self.run_system_one(primary)
                self.assertEqual(res["_backend"], FALLBACK)
                self.assertEqual((p, f, sleeps, auth), (1, 1, [], [None]))

    def test_numeric_retry_after_still_retries(self):
        def primary(n):
            if n < 3:
                raise http_429("0.25")
            return Resp()
        res, p, f, sleeps, _ = self.run_system_one(primary)
        self.assertEqual((res["_backend"], p, f, sleeps), ("jev", 3, 0, [0.25, 0.25]))


class F3(Harness):
    def test_response_failure_reaches_fallback(self):
        cases = {"remote_disconnected": disconnect,
                 "incomplete_read": lambda n: Resp(exc=http.client.IncompleteRead(b"x")),
                 "os_error": lambda n: Resp(exc=OSError("read failed")),
                 "connection_reset": lambda n: Resp(exc=ConnectionResetError("reset")),
                 "ssl_error": lambda n: Resp(exc=ssl.SSLError("TLS read failed"))}
        for name, primary in cases.items():
            with self.subTest(name):
                res, p, f, _, auth = self.run_system_one(primary)
                self.assertEqual((res["_backend"], p, f, auth), (FALLBACK, 1, 1, [None]))


class MalformedURLs(unittest.TestCase):
    def setUp(self):
        self.opener = self.enterContext(mock.patch.object(jevlib.OPENER, "open", return_value=Resp()))
        self.enterContext(mock.patch.object(jevlib, "api_key", return_value="k"))
        self.enterContext(mock.patch.dict(os.environ, {"JEVO_FALLBACK_STAGES": "triage"}))

    def test_malformed_primary_reaches_valid_fallback(self):
        with mock.patch.object(jevlib, "BASE", "missing-scheme"), \
                mock.patch.object(jevlib, "fallback_urls", return_value=[FALLBACK]):
            res = jevlib.system_one({}, Q, qset="triage")
        self.assertEqual(res["_backend"], FALLBACK)
        self.assertEqual(res["_jev_error"], "jev: transport: ValueError")
        self.opener.assert_called_once()
        req = self.opener.call_args.args[0]
        self.assertEqual(req.full_url, FALLBACK + "/v1/systemone")
        self.assertIsNone(req.get_header("Authorization"))

    def test_malformed_first_fallback_reaches_valid_second(self):
        self.opener.side_effect = [urllib.error.URLError("down"), Resp()]
        with mock.patch.object(jevlib, "BASE", "https://primary.invalid"), \
                mock.patch.object(jevlib, "fallback_urls", return_value=["http://[broken", FALLBACK]):
            res = jevlib.system_one({}, Q, qset="triage")
        self.assertEqual(res["_backend"], FALLBACK)
        requests = [call.args[0] for call in self.opener.call_args_list]
        self.assertEqual([req.full_url for req in requests],
                         ["https://primary.invalid/v1/systemone", FALLBACK + "/v1/systemone"])
        self.assertEqual([req.get_header("Authorization") for req in requests], ["Bearer k", None])

    def test_all_malformed_urls_raise_jev_error(self):
        with mock.patch.object(jevlib, "BASE", "missing-scheme"), \
                mock.patch.object(jevlib, "fallback_urls", return_value=["http://[broken", "also-missing-scheme"]):
            with self.assertRaises(jevlib.JevError) as caught:
                jevlib.system_one({}, Q, qset="triage")
        self.assertEqual(str(caught.exception),
                         "jev: transport: ValueError; fallback http://[broken: transport: ValueError; "
                         "fallback also-missing-scheme: transport: ValueError")
        self.opener.assert_not_called()


class N2(unittest.TestCase):
    """A tier answer without usable probabilities must fail over to the fallback, not crash stages.triage."""
    TRIAGE = stages.QUESTIONS["triage"]
    GOOD_P = {"fast": 0.8, "balanced": 0.1, "reasoning": 0.1}

    def answers(self, probs):
        ans = {"tier": {"choice": "fast", "confidence": 0.9}, "risk": {"score": 0},
               "needs_human": {"noul": 0.0}, "shared_surface": {"noul": 0.0}}
        if probs is not None:
            ans["tier"]["probabilities"] = probs
        return ans

    def run_triage(self, primary_probs):
        auth = []

        def opener(req, timeout=None):
            fb = req.full_url.startswith(FALLBACK)
            auth.append((fb, req.get_header("Authorization")))
            return Resp(json.dumps({"answers": self.answers(self.GOOD_P if fb else primary_probs)}).encode())
        with mock.patch.object(jevlib.OPENER, "open", opener), mock.patch.object(jevlib, "api_key", lambda: "k"), \
                mock.patch.object(jevlib, "fallback_urls", lambda: [FALLBACK]):
            res = jevlib.system_one({}, self.TRIAGE, timeout=30, qset="triage")
        return res, auth

    def test_unusable_probabilities_reach_fallback(self):
        nan, inf = float("nan"), float("inf")
        bad = {"missing": None, "list": [0.8], "empty": {}, "incomplete": {"fast": 0.9}, "string": "fast",
               "bool": {"fast": True, "balanced": 0.1, "reasoning": 0.1}, "nan": {"fast": nan, "balanced": 0.1, "reasoning": 0.1},
               "inf": {"fast": inf, "balanced": 0.1, "reasoning": 0.1}, "negative": {"fast": -0.1, "balanced": 0.5, "reasoning": 0.5},
               "above_one": {"fast": 1.5, "balanced": 0.1, "reasoning": 0.1},
               "all_below_floor": {"fast": 0.2, "balanced": 0.2, "reasoning": 0.2}}
        for name, probs in bad.items():
            with self.subTest(name):
                res, auth = self.run_triage(probs)
                self.assertEqual(res["_backend"], FALLBACK)
                self.assertEqual(auth, [(False, "Bearer k"), (True, None)])
                self.assertEqual(stages.triage({"goal": "g"}, lambda q, s: res["answers"])["tier"], "fast")

    def test_valid_probabilities_stay_on_jev(self):
        res, auth = self.run_triage(self.GOOD_P)
        self.assertEqual((res["_backend"], len(auth)), ("jev", 1))


class F4(unittest.TestCase):
    def watch_rows(self, answers):
        rows = []

        def ask(qset, state):
            if answers is None:
                raise jevlib.JevError("boom")
            return answers[qset]
        ask.cost, ask.stub, ask.backend = 0.0, None, "jev"
        with mock.patch.object(jevo, "load_slice", lambda i: {"acceptance": "a", "status": "new"}), \
                mock.patch.object(jevo, "find_transcript", lambda a: None), \
                mock.patch.object(jevo.jevlib, "state_dir", lambda: "/nonexistent-jev-state"), \
                mock.patch.object(jevo.jevlib, "log", lambda ev, **f: rows.append(dict(event=ev, **f))), \
                mock.patch.object(jevo.time, "sleep", lambda s: None):
            jevo.watch("S1", "A1", ask, limit=0, extend=0, poll=0)
        return rows

    def test_watch_logs_backend_on_every_decision(self):
        def w(h, p):
            return {"watch": {"hung": {"noul": h}, "progressing": {"noul": p}}}
        for name, answers, want in [("unstick", w(0.9, 0.9), ["unstick"]),
                                    ("extend", w(0.1, 0.9), ["extend", "escalate"]),
                                    ("error", None, ["escalate"])]:
            with self.subTest(name):
                rows = self.watch_rows(answers)
                self.assertEqual([r["decision"] for r in rows], want)
                self.assertTrue(all(r.get("backend") == "jev" for r in rows), rows)


class Q1(unittest.TestCase):
    """A fallback may never produce an approving decision on any command; Jev-answered qa pass stays exit 0."""
    QA = {"qa": {"done": {"noul": 0.95}, "failure_kind": {"choice": "none", "confidence": 0.9}}}

    def qa(self, backend):
        import subprocess, tempfile
        d = tempfile.mkdtemp(prefix="jevo-q1-")
        stub = os.path.join(d, "stub.json")
        json.dump(dict(self.QA, **({"_backend": backend} if backend else {})), open(stub, "w"))
        jevo_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "jevo.py")
        p = subprocess.run([sys.executable, jevo_py, "--answers", "@" + stub, "qa", "--acceptance", "x",
                            "--evidence", "3 passed", "--exit-code", "0"],
                           capture_output=True, text=True, env=dict(os.environ, CLAUDE_PROJECT_DIR=d))
        return p.returncode, json.loads(p.stdout)

    def test_standalone_qa_pass_from_fallback_escalates(self):
        code, out = self.qa("http://127.0.0.1:1")
        self.assertEqual((code, out["decision"], out["fallback_decision"]), (3, "escalate", "pass"))
        self.assertIn("frontier", out["reason"])

    def test_standalone_qa_pass_from_jev_still_exits_zero(self):
        code, out = self.qa(None)
        self.assertEqual((code, out["decision"]), (0, "pass"))

    def test_conservative_covers_approve_and_pass_only(self):
        ask = mock.Mock(backend="http://x")
        for dec in ("approve", "pass"):
            r = jevo.conservative(dict(decision=dec, exit=0), ask)
            self.assertEqual((r["decision"], r["exit"], r["fallback_decision"]), ("escalate", 3, dec))
        r = jevo.conservative(dict(decision="fix", exit=1), ask)
        self.assertEqual((r["decision"], r["exit"]), ("fix", 1))


class DefaultFallbackOff(unittest.TestCase):
    def test_default_fallback_urls_empty(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("JEVO_FALLBACK_URLS", None)
            self.assertEqual(jevlib.fallback_urls(), [])


if __name__ == "__main__":
    unittest.main()
