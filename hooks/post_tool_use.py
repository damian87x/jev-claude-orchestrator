#!/usr/bin/env python3
"""PostToolUse: foreman-style health check on slice workers every N tool calls.

Jev judges stuck / off-track from a compact action log; a steer is injected as
additionalContext. This hook never blocks a tool and never fails the worker: any error is a no-op.
"""
import json, os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "lib"), os.path.join(ROOT, "scripts")]
import hooklib, jevlib, jevo, stages  # noqa: E402

KEEP = 12


def summarize(inp):
    ti = inp.get("tool_input") or {}
    what = ti.get("command") or ti.get("file_path") or ti.get("pattern") or ti.get("path") or ""
    resp = inp.get("tool_response")
    text = resp if isinstance(resp, str) else json.dumps(resp)[:2000] if resp is not None else ""
    return {"tool": inp.get("tool_name"), "target": str(what)[:160], "result": text[:200]}


def main():
    inp = hooklib.read_input()
    if not hooklib.is_worker(inp) or not inp.get("agent_id"):
        return 0
    d = os.path.join(jevlib.state_dir(inp.get("cwd")), "agents")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, inp["agent_id"] + ".json")
    st = json.load(open(path)) if os.path.exists(path) else {"n": 0, "recent": [], "slice": hooklib.slice_id(inp)}
    st["n"] += 1
    st["recent"] = (st["recent"] + [summarize(inp)])[-KEEP:]
    json.dump(st, open(path, "w"))
    every = hooklib.option("health_every", 8)
    s = hooklib.load_slice(st["slice"]) if st.get("slice") else None
    if not s or every <= 0 or st["n"] % every:
        return 0
    ask = jevo.Asker(os.environ.get("JEVO_ANSWERS"))
    res = stages.health({"acceptance": s["acceptance"], "recent_actions": st["recent"]}, ask)
    jevlib.log("health", slice=s["id"], decision=res["decision"], exit=0, signals=res["signals"],
               cost_usd=round(ask.cost, 8), stub=bool(ask.stub), backend=ask.backend)
    if res["steer"]:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": res["steer"]}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # supervision must never break the worker
        try:
            jevlib.log("health", decision="error", exit=0, reason="%s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        sys.exit(0)
