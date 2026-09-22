"""Policy tests for jevo.py stages. Jev answers are stubbed; the policy under test is ours."""
import json, os, subprocess, sys, tempfile, unittest

PROJECT = tempfile.mkdtemp(prefix="jevo-test-")
SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "jevo.py")
DIFF = """diff --git a/src/status.py b/src/status.py
--- a/src/status.py
+++ b/src/status.py
@@ -1,2 +1,3 @@
+import json
 def status(): return {"ok": True}
"""


def noul(p): return {"type": "noul", "noul": p}
def choice(c, conf, probs=None): return {"type": "choice", "choice": c, "confidence": conf, "probabilities": probs or {c: conf}}
def score(s): return {"type": "score", "score": s, "confidence": 0.8}


def triage_ans(tier_p, risk=1.0, human=0.02, shared=0.1):
    return {"triage": {"tier": {"type": "choice", "choice": max(tier_p, key=tier_p.get), "confidence": 0.6,
                                "probabilities": tier_p},
                       "risk": score(risk), "needs_human": noul(human), "shared_surface": noul(shared)}}


def review_ans(verdict="approve", conf=0.9, sev=0.2, security=0.05, correctness=0.1, drift=0.1, tests=0.1):
    return {"review_risk": {"correctness": noul(correctness), "security": noul(security),
                            "tests_missing": noul(tests), "scope_drift": noul(drift)},
            "review_verdict": {"severity": score(sev), "verdict": choice(verdict, conf)}}


def qa_ans(done=0.9, kind="none", conf=0.9):
    return {"qa": {"done": noul(done), "failure_kind": choice(kind, conf)}}


def run(answers, *args, stdin=None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(answers, f)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=PROJECT)
    p = subprocess.run([sys.executable, SCRIPT, "--answers", "@" + f.name, *args],
                       input=stdin, capture_output=True, text=True, env=env)
    os.unlink(f.name)
    return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else {})


SLICE = json.dumps({"goal": "add --json flag", "files": ["src/status.py"]})


class Triage(unittest.TestCase):
    def test_fails_upward_to_highest_weighted_tier(self):
        code, out = run(triage_ans({"fast": 0.6, "balanced": 0.3, "reasoning": 0.1}), "triage", "--slice", SLICE)
        self.assertEqual((code, out["tier"], out["seat"]), (0, "balanced", "sonnet"))

    def test_severe_risk_forces_reasoning_and_frontier_review(self):
        code, out = run(triage_ans({"fast": 0.9, "balanced": 0.1, "reasoning": 0.0}, risk=2.8), "triage", "--slice", SLICE)
        self.assertEqual((code, out["tier"], out["review"]), (0, "reasoning", "frontier"))

    def test_needs_human_escalates(self):
        code, out = run(triage_ans({"fast": 1.0, "balanced": 0.0, "reasoning": 0.0}, human=0.7), "triage", "--slice", SLICE)
        self.assertEqual((code, out["decision"]), (3, "human"))

    def test_shared_surface_serializes(self):
        _, out = run(triage_ans({"fast": 1.0, "balanced": 0.0, "reasoning": 0.0}, shared=0.8), "triage", "--slice", SLICE)
        self.assertEqual(out["wave"], "serial")


class Review(unittest.TestCase):
    def review(self, ans, *extra, diff=DIFF):
        return run(ans, "review", "--acceptance", "status prints json", "--diff", "-", *extra, stdin=diff)

    def test_clean_approve(self):
        self.assertEqual(self.review(review_ans())[0], 0)

    def test_outside_allowlist_rejected_before_jev(self):
        code, out = self.review({}, "--allow", "tests/*")
        self.assertEqual((code, out["files"]), (1, ["src/status.py"]))

    def test_secret_escalates_before_jev(self):
        code, out = self.review({}, diff=DIFF + '+API_KEY = "not-a-real-key-000000"\n')
        self.assertEqual(code, 3)

    def test_security_signal_overrides_jev_approve(self):
        self.assertEqual(self.review(review_ans(security=0.8))[0], 3)

    def test_low_confidence_approve_escalates(self):
        self.assertEqual(self.review(review_ans(conf=0.4))[0], 3)

    def test_fix_verdict_rejects(self):
        self.assertEqual(self.review(review_ans(verdict="fix", sev=1.8))[0], 1)

    def test_empty_diff_is_fix(self):
        self.assertEqual(self.review({}, diff="")[0], 1)

    def test_malformed_answer_is_error_not_approve(self):
        code, out = self.review({"review_risk": {}, "review_verdict": {}})
        self.assertEqual((code, out["decision"]), (2, "error"))


class QA(unittest.TestCase):
    def qa(self, ans, evidence, exit_code):
        return run(ans, "qa", "--acceptance", "status prints json", "--evidence", "-",
                   "--exit-code", str(exit_code), stdin=evidence)

    def test_failure_count_parsed_in_code_beats_green_exit(self):
        code, out = self.qa(qa_ans(done=0.99), "12 passed, 3 failed", 0)
        self.assertEqual((code, out["failures"]), (1, 3))

    def test_pass(self):
        self.assertEqual(self.qa(qa_ans(done=0.9), "12 passed, 0 failed", 0)[0], 0)

    def test_green_exit_without_evidence_escalates(self):
        self.assertEqual(self.qa(qa_ans(done=0.3), "worker says done", 0)[0], 3)

    def test_flaky_is_retry(self):
        code, out = self.qa(qa_ans(kind="flaky"), "1 failed: timeout", 1)
        self.assertEqual((code, out["decision"]), (1, "retry"))

    def test_environment_escalates(self):
        self.assertEqual(self.qa(qa_ans(kind="environment"), "ModuleNotFoundError", 1)[0], 3)


if __name__ == "__main__":
    unittest.main()
