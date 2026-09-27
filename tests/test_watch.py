"""Worker watchdog: a limit hands the call to Jev (only Jev), which extends once, unsticks or escalates."""
import json, os, subprocess, sys, tempfile, unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
JEVO = os.path.join(ROOT, "scripts", "jevo.py")
sys.path[:0] = [os.path.join(ROOT, "lib"), os.path.join(ROOT, "scripts")]
import jevlib, jevo  # noqa: E402


def watch_answers(hung, progressing):
    return {"watch": {"hung": {"noul": hung}, "progressing": {"noul": progressing}}}


class Watch(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="jevo-watch-")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "base"], cwd=self.repo, check=True)
        self.stub = self.repo + "-stub.json"
        with open(self.stub, "w") as f:
            json.dump({}, f)
        self.run_jevo("slice", "new", "--id", "S1", "--acceptance", "add.py defines add", "--gate", "true")

    def run_jevo(self, *args):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.repo)
        p = subprocess.run([sys.executable, JEVO, "--answers", "@" + self.stub, *args], cwd=self.repo,
                           capture_output=True, text=True, env=env, timeout=30)
        return p.returncode, json.loads(p.stdout)

    def watch(self, answers, *extra):
        with open(self.stub, "w") as f:
            json.dump(answers, f)
        return self.run_jevo("watch", "--slice", "S1", "--agent", "A1", "--poll", "0.01", *extra)

    def ledger(self):
        path = os.path.join(self.repo, ".jev-orchestrator", "ledger.jsonl")
        rows = [json.loads(line) for line in open(path)] if os.path.exists(path) else []
        return [r for r in rows if r["event"] == "watch"]

    def test_finished_slice_exits_done_without_asking_jev(self):
        path = os.path.join(self.repo, ".jev-orchestrator", "slices", "S1.json")
        s = json.load(open(path))
        json.dump(dict(s, status="approved"), open(path, "w"))
        code, out = self.watch({}, "--limit", "0")
        self.assertEqual((code, out["decision"]), (0, "done"))
        self.assertEqual(self.ledger(), [])

    def test_hung_command_is_unstick(self):
        code, out = self.watch(watch_answers(0.9, 0.9), "--limit", "0")
        self.assertEqual((code, out["decision"]), (1, "unstick"))
        self.assertEqual([r["decision"] for r in self.ledger()], ["unstick"])

    def test_progress_extends_once_then_escalates(self):
        code, out = self.watch(watch_answers(0.1, 0.9), "--limit", "0", "--extend", "0")
        self.assertEqual((code, out["decision"]), (3, "escalate"))
        self.assertEqual([r["decision"] for r in self.ledger()], ["extend", "escalate"])

    def test_unsure_about_a_running_call_calls_back_instead_of_extending(self):
        transcript = os.path.join(self.repo, "t.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                         "input": {"command": "rm -rf *"}}]}}) + "\n")
        code, out = self.watch(watch_answers(0.3, 0.9), "--limit", "0", "--transcript", transcript)
        self.assertEqual((code, out["decision"], out["unsure"]), (3, "escalate", True))
        self.assertEqual(out["pending_action"], {"tool": "Bash", "target": "rm -rf *"})
        self.assertEqual([r["decision"] for r in self.ledger()], ["escalate"])

    def test_idle_worker_triggers_before_the_wall_clock(self):
        code, out = self.watch(watch_answers(0.1, 0.1), "--limit", "3600", "--idle", "0")
        self.assertEqual((code, out["decision"]), (3, "escalate"))

    def test_jev_error_escalates(self):
        code, out = self.watch({}, "--limit", "0")  # missing answers -> KeyError inside the stage
        self.assertEqual((code, out["decision"]), (3, "escalate"))
        self.assertIn("Jev error", out["reason"])


class Pending(unittest.TestCase):
    def transcript(self, *parts):
        path = tempfile.mktemp(suffix=".jsonl")
        with open(path, "w") as f:
            for p in parts:
                f.write(json.dumps({"message": {"content": [p]}}) + "\n")
        return path

    def test_tool_call_without_result_is_pending(self):
        path = self.transcript({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pnpm test"}},
                               {"type": "tool_result", "tool_use_id": "t1"},
                               {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "rm -rf *"}})
        self.assertEqual(jevo.pending_action(path), {"tool": "Bash", "target": "rm -rf *"})

    def test_all_calls_returned_means_none_pending(self):
        path = self.transcript({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                               {"type": "tool_result", "tool_use_id": "t1"})
        self.assertIsNone(jevo.pending_action(path))


class JevOnly(unittest.TestCase):
    def test_watch_asker_never_offers_the_local_fallback(self):
        # system_one only falls back when it gets a qset; the watch asker must pass none.
        fake = {"answers": watch_answers(0.1, 0.1)["watch"], "usage": {}}
        with mock.patch.object(jevlib, "system_one", return_value=fake) as call:
            jevo.Asker(jev_only=True)("watch", {})
            jevo.Asker()("watch", {})
        self.assertEqual([c.kwargs["qset"] for c in call.call_args_list], [None, "watch"])


if __name__ == "__main__":
    unittest.main()
