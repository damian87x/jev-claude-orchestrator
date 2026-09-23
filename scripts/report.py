"""Scoreboard from .jev-orchestrator/ledger.jsonl: per-slice latest decisions and total Jev spend."""
import html, json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jevlib  # noqa: E402


def rows():
    path = os.path.join(jevlib.state_dir(), "ledger.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn write never breaks the report
    return out


def build(html_path=None):
    rs = [r for r in rows() if not r.get("stub")]
    slices = {}
    for r in rs:
        if r.get("slice"):
            s = slices.setdefault(r["slice"], {"slice": r["slice"], "calls": 0, "blocks": 0, "steers": 0})
            s["calls"] += 1
            s["blocks"] += r["event"] in ("gate", "subagent_stop") and r.get("status") == "fixing"
            s["steers"] += r.get("decision") == "steer"
            s["last_" + r["event"]] = r.get("decision")
    total = round(sum(r.get("cost_usd") or 0 for r in rs), 6)
    by_event = {}
    for r in rs:
        by_event[r["event"]] = by_event.get(r["event"], 0) + 1
    res = dict(decision="report", exit=0, decisions=len(rs), by_event=by_event, jev_cost_usd=total,
               slices=sorted(slices.values(), key=lambda s: s["slice"]))
    if html_path:
        write_html(html_path, res)
        res["html"] = os.path.abspath(html_path)
    return res


def write_html(path, res):
    cols = ["slice", "last_triage", "last_check", "last_gate", "blocks", "steers", "calls"]
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % html.escape(str(s.get(c, ""))) for c in cols)
                   for s in res["slices"])
    open(path, "w").write("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jev Orchestrator Scoreboard</title>
<style>:root{--bg:#fafaf9;--fg:#1c1917;--mute:#78716c;--line:#e7e5e4}
@media (prefers-color-scheme:dark){:root{--bg:#1c1917;--fg:#f5f5f4;--mute:#a8a29e;--line:#44403c}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:960px;margin:auto}
.k{display:flex;gap:32px;flex-wrap:wrap;margin:16px 0 24px}.k b{display:block;font-size:28px}.k span{color:var(--mute)}
.t{overflow-x:auto}table{border-collapse:collapse;width:100%%}td,th{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--mute);font-weight:500}</style></head><body><h1>Jev Orchestrator Scoreboard</h1>
<div class="k"><div><b>%d</b><span>Jev decisions</span></div><div><b>$%.5f</b><span>Jev spend</span></div>
<div><b>%d</b><span>slices</span></div></div><div class="t"><table><tr>%s</tr>%s</table></div></body></html>""" % (
        res["decisions"], res["jev_cost_usd"], len(res["slices"]),
        "".join("<th>%s</th>" % c.replace("_", " ") for c in cols), body))
