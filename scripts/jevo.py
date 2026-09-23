#!/usr/bin/env python3
"""jevo — Jev decision stages for the jev-claude-orchestrator plugin.

  jevo.py slice new --id S1 --goal G --acceptance AC --allow 'src/a.py,tests/*' --gate 'pytest -q tests/test_a.py'
  jevo.py slice list
  jevo.py triage --slice S1                 # or --slice @packet.json
  jevo.py check  --slice S1                 # run gate -> qa, then review the slice diff
  jevo.py review --acceptance AC --diff @file|- [--allow globs]
  jevo.py qa     --acceptance AC --evidence @file|- --exit-code N
  jevo.py report [--html out.html]

Prints one JSON decision. Exit 0 proceed/approve/pass, 1 fix/reject/retry, 3 escalate, 2 error.
Errors and malformed Jev answers never exit 0. Every decision is appended to
.jev-orchestrator/ledger.jsonl. --answers @stub.json replays Jev answers (offline tests).
"""
import argparse, fnmatch, glob, json, os, subprocess, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jevlib, stages  # noqa: E402

GATE_TIMEOUT = int(os.environ.get("JEVO_GATE_TIMEOUT", "600"))
GENERATED = ("__pycache__", "*.pyc", ".pytest_cache", "node_modules", ".omc", ".jev-orchestrator", ".DS_Store")


class Asker:
    """Live Jev, or a stub file keyed by question set. Tracks spend."""

    def __init__(self, stub=None):
        self.stub = json.load(open(stub)) if stub else None
        self.cost = 0.0
        self.ms = 0.0

    def __call__(self, qset, state):
        if self.stub is not None:
            return self.stub[qset]
        res = jevlib.system_one(state, stages.QUESTIONS[qset])
        self.cost += jevlib.cost(res)
        self.ms += res.get("_ms", 0)
        return res["answers"]


def read(arg):
    return sys.stdin.read() if arg == "-" else open(arg[1:]).read() if arg.startswith("@") else arg


def slices_dir():
    d = os.path.join(jevlib.state_dir(), "slices")
    os.makedirs(d, exist_ok=True)
    return d


def exclude_state_dir():
    """Keep .jev-orchestrator/ out of commits without touching tracked files."""
    gitdir = jevlib.git("rev-parse", "--git-dir").strip()
    if not gitdir:
        return
    path = os.path.join(jevlib.project_dir(), gitdir, "info", "exclude")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = open(path).read() if os.path.exists(path) else ""
    if ".jev-orchestrator/" not in existing.split():
        with open(path, "a") as f:
            f.write("\n.jev-orchestrator/\n")


def load_slice(ref):
    if ref.startswith("@") or ref.startswith("{"):
        return json.loads(read(ref))
    return json.load(open(os.path.join(slices_dir(), ref + ".json")))


def untracked(cwd=None):
    """Untracked files, minus tool/build output that no worker is accountable for."""
    files = jevlib.git("ls-files", "--others", "--exclude-standard", cwd=cwd).split()
    return [f for f in files if not f.startswith(".claude/worktrees/")
            and not any(fnmatch.fnmatch(part, g) for part in f.split("/") for g in GENERATED)]


def slice_diff(base, cwd=None, since=0.0):
    """Committed + uncommitted changes since base, plus untracked files written since the slice was cut.
    Untracked files older than the slice are someone else's (a list of them can be huge, so we compare mtimes)."""
    root = cwd or jevlib.project_dir()
    diff = jevlib.git("diff", base, cwd=cwd)
    for f in untracked(cwd):
        try:
            if os.path.getmtime(os.path.join(root, f)) < since:
                continue
        except OSError:
            continue
        p = subprocess.run(["git", "diff", "--no-index", "/dev/null", f], cwd=cwd or jevlib.project_dir(),
                           capture_output=True, text=True)
        diff += p.stdout.replace("+++ b//", "+++ b/")
    return diff


def run_gate(cmd, cwd=None):
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd or jevlib.project_dir(), capture_output=True, text=True,
                           timeout=GATE_TIMEOUT)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        return 124, "gate timed out after %ds\n%s" % (GATE_TIMEOUT, str(e.stdout or "")[-2000:])


def check(s, ask, cwd=None):
    """What the SubagentStop hook calls: the gate the conductor wrote, then QA, then review.
    cwd is the worker's checkout (a worktree when isolated); slice state stays in the main checkout."""
    code, log = run_gate(s["gate"], cwd)
    root = cwd or jevlib.project_dir()
    missing = [p for p in s.get("allow", []) if not any(c in p for c in "*?[") and not os.path.exists(os.path.join(root, p))]
    if code != 0 and missing:  # red gate + a named slice file never written: the worker's job, not the environment
        return dict(decision="fix", exit=1, reason="gate failed and slice files are missing", files=missing,
                    stage="qa", gate_exit=code, gate_tail=log[-1500:])
    q = stages.qa(s["acceptance"], log, code, ask)
    if q["exit"] != 0:
        return dict(q, stage="qa", gate_exit=code, gate_tail=log[-1500:])
    r = stages.review(s["acceptance"], slice_diff(s["base"], cwd, s.get("created", 0.0)), ask,
                      s.get("allow"))
    return dict(r, stage="review", gate_exit=code, qa=q)


def save_slice(s):
    path = os.path.join(slices_dir(), s["id"] + ".json")
    with open(path + ".tmp", "w") as f:
        json.dump(s, f, indent=2)
    os.replace(path + ".tmp", path)


def gate_slice(s, ask, cwd=None, max_blocks=2, agent=None):
    """The finish gate every host (Claude SubagentStop, pi turn_end) calls when a worker tries to stop.

    Returns {"block": bool, "message": str|None, "status", ...}. block=True means send the worker back.
    approved/escalate are final: a fix goes to a fresh worker under a new slice id, never a re-check loop.
    Any error escalates; nothing is ever approved on error.
    """
    if s.get("status") in ("approved", "escalate"):
        return dict(block=False, status=s["status"], final=True)
    try:
        res = check(s, ask, cwd)
    except Exception as e:  # fail closed: never approved, conductor decides
        res = dict(decision="error", exit=2, reason="%s: %s" % (type(e).__name__, e))
    blocks, code = s.get("blocks", 0), res["exit"]
    if code == 1 and blocks < max_blocks:
        s.update(status="fixing", blocks=blocks + 1, last=res.get("reason"))
    else:
        s.update(status="approved" if code == 0 else "escalate", last=res.get("reason"))
    save_slice(s)
    jevlib.log("gate", slice=s["id"], agent=agent, decision=res["decision"], exit=code, status=s["status"],
               reason=res.get("reason"), files=res.get("files"), gate_exit=res.get("gate_exit"),
               gate_tail=(res.get("gate_tail") or "")[-300:] or None, cwd=cwd,
               cost_usd=round(ask.cost, 8), stub=ask.stub is not None)
    out = dict(block=s["status"] == "fixing", status=s["status"], decision=res["decision"],
               reason=res.get("reason"), message=None)
    if out["block"]:
        lines = ["Jev supervisor: slice %s is not done (%s: %s)." % (s["id"], res["decision"], res.get("reason"))]
        if res.get("files"):
            lines.append("Files: " + ", ".join(res["files"]))
        if res.get("gate_tail"):
            lines.append("Gate `%s` exit %s, output tail:\n%s" % (s["gate"], res.get("gate_exit"), res["gate_tail"][-800:]))
        lines.append("Fix this specific problem inside your slice, rerun the gate, then finish. Never delete, move "
                     "or revert files you did not create to get past this check; if the cause is outside your "
                     "slice, say so in your final message and finish. Round %d of %d." % (blocks + 1, max_blocks))
        out["message"] = "\n".join(lines)
    return out


def cmd_slice(a, ask):
    if a.action == "list":
        rows = [json.load(open(p)) for p in sorted(glob.glob(os.path.join(slices_dir(), "*.json")))]
        return dict(decision="list", exit=0, slices=[{k: r.get(k) for k in ("id", "status", "goal")} for r in rows])
    if not all([a.id, a.acceptance, a.gate]):
        raise ValueError("slice new needs --id, --acceptance and --gate")
    base = jevlib.git("rev-parse", "HEAD").strip()
    if not base:
        raise ValueError("not a git repository with a commit")
    s = dict(id=a.id, goal=a.goal or a.acceptance, acceptance=a.acceptance, gate=a.gate, base=base,
             allow=[g.strip() for g in (a.allow or "").split(",") if g.strip()], status="new",
             created=time.time())
    json.dump(s, open(os.path.join(slices_dir(), a.id + ".json"), "w"), indent=2)
    exclude_state_dir()
    return dict(decision="created", exit=0, slice=s, marker="JEV-SLICE: " + a.id)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--answers", help="@stub.json: replay Jev answers instead of calling the API")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("slice"); s.add_argument("action", choices=["new", "list"])
    for f in ("--id", "--goal", "--acceptance", "--allow", "--gate"):
        s.add_argument(f)
    t = sub.add_parser("triage"); t.add_argument("--slice", required=True); t.add_argument("--seats")
    c = sub.add_parser("check"); c.add_argument("--slice", required=True)
    r = sub.add_parser("review"); r.add_argument("--acceptance", required=True)
    r.add_argument("--diff", required=True); r.add_argument("--allow")
    q = sub.add_parser("qa"); q.add_argument("--acceptance", required=True)
    q.add_argument("--evidence", required=True); q.add_argument("--exit-code", type=int, required=True)
    g = sub.add_parser("gate"); g.add_argument("--slice", required=True); g.add_argument("--cwd")
    g.add_argument("--max-blocks", type=int, default=2); g.add_argument("--agent")
    h = sub.add_parser("health"); h.add_argument("--slice", required=True); h.add_argument("--actions", required=True)
    rp = sub.add_parser("report"); rp.add_argument("--html")
    a = p.parse_args()
    ask = Asker(a.answers[1:] if a.answers else None)
    slice_id = None
    try:
        if a.cmd == "slice":
            res = cmd_slice(a, ask)
        elif a.cmd == "report":
            import report
            res = report.build(a.html)
        elif a.cmd == "triage":
            sl = load_slice(a.slice); slice_id = sl.get("id")
            res = stages.triage(sl, ask, json.loads(a.seats or "{}"))
        elif a.cmd == "check":
            sl = load_slice(a.slice); slice_id = sl.get("id")
            res = check(sl, ask)
        elif a.cmd == "gate":  # logs itself; exit 0 unless the gate itself crashed
            res = dict(gate_slice(load_slice(a.slice), ask, a.cwd, a.max_blocks, a.agent), exit=0)
        elif a.cmd == "health":
            sl = load_slice(a.slice); slice_id = sl.get("id")
            res = stages.health({"acceptance": sl["acceptance"], "recent_actions": json.loads(read(a.actions))}, ask)
        elif a.cmd == "review":
            res = stages.review(a.acceptance, read(a.diff), ask, (a.allow or "").split(","))
        else:
            res = stages.qa(a.acceptance, read(a.evidence), a.exit_code, ask)
    except Exception as e:  # malformed answers, bad input, API failure: fail closed
        res = dict(decision="error", exit=2, error="%s: %s" % (type(e).__name__, e))
    if a.cmd not in ("slice", "report", "gate"):
        res.update(cost_usd=round(ask.cost, 8), jev_ms=round(ask.ms, 1))
        jevlib.log(a.cmd, slice=slice_id, decision=res["decision"], exit=res["exit"],
                   reason=res.get("reason") or res.get("error"), cost_usd=res["cost_usd"], stub=bool(a.answers))
    print(json.dumps(res, indent=2))
    sys.exit(res["exit"])


if __name__ == "__main__":
    main()
