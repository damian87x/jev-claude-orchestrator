"""Hook contract tests: feed the documented hook stdin JSON, check exit codes, stderr/stdout and slice status."""
import json, os, subprocess, sys, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
STOP = os.path.join(ROOT, "hooks", "subagent_stop.py")
POST = os.path.join(ROOT, "hooks", "post_tool_use.py")
JEVO = os.path.join(ROOT, "scripts", "jevo.py")
WORKER = "jev-claude-orchestrator:slice-worker"
GOOD_REVIEW = {"review_risk": {k: {"noul": 0.05} for k in ("correctness", "security", "tests_missing", "scope_drift")},
               "review_verdict": {"severity": {"score": 0.2}, "verdict": {"choice": "approve", "confidence": 0.9}}}
DONE = {"done": {"noul": 0.95}, "failure_kind": {"choice": "code", "confidence": 0.9}}


class Hooks(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="jevo-hooks-")
        run = lambda *c: subprocess.run(c, cwd=self.repo, capture_output=True, check=True)
        run("git", "init", "-q", "-b", "main")
        run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
        self.env = dict(os.environ, CLAUDE_PROJECT_DIR=self.repo)
        subprocess.run([sys.executable, JEVO, "slice", "new", "--id", "S7", "--acceptance", "add.py defines add",
                        "--allow", "add.py", "--gate", "python3 -c 'import add'"], cwd=self.repo, env=self.env,
                       capture_output=True, check=True)
        self.transcript = os.path.join(self.repo, "..", os.path.basename(self.repo) + "-agent.jsonl")
        with open(self.transcript, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "JEV-SLICE: S7\nImplement add.py"}}) + "\n")

    def stub(self, answers):
        path = self.transcript + ".stub.json"
        with open(path, "w") as f:
            json.dump(answers, f)
        self.env["JEVO_ANSWERS"] = path

    def hook(self, script, **inp):
        base = {"session_id": "s", "hook_event_name": "x", "agent_id": "a1", "agent_type": WORKER,
                "agent_transcript_path": self.transcript}
        p = subprocess.run([sys.executable, script], input=json.dumps(dict(base, **inp)), cwd=self.repo,
                           env=self.env, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def status(self):
        with open(os.path.join(self.repo, ".jev-orchestrator", "slices", "S7.json")) as f:
            return json.load(f)

    def test_other_subagents_are_ignored(self):
        self.assertEqual(self.hook(STOP, agent_type="Explore"), (0, "", ""))

    def test_red_gate_blocks_stop_with_reason(self):
        self.stub({"qa": DONE})
        code, _, err = self.hook(STOP)
        self.assertEqual((code, self.status()["status"]), (2, "fixing"))
        self.assertIn("slice S7 is not done", err)
        self.assertIn("No module named 'add'", err)

    def test_block_rounds_are_capped_then_escalated(self):
        self.stub({"qa": DONE})
        self.assertEqual([self.hook(STOP)[0] for _ in range(3)], [2, 2, 0])
        self.assertEqual(self.status()["status"], "escalate")

    def test_green_gate_and_approve_lets_worker_stop(self):
        self.stub(dict(GOOD_REVIEW, qa=DONE))
        with open(os.path.join(self.repo, "add.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        self.assertEqual(self.hook(STOP)[0], 0)
        self.assertEqual(self.status()["status"], "approved")

    def test_jev_error_never_approves(self):
        self.stub({})  # every answer missing -> KeyError inside the stage
        with open(os.path.join(self.repo, "add.py"), "w") as f:
            f.write("x = 1\n")
        self.assertEqual(self.hook(STOP)[0], 0)
        self.assertEqual(self.status()["status"], "escalate")

    def test_preexisting_and_generated_untracked_files_are_not_blamed(self):
        # live run 2026-09-22: logs + __pycache__/.omc in the checkout made every slice fail the allowlist
        self.stub(dict(GOOD_REVIEW, qa=DONE))
        def touch(rel):
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)) or self.repo, exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("x")
        touch("notes.log")
        subprocess.run([sys.executable, JEVO, "slice", "new", "--id", "S7", "--acceptance", "add.py defines add",
                        "--allow", "add.py", "--gate", "python3 -c 'import add'"], cwd=self.repo, env=self.env,
                       capture_output=True, check=True)  # re-cut the slice: notes.log now preexists
        touch("pkg/__pycache__/x.pyc")  # generated while the worker runs, after the cut
        touch(".omc/state.json")
        with open(os.path.join(self.repo, "add.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        self.assertEqual(self.hook(STOP)[0], 0)
        self.assertEqual(self.status()["status"], "approved")

    def test_slice_packet_stays_small_with_many_untracked_files(self):
        # city-shift run: thousands of untracked paths made a 34 MB packet and broke triage
        for i in range(3000):
            with open(os.path.join(self.repo, "junk%d.txt" % i), "w") as f:
                f.write("x")
        subprocess.run([sys.executable, JEVO, "slice", "new", "--id", "S7", "--acceptance", "a", "--gate", "true"],
                       cwd=self.repo, env=self.env, capture_output=True, check=True)
        self.assertLess(os.path.getsize(os.path.join(self.repo, ".jev-orchestrator", "slices", "S7.json")), 2000)

    def test_final_status_is_not_rechecked(self):
        # live run: an escalated slice was re-gated on every later stop (24 calls for 2 slices)
        self.stub({"qa": DONE})
        s = self.status(); s["status"] = "escalate"
        with open(os.path.join(self.repo, ".jev-orchestrator", "slices", "S7.json"), "w") as f:
            json.dump(s, f)
        self.assertEqual(self.hook(STOP), (0, "", ""))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".jev-orchestrator", "ledger.jsonl")))

    def test_worktree_worker_uses_main_state_and_its_own_checkout(self):
        self.stub(dict(GOOD_REVIEW, qa=DONE))
        wt = self.repo + "-wt"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "s7", wt], cwd=self.repo, check=True)
        with open(os.path.join(wt, "add.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        self.env["CLAUDE_PROJECT_DIR"] = wt  # even if the hook is told the worktree is the project
        code, _, err = self.hook(STOP, cwd=wt)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.status()["status"], "approved")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "add.py")))

    def test_missing_slice_file_is_code_not_environment(self):
        # live run 2: gate failed on a test module the lazy worker never wrote; Jev called it "environment"
        subprocess.run([sys.executable, JEVO, "slice", "new", "--id", "S7", "--acceptance", "sub + its test",
                        "--allow", "add.py,tests/test_sub.py", "--gate", "python3 -m unittest -q tests.test_sub"],
                       cwd=self.repo, env=self.env, capture_output=True, check=True)
        self.stub({"qa": {"failure_kind": {"choice": "environment", "confidence": 0.95}}})
        with open(os.path.join(self.repo, "add.py"), "w") as f:
            f.write("def sub(a, b):\n    return a - b\n")
        code, _, err = self.hook(STOP)
        self.assertEqual((code, self.status()["status"]), (2, "fixing"))
        self.assertIn("tests/test_sub.py", err)

    def test_block_message_forbids_deleting_foreign_files(self):
        self.stub({"qa": DONE})
        self.assertIn("Never delete", self.hook(STOP)[2])

    def test_health_steers_every_n_calls(self):
        self.env["CLAUDE_PLUGIN_OPTION_HEALTH_EVERY"] = "2"
        self.stub({"health": {"worker_stuck": {"noul": 0.9}, "off_track": {"noul": 0.1},
                              "meaningful_progress": {"noul": 0.1}}})
        call = dict(tool_name="Bash", tool_input={"command": "pytest"}, tool_response="1 failed")
        first, second = self.hook(POST, **call), self.hook(POST, **call)
        self.assertEqual(first, (0, "", ""))
        ctx = json.loads(second[1])["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertIn("stuck", ctx["additionalContext"])

    def test_health_errors_are_silent(self):
        self.env["CLAUDE_PLUGIN_OPTION_HEALTH_EVERY"] = "1"
        self.stub({})
        self.assertEqual(self.hook(POST, tool_name="Read", tool_input={}, tool_response="")[:2], (0, ""))


if __name__ == "__main__":
    unittest.main()
