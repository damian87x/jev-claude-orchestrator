"""Closeout regressions F1-F4. No network: OPENER, sleep and the ledger are mocked."""
import http.client, io, os, re, sys, unittest, urllib.error
from email.message import Message
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "lib"), os.path.join(ROOT, "scripts")]
import jevlib, jevo  # noqa: E402

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
                 "incomplete_read": lambda n: Resp(exc=http.client.IncompleteRead(b"x"))}
        for name, primary in cases.items():
            with self.subTest(name):
                res, p, f, _, auth = self.run_system_one(primary)
                self.assertEqual((res["_backend"], p, f, auth), (FALLBACK, 1, 1, [None]))


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


if __name__ == "__main__":
    unittest.main()
