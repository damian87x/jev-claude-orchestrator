---
name: conductor-max
description: Madmax conductor — split a goal into up to ~50 tiny slices, run them as parallel waves of slice-worker subagents, and let TypeSafe Jev make every routing, review, QA and supervision decision in under a second for fractions of a cent. Use with /conductor-max when a goal is large but decomposes into many small, independently testable changes and speed/cost matter. Not for one-file fixes or exploratory prototypes.
---

# Conductor Max

**Large model plans. Jev decides. Workers execute. Verification checks.**

You (the main session) plan once and adjudicate escalations. `slice-worker*` subagents write code.
Jev answers every per-slice snap judgment; thresholds and vetoes live in code and fail closed. The
plugin's hooks supervise workers while they run:

- **SubagentStop gate:** when a slice worker tries to finish, the hook reruns the slice gate, asks Jev
  for QA and a staged review, and **sends the worker back** (with the reason) until it passes. After
  `max_blocks` (default 2) rounds, or on any Jev error, the slice is marked `escalate` for you.
- **PostToolUse health:** every `health_every` (default 8) tool calls, Jev checks whether the worker is
  stuck or off track and injects a steer into its context. It never blocks a tool.

## Setup check

`J="<this skill's base directory>/../../scripts/jevo.py"`, then run `python3 $J slice list`.
The Jev key comes from `TYPESAFE_API_KEY`, else `TYPESAFE_API_KEY=` in the project's `.env`,
else `~/.pi/agent/secrets/typesafe_api_key`. With no key, or with Jev failing, jevo uses the local fallback
below. Only when no backend answers does a stage exit 2. Treat that as escalate; never skip a stage.

**Fallback when Jev is down:** jevo then asks the local servers in `JEVO_FALLBACK_URLS`, default
`http://127.0.0.1:8765` (Polaris); set it empty to disable. A fallback may route slices and send them
back to fix, but it can't approve one: its approval returns exit 3 with `backend` and `fallback_decision:
approve`, and you then dispatch `frontier-reviewer`. If no backend answers, the stage exits 2.

## Commands

```
python3 $J slice new --id S3 --goal "..." --acceptance "..." --allow 'src/cart.py,tests/test_cart.py' --gate 'pytest -q tests/test_cart.py'
python3 $J triage --slice S3          # tier/seat, risk, wave, needs_human
python3 $J check  --slice S3          # gate -> Jev QA -> Jev staged review (the stop hook runs this too)
python3 $J slice list                 # status per slice: new / fixing / approved / escalate
python3 $J report --html .jev-orchestrator/scoreboard.html
```

Exit codes: **0** proceed/approve/pass · **1** fix/reject/retry · **3** escalate · **2** error → escalate.

| stage | runs in code first (no Jev) | Jev asks | policy in code |
|---|---|---|---|
| triage | — | tier, risk, needs_human, shared_surface | highest tier with p ≥ 0.25 (fail upward); risk ≥ 2.5 → reasoning + frontier review; needs_human ≥ 0.5 → stop; shared ≥ 0.5 → serial |
| qa | runs the gate; parses failure counts; exit code | failing: code / flaky / environment · green: does the evidence show the AC met? | flaky ≥ 0.7 → retry; env ≥ 0.7 → escalate; green needs done ≥ 0.8 |
| review | empty diff, files outside `allow`, secret patterns, diff > 60k | risk nouls, then severity + verdict (two staged calls) | security ≥ 0.5 / verdict escalate / severity ≥ 2.5 / confidence < 0.6 → escalate; fix / severity ≥ 1.5 / drift ≥ 0.7 → fix |
| health | — | stuck, off_track, progress | stuck or off_track ≥ 0.7 → steer |

## Loop

1. **Scope + authority.** Restate the goal. Quote what the human authorizes (commits, pushes, PRs,
   deploys). By default workers commit locally only. A Jev answer never authorizes an irreversible action.
2. **Plan once.** Build a slice DAG. A slice is one acceptance criterion, ≤ ~3 files, ≤ ~150 changed
   lines, and has its own fast gate command. If a slice can't be that small, split it. Create each with `slice new`.
3. **Triage all slices** in one burst. Exit 3 goes to the human list. `wave: serial` slices run after
   the parallel wave, one at a time.
4. **Dispatch waves.** Launch ready slices (deps met) in parallel, in one message, up to the cap:
   default **12** in flight, raise toward 50 when slices touch disjoint files. Pick the agent from
   triage's tier: `slice-worker-fast` (haiku), `slice-worker` (sonnet), `slice-worker-reasoning` (opus).
   Every worker runs in **its own git worktree** (agent `isolation: worktree`). Never run two workers in
   one checkout: each would see the other's files and fail the allowlist. Cut all slices from the same
   HEAD you dispatch from, and use `run_in_background` for big waves.
   **The worker prompt must start with `JEV-SLICE: <id>`** (that's how the hooks find the slice), then
   the packet inline (the slice file isn't visible inside the worktree), then "write the test first,
   commit in your worktree, never push".
5. **Collect.** When a worker returns, read `slice list`. `approved` → merge that worker's worktree
   branch in dependency order. `escalate` → dispatch `frontier-reviewer` with the slice id and the ledger
   reason (`.jev-orchestrator/ledger.jsonl` has the gate exit and output tail). Its `VERDICT: fix` → cut
   a **new** slice (`S3-fix1`) and a fresh worker, never re-run the old one. `human` → the human list.
   Triage `review: frontier` slices also get a frontier-reviewer pass even when Jev approved.
   **Top-model fallback:** a worker run is one attempt (its stop-gate rounds are part of it). After
   **three** failed attempts on one slice (`S3`, `S3-fix1`, `S3-fix2`), cut **one** last fix slice for the
   highest model: `slice-worker-reasoning` with `model: "opus"` (Opus 5.5), or Astra through Codex
   (`codex exec -m gpt-6-astra`). Pick the family that did not write the earlier attempts: Astra after
   Claude workers, Opus 5.5 after Codex or other workers. Pass it every earlier failure reason verbatim. The other model reviews
   it: Astra reviews an Opus fix, `frontier-reviewer` (Opus) reviews an Astra fix. Note the switch in
   the scoreboard summary. If it still fails, the slice goes to the human list. Never make a fifth
   attempt and never drop to a cheaper tier. `needs_human`, security and authority escalations skip the
   fallback and go straight to the human.
6. **Integration verify** after each wave merges: full test/lint/typecheck. Red → find the culprit merge,
   revert it, requeue it as a fix slice.
7. **Runtime proof** for user-facing goals: drive the running app (browser QA, or Reticle if installed).
   "Couldn't tell" is not pass.
8. **Scoreboard:** `report --html`, plus a short summary: slices approved / escalated / human, fix rounds,
   total Jev `cost_usd`. Only report numbers the ledger measured.

## Rules

- Jev reads `state` literally and treats it as trustworthy, so worker output can argue with it. The code
  vetoes (allowlist, secrets, failure counts, exit codes) run first and Jev can't override them.
- Jev can't count or do arithmetic: numbers are parsed in code; Jev only gives the judgment.
- One writer per file per wave. Two slices on one file → serialize them or merge them into one slice.
- A worker's "done" is not evidence. The stop gate and your integration verify are.
- Don't let dispatch outrun review: if escalations pile up past the cap, stop dispatching and clear them.
- A high escalation rate means the slices are too big. Re-slice instead of raising thresholds.
- Three failed attempts on a slice → one top-model attempt (Opus 5.5 or Astra, reviewed by the other) → human. No fifth attempt.
- No push, PR, deploy or ticket write outside quoted authority. No invented prices or savings.
