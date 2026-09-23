"""End-to-end slice flow in a throwaway git repo: slice new -> worker edits -> check. Jev answers stubbed."""
import json, os, subprocess, sys, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
JEVO = os.path.join(ROOT, "scripts", "jevo.py")
sys.path.insert(0, os.path.join(ROOT, "lib"))
import stages  # noqa: E402

APPROVE = {"qa": {"done": {"noul": 0.95}, "failure_kind": {"choice": "none", "confidence": 0.9}},
           "review_risk": {k: {"noul": 0.05} for k in ("correctness", "security", "tests_missing", "scope_drift")},
           "review_verdict": {"severity": {"score": 0.2}, "verdict": {"choice": "approve", "confidence": 0.9}}}


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


class Flow(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="jevo-flow-")
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
        self.stub = os.path.join(self.repo, "..", os.path.basename(self.repo) + "-stub.json")
        with open(self.stub, "w") as f:
            json.dump(APPROVE, f)

    def jevo(self, *args):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.repo)
        p = subprocess.run([sys.executable, JEVO, "--answers", "@" + self.stub, *args], cwd=self.repo,
                           capture_output=True, text=True, env=env)
        return p.returncode, json.loads(p.stdout)

    def new_slice(self, gate):
        return self.jevo("slice", "new", "--id", "S1", "--acceptance", "add.py defines add(a, b)",
                         "--allow", "add.py", "--gate", gate)

    def test_slice_new_writes_packet_and_excludes_state_dir(self):
        code, out = self.new_slice("true")
        self.assertEqual((code, out["marker"]), (0, "JEV-SLICE: S1"))
        self.assertIn(".jev-orchestrator/", open(os.path.join(self.repo, ".git", "info", "exclude")).read())
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")

    def test_check_approves_in_scope_untracked_change_with_green_gate(self):
        self.new_slice("python3 -c 'import add; assert add.add(1, 2) == 3'")
        open(os.path.join(self.repo, "add.py"), "w").write("def add(a, b):\n    return a + b\n")
        code, out = self.jevo("check", "--slice", "S1")
        self.assertEqual((code, out["decision"], out["files"]), (0, "approve", ["add.py"]))

    def test_check_red_gate_is_fix_even_if_jev_says_done(self):
        self.new_slice("python3 -c 'import add'")
        code, out = self.jevo("check", "--slice", "S1")
        self.assertEqual((code, out["stage"], out["gate_exit"] != 0), (1, "qa", True))

    def test_check_rejects_file_outside_allowlist(self):
        self.new_slice("true")
        open(os.path.join(self.repo, "add.py"), "w").write("x = 1\n")
        open(os.path.join(self.repo, "other.py"), "w").write("y = 2\n")
        code, out = self.jevo("check", "--slice", "S1")
        self.assertEqual((code, out["files"]), (1, ["other.py"]))

    def test_every_decision_is_ledgered(self):
        self.new_slice("true")
        self.jevo("check", "--slice", "S1")
        rows = [json.loads(l) for l in open(os.path.join(self.repo, ".jev-orchestrator", "ledger.jsonl"))]
        self.assertEqual([(r["event"], r["slice"]) for r in rows], [("check", "S1")])

    def test_gate_cli_blocks_then_approves(self):
        # the host-neutral finish gate pi calls: JSON block/message, status kept in the packet
        self.new_slice("python3 -c 'import add; assert add.add(1, 2) == 3'")
        code, out = self.jevo("gate", "--slice", "S1", "--cwd", self.repo)
        self.assertEqual((code, out["block"], out["status"]), (0, True, "fixing"))
        self.assertIn("Never delete", out["message"])
        open(os.path.join(self.repo, "add.py"), "w").write("def add(a, b):\n    return a + b\n")
        code, out = self.jevo("gate", "--slice", "S1", "--cwd", self.repo)
        self.assertEqual((out["block"], out["status"]), (False, "approved"))
        code, out = self.jevo("gate", "--slice", "S1")
        self.assertEqual((out["block"], out.get("final")), (False, True))

    def test_health_cli_returns_steer(self):
        self.new_slice("true")
        with open(self.stub, "w") as f:
            json.dump({"health": {"worker_stuck": {"noul": 0.9}, "off_track": {"noul": 0.1},
                                  "meaningful_progress": {"noul": 0.1}}}, f)
        code, out = self.jevo("health", "--slice", "S1", "--actions", '[{"tool": "bash", "target": "pytest"}]')
        self.assertEqual(code, 0)
        self.assertIn("stuck", out["steer"])


class Health(unittest.TestCase):
    def ask(self, stuck, off):
        return lambda q, s: {"worker_stuck": {"noul": stuck}, "off_track": {"noul": off},
                             "meaningful_progress": {"noul": 0.5}}

    def test_stuck_steers(self):
        self.assertIn("stuck", stages.health({}, self.ask(0.9, 0.1))["steer"])

    def test_off_track_steers(self):
        self.assertIn("unrelated", stages.health({}, self.ask(0.1, 0.8))["steer"])

    def test_healthy_is_silent(self):
        self.assertIsNone(stages.health({}, self.ask(0.2, 0.2))["steer"])


if __name__ == "__main__":
    unittest.main()
