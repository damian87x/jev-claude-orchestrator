"""Shared plumbing: API key lookup, the Jev call, and the decision ledger. Stdlib only.

The key is never printed, logged, or written to the ledger.
"""
import json, os, subprocess, time, urllib.error, urllib.request

BASE = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
PRICE_PER_INPUT_TOKEN = 0.042e-6  # USD; output tokens are free
PI_KEY_FILE = os.path.expanduser("~/.pi/agent/secrets/typesafe_api_key")
FALLBACK_DEFAULT = "http://127.0.0.1:8765"  # autonoxis server (Polaris)


class JevError(RuntimeError):
    """Any failure to get a well-formed answer. Callers must fail closed.
    `usage` keeps a billed-but-malformed Jev response's token count."""

    def __init__(self, msg, usage=None):
        super().__init__(msg)
        self.usage = usage


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the key to another host and let it answer as Jev: refuse (3xx -> HTTPError)."""

    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def project_dir():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def main_root(cwd=None):
    """The main checkout, even when called from a linked worktree, so every worker shares one state dir."""
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip()
    if common.endswith(os.sep + ".git"):
        return os.path.dirname(common)
    return cwd or project_dir()


def state_dir(cwd=None):
    d = os.path.join(main_root(cwd), ".jev-orchestrator")
    os.makedirs(d, exist_ok=True)
    return d


def dotenv_key(start):
    d = os.path.abspath(start)
    while True:
        path = os.path.join(d, ".env")
        if os.path.isfile(path):
            for line in open(path):
                name, _, value = line.strip().partition("=")
                if name.strip() in ("TYPESAFE_API_KEY", "export TYPESAFE_API_KEY") and value.strip():
                    return value.strip().strip("'\"")
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent


def api_key():
    """TYPESAFE_API_KEY env var, then the nearest .env, then the pi secrets file."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip() or dotenv_key(project_dir())
    if not key and os.path.exists(PI_KEY_FILE):
        key = open(PI_KEY_FILE).read().strip()
    if not key:
        raise JevError("no_key: set TYPESAFE_API_KEY or add it to .env")
    return key


def fallback_urls():
    """Local Jev-compatible servers (e.g. Polaris on :8765) tried in order when Jev fails.
    JEVO_FALLBACK_URLS is a comma list; set it empty to disable."""
    raw = os.environ.get("JEVO_FALLBACK_URLS", FALLBACK_DEFAULT)
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def malformed(out, questions):
    """Why `out` is not a usable answer to `questions`, or "" when it is."""
    if not isinstance(out, dict) or not isinstance(out.get("answers"), dict):
        return "no answers"
    def num(v, hi=1.0):  # a finite number in [0, hi]
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= hi

    for qid, q in questions.items():
        a = out["answers"].get(qid)
        t = q.get("type")
        if not isinstance(a, dict):
            return "missing " + qid
        if t == "noul" and not num(a.get("noul")):
            return "bad noul for " + qid
        if t == "choice" and (not isinstance(a.get("choice"), str) or a["choice"] not in (q.get("criteria") or {})
                              or not num(a.get("confidence"))):
            return "bad choice for " + qid
        if t == "score" and not num(a.get("score"), max(len(q.get("criteria") or ()) - 1, 0)):
            return "bad score for " + qid
    return ""


def post(url, body, headers, timeout):
    req = urllib.request.Request(
        url + "/v1/systemone", method="POST", data=json.dumps(body).encode(),
        headers=dict(headers, **{"Content-Type": "application/json", "Accept": "application/json",
                                 "User-Agent": "jev-claude-orchestrator/0.1"}))
    for attempt in range(4):
        try:
            t0 = time.perf_counter()
            with OPENER.open(req, timeout=timeout) as r:
                out = json.load(r)
            bad = malformed(out, body["questions"])
            if bad:
                raise JevError("malformed response: " + bad, out.get("usage") if isinstance(out, dict) else None)
            out["_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(float(e.headers.get("retry-after") or 2 ** attempt))
                continue
            raise JevError("http_%d" % e.code)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise JevError("transport: %s" % type(e).__name__)
    raise JevError("rate_limited")


def system_one(state, questions, model=None, timeout=30):
    """Jev first; on any failure, each local fallback in turn. The key is only ever sent to Jev.
    `_backend` says who answered: "jev" or the fallback URL."""
    body = {"state": state, "questions": questions, "model": model or MODEL}
    try:
        return dict(post(BASE, body, {"Authorization": "Bearer " + api_key()}, timeout), _backend="jev")
    except JevError as e:
        errors, jev_usage = ["jev: %s" % e], e.usage
    for url in fallback_urls():
        try:
            return dict(post(url, body, {}, timeout), _backend=url, _jev_error=errors[0], _jev_usage=jev_usage)
        except JevError as e:
            errors.append("fallback %s: %s" % (url, e))
    raise JevError("; ".join(errors), jev_usage)


def cost(res):
    """Jev's list price. A local fallback costs nothing we can measure, but a malformed Jev reply it
    replaced may still have been billed."""
    if res.get("_backend", "jev") != "jev":
        return ((res.get("_jev_usage") or {}).get("input_tokens") or 0) * PRICE_PER_INPUT_TOKEN
    return res.get("usage", {}).get("input_tokens", 0) * PRICE_PER_INPUT_TOKEN


def log(event, **fields):
    """Append one decision to .jev-orchestrator/ledger.jsonl. Never pass secrets here."""
    row = dict(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), event=event, **fields)
    with open(os.path.join(state_dir(), "ledger.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def git(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd or project_dir(), capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ""
