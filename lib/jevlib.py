"""Shared plumbing: API key lookup, the Jev call, and the decision ledger. Stdlib only.

The key is never printed, logged, or written to the ledger.
"""
import json, os, subprocess, time, urllib.error, urllib.request

BASE = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
PRICE_PER_INPUT_TOKEN = 0.042e-6  # USD; output tokens are free
PI_KEY_FILE = os.path.expanduser("~/.pi/agent/secrets/typesafe_api_key")


class JevError(RuntimeError):
    """Any failure to get a well-formed answer. Callers must fail closed."""


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


def system_one(state, questions, model=None, timeout=30):
    body = {"state": state, "questions": questions, "model": model or MODEL}
    req = urllib.request.Request(
        BASE + "/v1/systemone", method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + api_key(), "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "jev-claude-orchestrator/0.1"})
    for attempt in range(4):
        try:
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.load(r)
            out["_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if not isinstance(out.get("answers"), dict):
                raise JevError("malformed response: no answers")
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(float(e.headers.get("retry-after") or 2 ** attempt))
                continue
            raise JevError("http_%d" % e.code)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise JevError("transport: %s" % type(e).__name__)
    raise JevError("rate_limited")


def cost(res):
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
