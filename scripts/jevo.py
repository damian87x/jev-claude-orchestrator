#!/usr/bin/env python3
"""jevo — Jev decision stages for the jev-claude-orchestrator plugin.

  jevo.py slice new --id S1 --goal G --acceptance AC --allow 'src/a.py,tests/*' --gate 'pytest -q tests/test_a.py'
  jevo.py slice list
  jevo.py triage --slice S1                 # or --slice @packet.json
  jevo.py check  --slice S1                 # run gate -> qa, then review the slice diff
  jevo.py review --acceptance AC --diff @file|- [--allow globs]
  jevo.py qa     --acceptance AC --evidence @file|- --exit-code N
  jevo.py report [--html out.html]
  jevo.py watch  --slice S1 --agent A1      # run in the background per worker; exits on done / unstick / escalate

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

    def __init__(self, stub=None, jev_only=False):
        self.stub = json.load(open(stub)) if stub else None
        self.jev_only = jev_only
        self.cost = 0.0
        self.ms = 0.0
        self.backend = self.stub.get("_backend", "jev") if self.stub else "jev"

    def __call__(self, qset, state):
        if self.stub is not None:
            return self.stub[qset]
        try:
            res = jevlib.system_one(state, stages.QUESTIONS[qset], qset=None if self.jev_only else qset)
        except jevlib.JevError as e:  # every backend failed; a billed malformed Jev reply still costs
            self.cost += jevlib.cost({"usage": e.usage or {}})
            raise
        self.cost += jevlib.cost(res)
        self.ms += res.get("_ms", 0)
        if res.get("_backend", "jev") != "jev":
            self.backend = res["_backend"]
        return res["answers"]


def conservative(res, ask):
    """A local fallback model is unmeasured on these questions: it may route and send work back,
    but its approval only escalates to a frontier reviewer."""
    if ask.backend == "jev":
        return res
    res = dict(res, backend=ask.backend)
    if res["exit"] == 0 and res["decision"] == "approve":
        res.update(decision="escalate", exit=3, fallback_decision="approve",
                   reason="approved by local fallback %s, not Jev: a frontier reviewer must confirm" % ask.backend)
    return res


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
    return conservative(dict(r, stage="review", gate_exit=code, qa=q), ask)


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
               cost_usd=round(ask.cost, 8), stub=ask.stub is not None, backend=ask.backend)
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


def find_transcript(agent):
    """A Claude Code subagent transcript: ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl."""
    hits = glob.glob(os.path.expanduser("~/.claude/projects/*/*/subagents/agent-%s.jsonl" % agent))
    return max(hits, key=os.path.getmtime) if hits else None


def pending_action(transcript):
    """The worker's tool call that has no result yet: the one a hang is stuck in. PostToolUse never sees it."""
    calls, done = {}, set()
    try:
        for line in open(transcript):
            content = (json.loads(line).get("message") or {}).get("content")
            for part in content if isinstance(content, list) else ():
                if part.get("type") == "tool_use":
                    calls[part["id"]] = part
                elif part.get("type") == "tool_result":
                    done.add(part.get("tool_use_id"))
    except (OSError, ValueError, AttributeError):
        return None
    open_calls = [c for i, c in calls.items() if i not in done]
    if not open_calls:
        return None
    ti = open_calls[-1].get("input") or {}
    what = ti.get("command") or ti.get("file_path") or ti.get("pattern") or ti.get("url") or ""
    return {"tool": open_calls[-1].get("name"), "target": str(what)[:300]}


def watch(slice_id, agent, ask, limit=3600, idle=900, extend=1800, poll=30, transcript=None):
    """Wall-clock watchdog for one worker. A hung tool call emits no hook events, so health steering
    never sees it; this does. Activity = mtime of the worker's transcript (found by agent id, or --transcript),
    else of its PostToolUse state file. The transcript also shows the tool call still running.
    On a limit, Jev ONLY decides (no local fallback): extend once silently, else return unstick / escalate
    so the conductor acts. A Jev error escalates. Nothing is killed here."""
    t0 = floor = time.time()
    deadline, extended = t0 + limit, False
    state = os.path.join(jevlib.state_dir(), "agents", agent + ".json")
    while True:
        transcript = transcript or find_transcript(agent)  # a fresh worker may not have written it yet
        activity = transcript or state
        s = load_slice(slice_id)
        if s.get("status") in ("approved", "escalate"):
            return dict(decision="done", exit=0, status=s["status"])
        now = time.time()
        try:
            last = max(os.path.getmtime(activity), floor)
        except OSError:
            last = floor
        if now >= deadline or now - last >= idle:
            try:
                recent = json.load(open(state)).get("recent", [])
            except (OSError, ValueError):
                recent = []
            packet = dict(acceptance=s["acceptance"], elapsed_min=round((now - t0) / 60, 1),
                          idle_min=round((now - last) / 60, 1), extended=extended, recent_actions=recent,
                          pending_action=pending_action(transcript) if transcript else None)
            try:
                res = stages.watch(packet, ask)
            except Exception as e:
                res = dict(decision="escalate", exit=3, reason="Jev error: %s: %s" % (type(e).__name__, e))
            jevlib.log("watch", slice=slice_id, agent=agent, decision=res["decision"], exit=res["exit"],
                       reason=res.get("reason"), elapsed_min=packet["elapsed_min"], idle_min=packet["idle_min"],
                       cost_usd=round(ask.cost, 8), stub=ask.stub is not None)
            if res["decision"] != "extend":
                return dict(res, elapsed_min=packet["elapsed_min"], idle_min=packet["idle_min"],
                            pending_action=packet["pending_action"])
            extended, deadline, floor = True, now + extend, now  # the idle clock restarts with the extension
        time.sleep(poll)


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
    w = sub.add_parser("watch"); w.add_argument("--slice", required=True); w.add_argument("--agent", required=True)
    w.add_argument("--transcript"); w.add_argument("--poll", type=float, default=30)
    for f, v in (("--limit", 3600), ("--idle", 900), ("--extend", 1800)):
        w.add_argument(f, type=float, default=v, help="seconds (default %d)" % v)
    a = p.parse_args()
    ask = Asker(a.answers[1:] if a.answers else None, jev_only=a.cmd == "watch")
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
        elif a.cmd == "watch":  # logs itself
            res = watch(a.slice, a.agent, ask, a.limit, a.idle, a.extend, a.poll, a.transcript)
        elif a.cmd == "gate":  # logs itself; exit 0 unless the gate itself crashed
            res = dict(gate_slice(load_slice(a.slice), ask, a.cwd, a.max_blocks, a.agent), exit=0)
        elif a.cmd == "health":
            sl = load_slice(a.slice); slice_id = sl.get("id")
            res = stages.health({"acceptance": sl["acceptance"], "recent_actions": json.loads(read(a.actions))}, ask)
        elif a.cmd == "review":
            res = conservative(stages.review(a.acceptance, read(a.diff), ask, (a.allow or "").split(",")), ask)
        else:
            res = stages.qa(a.acceptance, read(a.evidence), a.exit_code, ask)
    except Exception as e:  # malformed answers, bad input, API failure: fail closed
        res = dict(decision="error", exit=2, error="%s: %s" % (type(e).__name__, e))
    if a.cmd not in ("slice", "report", "gate", "watch"):
        res.update(cost_usd=round(ask.cost, 8), jev_ms=round(ask.ms, 1))
        if ask.backend != "jev":
            res["backend"] = ask.backend
        jevlib.log(a.cmd, slice=slice_id, decision=res["decision"], exit=res["exit"],
                   reason=res.get("reason") or res.get("error"), cost_usd=res["cost_usd"], stub=bool(a.answers),
                   backend=ask.backend)
    print(json.dumps(res, indent=2))
    sys.exit(res["exit"])


if __name__ == "__main__":
    main()
