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
    res = jevo.gate_slice(s, jevo.Asker(os.environ.get("JEVO_ANSWERS")), inp.get("cwd"),
                          hooklib.option("max_blocks", 2), inp.get("agent_type"))
    if not res["block"]:
        return 0
    print(res["message"], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
