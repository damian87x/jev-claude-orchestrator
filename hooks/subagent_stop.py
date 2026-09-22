#!/usr/bin/env python3
"""SubagentStop: a slice worker may stop only when its gate passes and the Jev review approves.

fix  -> block the stop (exit 2); the reason goes back to the worker, which keeps working.
pass -> allow; slice status "approved".
escalate / error / too many blocks -> allow the stop, but mark the slice for the conductor
(status "escalate") so a frontier reviewer or a human decides. Never marks approved on error.
"""
import os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "lib"), os.path.join(ROOT, "scripts")]
import hooklib, jevlib, jevo  # noqa: E402


def main():
    inp = hooklib.read_input()
    if not hooklib.is_worker(inp):
        return 0
    sid = hooklib.slice_id(inp)
    s = hooklib.load_slice(sid) if sid else None
    if not s:
        jevlib.log("subagent_stop", slice=sid, agent=inp.get("agent_type"), decision="no_slice", exit=0)
        return 0
    if s.get("status") in ("approved", "escalate"):
        return 0  # final: a fix goes to a fresh worker under a new slice id, never a re-check loop
    ask = jevo.Asker(os.environ.get("JEVO_ANSWERS"))
    try:
        res = jevo.check(s, ask, inp.get("cwd"))
    except Exception as e:  # fail closed: never approved, conductor decides
        res = dict(decision="error", exit=2, reason="%s: %s" % (type(e).__name__, e))
    blocks = s.get("blocks", 0)
    max_blocks = hooklib.option("max_blocks", 2)
    code = res["exit"]
    if code == 1 and blocks < max_blocks:
        s.update(status="fixing", blocks=blocks + 1, last=res.get("reason"))
    else:
        s.update(status="approved" if code == 0 else "escalate", last=res.get("reason"))
    hooklib.save_slice(s)
    jevlib.log("subagent_stop", slice=sid, agent=inp.get("agent_type"), decision=res["decision"],
               exit=code, status=s["status"], reason=res.get("reason"), files=res.get("files"),
               gate_exit=res.get("gate_exit"), gate_tail=(res.get("gate_tail") or "")[-300:] or None,
               cwd=inp.get("cwd"), cost_usd=round(ask.cost, 8), stub=bool(ask.stub))
    if s["status"] != "fixing":
        return 0
    detail = [f"Jev supervisor: slice {sid} is not done ({res['decision']}: {res.get('reason')})."]
    if res.get("files"):
        detail.append("Files: " + ", ".join(res["files"]))
    if res.get("gate_tail"):
        detail.append("Gate `%s` exit %s, output tail:\n%s" % (s["gate"], res.get("gate_exit"), res["gate_tail"][-800:]))
    detail.append("Fix this specific problem inside your slice, rerun the gate, then finish. Never delete, move "
                  "or revert files you did not create to get past this check; if the cause is outside your "
                  "slice, say so in your final message and finish. Round %d of %d." % (blocks + 1, max_blocks))
    print("\n".join(detail), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
