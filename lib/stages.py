"""Decision stages. Jev answers snap judgments through `ask`; every threshold and veto is here.

Each stage returns {"decision", "exit", ...}. exit: 0 proceed/approve/pass, 1 fix/reject/retry,
3 escalate to a frontier reviewer or a human. Callers map any exception to exit 2 (fail closed).
"""
import fnmatch, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS = json.load(open(os.path.join(HERE, "..", "skills", "conductor-max", "references", "jev-questions.json")))
TIERS = ["fast", "balanced", "reasoning"]
DEFAULT_SEATS = {"fast": "haiku", "balanced": "sonnet", "reasoning": "opus"}
SECRET = re.compile(r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|"
                    r"(?i:(api[_-]?key|secret|token|password))\s*[:=]\s*['\"][^'\"\s]{12,}")
FAILS = re.compile(r"\b(\d+)\s+(failed|failing|failures?|errors?)\b", re.I)
MAX_DIFF = 60000
MAX_EVIDENCE = 8000


def out(decision, code, **info):
    return dict(decision=decision, exit=code, **info)


def triage(slice_, ask, seats=None):
    seats = dict(DEFAULT_SEATS, **(seats or {}))
    brief = {k: slice_[k] for k in ("goal", "acceptance", "allow", "gate") if k in slice_}
    ans = ask("triage", {"slice": brief})  # never the whole packet: bookkeeping fields can be huge
    tier_p = ans["tier"]["probabilities"]
    risk = float(ans["risk"]["score"])
    human = float(ans["needs_human"]["noul"])
    shared = float(ans["shared_surface"]["noul"])
    # Fail upward: the highest tier Jev gives real weight to, never the argmax alone.
    tier = [t for t in TIERS if float(tier_p.get(t, 0)) >= 0.25][-1]
    if risk >= 2.5:
        tier = "reasoning"
    info = dict(tier=tier, seat=seats[tier], risk=risk,
                review="frontier" if risk >= 2.5 else "jev",
                wave="serial" if shared >= 0.5 else "parallel",
                jev=dict(tier=tier_p, needs_human=human, shared_surface=shared))
    if human >= 0.5:
        return out("human", 3, reason="needs human approval", **info)
    return out("dispatch", 0, **info)


def changed_paths(diff):
    return sorted(set(re.findall(r"^\+\+\+ b/(\S+)", diff, re.M)))


def outside_allowlist(paths, allow):
    globs = [g.strip() for g in (allow or []) if g.strip()]
    if not globs:
        return []
    return [p for p in paths if not any(fnmatch.fnmatch(p, g) for g in globs)]


def review(acceptance, diff, ask, allow=None):
    paths = changed_paths(diff)
    if not diff.strip() or not paths:
        return out("fix", 1, reason="empty diff: nothing implemented, or new files are gitignored "
                                    "(commit them with git add -f)")
    outside = outside_allowlist(paths, allow)
    if outside:
        return out("fix", 1, reason="files outside allowlist", files=outside)
    added = "\n".join(l for l in diff.splitlines() if l.startswith("+"))
    if SECRET.search(added):
        return out("escalate", 3, reason="possible secret in added lines")
    if len(diff) > MAX_DIFF:
        return out("escalate", 3, reason="diff too large for one Jev pass; slice is not small")
    state = {"acceptance": acceptance, "diff": diff}
    risk = {k: float(v["noul"]) for k, v in ask("review_risk", state).items()}
    ans = ask("review_verdict", dict(state, risk=risk))
    sev = float(ans["severity"]["score"])
    verdict, conf = ans["verdict"]["choice"], float(ans["verdict"]["confidence"])
    info = dict(files=paths, risk=risk, severity=sev, jev_verdict=verdict, confidence=conf)
    if risk["security"] >= 0.5:
        return out("escalate", 3, reason="security-sensitive change", **info)
    if verdict == "escalate" or sev >= 2.5 or conf < 0.6:
        return out("escalate", 3, reason="severity or low confidence", **info)
    if verdict == "fix" or sev >= 1.5 or risk["scope_drift"] >= 0.7:
        return out("fix", 1, reason="defect or scope drift", **info)
    if risk["correctness"] >= 0.7:
        return out("escalate", 3, reason="approve contradicts correctness risk", **info)
    return out("approve", 0, **info)


def failure_count(evidence):
    return sum(int(n) for n, _ in FAILS.findall(evidence))


def qa(acceptance, evidence, exit_code, ask):
    evidence = evidence[-MAX_EVIDENCE:]
    fails = failure_count(evidence)
    state = {"acceptance": acceptance, "evidence": evidence}
    if exit_code != 0 or fails > 0:
        kind = ask("qa", state)["failure_kind"]
        info = dict(exit_code=exit_code, failures=fails, kind=kind["choice"], confidence=float(kind["confidence"]))
        if kind["choice"] == "environment" and info["confidence"] >= 0.7:
            return out("escalate", 3, reason="environment problem, not the slice", **info)
        if kind["choice"] == "flaky" and info["confidence"] >= 0.7:
            return out("retry", 1, reason="looks flaky: rerun once, then treat as code", **info)
        return out("fix", 1, reason="failing evidence", **info)
    p = float(ask("qa", state)["done"]["noul"])
    if p >= 0.8:
        return out("pass", 0, done_probability=p)
    return out("escalate", 3, reason="green exit but evidence does not show the AC met", done_probability=p)


def health(packet, ask):
    """Foreman-style worker supervision. Returns steer text or None; never blocks work."""
    a = ask("health", packet)
    p = {k: float(v["noul"]) for k, v in a.items()}
    if p["worker_stuck"] >= 0.7:
        msg = ("Supervisor: you appear stuck (repeating the same failing step). Stop, re-read the slice "
               "acceptance criterion, and try a materially different approach or report the blocker.")
    elif p["off_track"] >= 0.7:
        msg = ("Supervisor: recent actions look unrelated to the slice acceptance criterion. Return to "
               "the slice scope and the allowed files only.")
    else:
        msg = None
    return out("steer" if msg else "continue", 0, steer=msg, signals=p)
