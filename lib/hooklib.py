"""Hook helpers: which subagent is this, and which slice is it working on."""
import glob, json, os, re, sys

import jevlib

MARKER = re.compile(r"JEV-SLICE:\s*([A-Za-z0-9_.-]+)")


def read_input():
    return json.load(sys.stdin)


def is_worker(inp):
    return "slice-worker" in (inp.get("agent_type") or "")


def option(name, default):
    """Non-sensitive plugin userConfig values arrive as CLAUDE_PLUGIN_OPTION_<KEY>."""
    try:
        return type(default)(os.environ.get("CLAUDE_PLUGIN_OPTION_" + name.upper(), default))
    except ValueError:
        return default


def subagent_transcript(inp):
    path = inp.get("agent_transcript_path")
    if path and os.path.exists(path):
        return path
    main, agent = inp.get("transcript_path") or "", inp.get("agent_id") or ""
    if main and agent:
        hits = glob.glob(os.path.join(main[:-len(".jsonl")] if main.endswith(".jsonl") else main,
                                      "subagents", "agent-%s.jsonl" % agent))
        if hits:
            return hits[0]
    return None


def slice_id(inp):
    """The JEV-SLICE marker from the start of the worker's own transcript (its task prompt)."""
    path = subagent_transcript(inp)
    if not path:
        return None
    with open(path, errors="replace") as f:
        for _, line in zip(range(20), f):
            m = MARKER.search(line)
            if m:
                return m.group(1)
    return None


def load_slice(sid):
    path = os.path.join(jevlib.state_dir(), "slices", sid + ".json")
    return json.load(open(path)) if os.path.exists(path) else None


def save_slice(s):
    path = os.path.join(jevlib.state_dir(), "slices", s["id"] + ".json")
    tmp = path + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, path)
