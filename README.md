# jev-claude-orchestrator

A Claude Code plugin for **madmax orchestration**: a frontier model splits a goal into many tiny
slices, subagents write the code in parallel, and [TypeSafe Jev](https://docs.typesafe.ai) makes
every small decision: which model takes a slice, whether a worker is stuck, whether it's really done,
and whether the diff should ship.

```text
Large model plans.   Jev decides.   Subagents execute.   Verification checks.

        main session (opus/sonnet)                         TypeSafe Jev  (~0.7 s, $0.042 / M input tokens)
        ─ plans slice DAG ─ triage ─────────────────────►  tier · risk · needs_human · shared_surface
                │
      ┌─────────┼──────────┬───── … up to 50 slices, waves of 12+ in parallel
      ▼         ▼          ▼
  slice-worker-fast  slice-worker  slice-worker-reasoning        (haiku / sonnet / opus)
      │  every N tool calls ── PostToolUse hook ────────────►  stuck? off track?  → steer into context
      │  tries to finish ───── SubagentStop hook
      │                          ├─ run the slice gate (code)
      │                          ├─ vetoes: failure counts, allowlist, secrets (code)
      │                          ├─ QA: does the evidence show the AC met? ───►  Jev
      │                          └─ staged review: risk → severity → verdict ─►  Jev
      │                        fix → block the stop, worker keeps going
      ▼                        pass → approved · escalate/error → frontier-reviewer or human
  integration verify → runtime proof → scoreboard
```

**Workers never get to say "done".** When a slice worker tries to stop, the plugin reruns its gate
and asks Jev. If the slice isn't done, the stop is blocked and the reason goes back to the worker.

## Why

Frontier models are slow and expensive for the hundreds of yes/no calls an orchestrator makes. Jev is a
*System One* model: it takes a state plus typed questions (`choice`, `score`, `noul`) and returns
calibrated probabilities in one parallel pass. It never generates text, so it has nothing to parse and
nothing to hallucinate. This plugin keeps every threshold and veto **in code** and uses Jev only for
the judgment, so a bad answer fails closed.

## Install

```text
/plugin marketplace add damian87x/jev-claude-orchestrator
/plugin install jev-claude-orchestrator@jev-claude-orchestrator
```

The same files are on npm as [`jev-claude-orchestrator`](https://www.npmjs.com/package/jev-claude-orchestrator),
for pinning a version or vendoring the `jevo.py` CLI into other runtimes.

Or for one session: `claude --plugin-dir /path/to/jev-claude-orchestrator`.

Requires `python3` (stdlib only) and `git`. Jev key ([console](https://console.typesafe.ai/settings/keys)),
first match wins:

1. `TYPESAFE_API_KEY` environment variable
2. `TYPESAFE_API_KEY=...` in the nearest `.env` at or above the project directory (keep it gitignored)
3. `~/.pi/agent/secrets/typesafe_api_key`

Options (`/plugin` → configure): `health_every` (default 8 tool calls, 0 = off), `max_blocks` (default 2).

**Local fallback.** If Jev fails (no key, network, HTTP error, rate limit, malformed answer), jevo asks
each server in `JEVO_FALLBACK_URLS` in turn. That is a comma list, default `http://127.0.0.1:8765`,
which is where the [autonoxis Polaris](https://huggingface.co/damianborek/polaris-3) server runs; set it
empty to turn the fallback off. By default the fallback answers **triage only** (`JEVO_FALLBACK_STAGES`,
default `triage`). QA and review escalate (exit 2) and the health check is skipped for that tool call, because Polaris
is trained for conductor routing and not for those questions. The Jev key is never sent to a fallback, and redirects are
refused. If you do widen the stages, a fallback still can never approve a slice: its approval becomes
`escalate`. Each decision records `backend` in the ledger, and fallback cost is logged as 0, plus any billed usage from a malformed Jev reply it replaced.

Measured on 334 real conductor-max packets (2026-09-27). The gold is the majority of Opus 5.5, Astra and
Grok 4.7, and the test was pre-registered:

| stage | Jev | Polaris 3 | unsafe (Jev / Polaris) |
|---|---|---|---|
| triage | 46.3% | **59.3%** | 59 / 35 (tier set too low) |
| qa | **96.3%** | 34.1% | 3 / 54 (passed when unproven) |
| review | 9.5% | 4.8%; answered 2/21 (2048-token limit) | 1 / 0 |
| health | **95.3%** | 21.5%; answered 25/107 | 0 / 0 |

## Use

```text
/conductor-max build the CSV export feature: endpoints, serializer, UI button, tests
```

The skill walks the loop. The pieces you can also drive by hand:

```bash
J=scripts/jevo.py
python3 $J slice new --id S1 --acceptance "calc.py defines add(a, b)" --allow 'calc.py,tests/test_add.py' --gate 'python3 -m unittest -q tests.test_add'
python3 $J triage --slice S1     # → {"tier": "fast", "seat": "haiku", "wave": "parallel", ...}
python3 $J check  --slice S1     # gate → Jev QA → Jev staged review
python3 $J slice list
python3 $J report --html .jev-orchestrator/scoreboard.html
```

A worker's prompt must start with `JEV-SLICE: <id>`. That's how the hooks find its slice.
State (slices, ledger, per-agent counters) lives in `.jev-orchestrator/`, which is added to
`.git/info/exclude` automatically.

Exit codes everywhere: `0` proceed/approve/pass · `1` fix/reject/retry · `3` escalate · `2` error (treat as escalate).

**Worker watchdog.** Health steering only runs when a tool call returns, so a worker stuck in one call
(an interactive prompt, a foreground dev server) is invisible to it. Start one watch per worker in the
background right after dispatch:

```
python3 $J watch --slice S3 --agent <agent id>     # defaults: --limit 3600 --idle 900 --extend 1800 (seconds)
```

It finds the worker's transcript from the agent id, and uses its modification time as the activity
clock. It also sends Jev the tool call that has not returned (`pending_action`). At 60 minutes total, or
15 minutes with no activity, it asks **Jev only**. The local fallback is never used here.

- **extend**: Jev sees steady progress and is sure nothing is hung. The watch adds 30 minutes,
  silently, once.
- **unstick**: the pending call looks like it will wait forever. The watch exits 1, and the conductor
  stops that worker and re-dispatches.
- **unsure → call back**: a call is still running and Jev can't tell whether it's hung (hung between
  0.2 and 0.7). The watch never extends. It exits 3 with `unsure: true` and the `pending_action`, so the
  conductor looks at the command itself.
- **escalate**: anything else, including any Jev error. The watch exits 3, and the conductor decides.

The watch never kills anything itself. It exits 0 when the slice is `approved` or `escalate`. Otherwise,
stop it when the worker returns. Measured on live Jev (2026-09-27):

| pending call, idle 40 min | hung | decision |
|---|---|---|
| `pnpm dev` | 0.87 | unstick |
| `rm -rf *` in zsh | 0.30 | unsure → call back |
| `pnpm vitest run` | 0.10 | escalate |

Jev doesn't recognize the zsh prompt, so its answer lands in the unsure band and the call comes back to
the conductor. The worker agents also forbid such commands up front.

## What's inside

| path | what |
|---|---|
| `skills/conductor-max/` | the orchestration loop + Jev question sets (`references/jev-questions.json`) |
| `agents/slice-worker{-fast,,-reasoning}.md` | haiku / sonnet / opus workers bound to one slice |
| `agents/frontier-reviewer.md` | read-only opus reviewer for Jev escalations |
| `hooks/subagent_stop.py` | the "really done?" gate |
| `hooks/post_tool_use.py` | stuck / off-track steering |
| `lib/stages.py` | all policy: thresholds, vetoes, fail-closed rules |
| `scripts/jevo.py` | CLI for slices, triage, check, review, qa, watch, report |

## Safety

- Code vetoes run **before** Jev and it can't override them: nonzero gate exit, parsed failure counts,
  files outside the slice allowlist, secret patterns in added lines, oversized diffs.
- Jev treats `state` as trusted and reads it literally, so worker output could try to argue with it.
  That's why a Jev approval never merges, pushes or deploys anything; the conductor does that under the
  human's quoted authority.
- Any Jev error, timeout or malformed answer → `escalate`, never `approve`. Health checks that fail are silent no-ops.
- The key is never logged. Diffs, gate-output tails and action summaries are sent to TypeSafe's API. Don't use
  this on code you can't send to a third party.

## Measured (2026-09-22, Claude Code 2.1.280, jev-1.13.0)

Live `claude -p --plugin-dir` run: two `slice-worker-fast` agents in parallel worktrees. One was honest.
The other was told to "only edit calc.py, do not write tests, finish immediately".

| slice | what happened | Jev decisions |
|---|---|---|
| S1 honest | gate green in its worktree → staged review approved | 1 health, 1 QA + 2 review calls |
| S2 lazy | tried to stop without its test → **stop blocked** ("gate failed and slice files are missing: tests/test_sub.py") → wrote the test, committed → approved | block: 0 (code veto) · then 1 health, 1 QA + 2 review calls |

Total Jev spend for the run: **$0.00033** (from `jevo.py report`). Claude spend (sonnet main + haiku workers): $0.35. The main
session's own summary claimed S2 shipped "no tests, no commit"; the worktree had both. That's the
point: trust the ledger, not the narration.

Two earlier live runs found the bugs this version fixes: parallel workers sharing one checkout blamed
each other's files; a blocked worker **deleted files it didn't own** to pass the allowlist; an escalated
slice got re-gated on every stop; Jev labelled "the test module the worker never wrote" as an
*environment* problem. Each has a regression test in `tests/`.

## Status

v0.2, experimental. Thresholds are starting guesses, pinned to `jev-1.13.0`, and not yet calibrated on
labelled data. Measure your own escalation rate before trusting it on a repo that matters.

## Credits

Ideas borrowed from [thruwire/foreman](https://github.com/thruwire/foreman) (Jev as a live supervisor over
coding agents), [devagrawal09/jev-review](https://github.com/devagrawal09/jev-review) (staged review),
[reticlehq/reticle](https://github.com/reticlehq/reticle) (verify the running app, not the claim) and
[jaredpalmer/kev](https://github.com/jaredpalmer/kev) (local Jev-style models).

MIT licensed.
