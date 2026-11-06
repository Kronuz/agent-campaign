# External research for Campaign 2.0

> Status: CURRENT (2026-11-05). Sources accessed 2026-11-05. Raw notes with line citations were
> gathered by one research agent and checked against the repositories where a decision depends
> on them. "Documented" means the project's own code or docs say so; "claimed" means a promotional
> or first-party number nobody outside reproduced.

## Systems compared

| System | Problem it solves | Extra software | Verification | Recovery | Concurrent writers | Evidence |
|---|---|---|---|---|---|---|
| [Superpowers](https://github.com/obra/superpowers) v7.0.2 | Skills-based plan → execute → review discipline | None (bash, markdown) | TDD evidence in the implementer's report; a script records a task done only after running the tests (`skills/executing-plans/scripts/task-done:42-55`); per-task and whole-branch review | Plan-scoped ledger whose first line names the plan, plus `git log` (`references/resuming.md:6-12`) | Not supported: "Never dispatch multiple implementation subagents in parallel" | Maintainer evals: 25/25 resumes refused a stale ledger, ~9 tool calls each. Cost table: controller ~half of a run's spend. |
| [Beads](https://github.com/gastownhall/beads) v1.3.4 | Durable dependency-aware issue memory for agents | `bd` binary, embedded or server Dolt, schema migrations | None (left to the layer above) | Dolt is the source of truth; lease + heartbeat + `reclaim` for dead claimers | Dolt cell merge; atomic `--claim` | Heavy release cadence; 1,606 open issues. **Removed** its JSONL-in-git + SQLite design in v0.50-0.51 (`CHANGELOG.md:11390-11520`). |
| [Gas Town](https://github.com/gastownhall/gastown) v1.2.1 | 10-30 Claude Code instances with persistent state | `gt`, `bd`, Dolt server, tmux, daemon, several standing roles | Pluggable test gates in a merge queue (batch then bisect) | Work "hooks" in Beads; watchdog nudges | One worktree per agent; serialized merge queue | Author: "expensive as hell", "some work gets lost", "hands-on-the-wheel" (2026-01-01 post). Scale claims unverified. |
| [claude-swarm-skill](https://github.com/linkbag/claude-swarm-skill) v0.3 | Parallel worktree tasks in Claude Code | jq | Reviewer agent verdict; review rounds capped at 3 | None documented | Disjoint file sets by construction; sequential `--no-ff` merges stop on conflict | Weak: 0 stars, one day of commits; its own docs admit the live-state JSON race |
| [orchestration-skill](https://github.com/Cephalode/orchestration-skill) | Minimal plan → workers → validator | None | Validator runs build, tests, type check, lint | None | Planner assigns file ownership per task | Weak. Confirms a three-role core is workable. Its env-var security hook likely never fires (hooks get JSON on stdin). |
| [Overstory](https://github.com/Nokodoko/overstory) (snapshot of jayminwest/overstory) | Swarm in tmux worktrees with SQLite mail, merge queue, watchdog | Bun, tmux, SQLite, daemon | Reviewer agents | Checkpoints, watchdog tiers | FIFO merge queue, 4-tier resolver ending in AI merge | Its own `STEELMAN.md` lists cost amplification and "coordination theater"; no outcome data |
| Anthropic guidance | Skills, long-running harnesses, multi-agent research, context engineering | n/a | Separate skeptical evaluator; end-to-end tests; JSON feature list with a `passes` field | Progress file + git log read at session start; resume from where the error happened | "Most coding tasks involve fewer truly parallelizable tasks than research" | First-party: multi-agent ~15× chat tokens; +90.2% on an internal research eval |
| OpenAI guide, Codex docs | Agent design; Codex subagents (`.codex/agents/*.toml`) | n/a | n/a | `codex exec resume` | "Be more careful with parallel write-heavy workflows" | Vendor docs |
| GitHub Copilot CLI docs | Custom agents, `/fleet`, skills in `.agents/skills` | n/a | n/a | `--resume`, `/resume` | `/fleet` "may not provide any benefit" for sequential work | Vendor docs |

## What each system teaches campaign

1. **Superpowers** is the closest in spirit and the most useful. It shows that a methodology can
   stay in markdown and a few scripts, that completion should be written by the script that ran
   the check rather than by the agent's narrative, that reviews need a round cap with a breaker,
   and that a final single fix wave beats per-finding loops. It also shows the cost of
   per-task fresh implementer plus reviewer: its maintainers built an "inline" mode with one final
   review to avoid it. It does not do parallel writers, which campaign needs.
2. **Beads** contributes semantics, not software: `ready` is derived (open, no open blocking
   dependency), claims are atomic, dependency kinds differ in whether they block readiness, and a
   dead claimer is detected by an expired lease rather than by an agent's say-so. Its history is
   the strongest single piece of evidence in this research: **git-merged JSONL with a SQLite
   cache and a daemon did not survive concurrent writers**, and the project replaced it with a
   database server. The lesson for campaign is to keep one writer, not to adopt Dolt.
3. **Gas Town** validates durable assignments and a serialized integration point, and warns,
   in its author's words, about cost and lost work when many agents churn. Its daemon, standing
   roles and multiple heartbeat stores are what campaign must not grow.
4. **The small skills** (swarm, orchestration) confirm two cheap rules: assign disjoint file sets
   up front, and integrate sequentially, stopping on the first conflict. Neither recovers from a
   lost session.
5. **Overstory** contributes one principle worth stating: observable state (git, worktrees,
   process liveness) beats recorded state. Its own steelman explains why the rest is overhead.
6. **Anthropic** contributes the harness facts campaign already relied on (skeptical separate
   evaluator, progress file + git, criteria the builder cannot edit) and one new one: models are
   less likely to clobber JSON than markdown. Its skill guidance (SKILL.md under 500 lines,
   references one level deep, add only what the model lacks) sets the shape of the core.
7. **Harness capabilities differ** (from vendor docs; see `references/harness.md` in the skill):
   Claude Code agent files pin model and effort and support worktree isolation and background
   runs; Copilot agent files pin a model but not effort, and Copilot's `task` tool takes effort
   per launch; Codex reads `.codex/agents/*.toml` with model and reasoning effort and pushes
   subagent results to the parent. None of them lets a subagent sleep cheaply on a long command:
   a waiting turn rereads the context.

## Adopted, adapted, rejected

| Mechanism | Decision | Where it lands in 2.0 |
|---|---|---|
| Completion recorded by the script that ran the check | **Adopted** | `campaign land` reads job records at the head commit; an agent's report is never evidence |
| Derived ready queue, typed dependencies | **Adapted** | `needs` blocks starting, `after` blocks landing; `campaign status` derives readiness |
| Single writer for shared state; JSON for machine state | **Adopted** | `docs/campaign/state.json`, written only through `campaign` from the home repository's main checkout |
| Lease and heartbeat for dead workers | **Adapted** | Job runners write a heartbeat; a stale heartbeat makes a job `interrupted` and requeues it; no agent leases (in-process subagents die with their lead) |
| Observable state beats recorded state | **Adopted** | `campaign check` reconciles state with branches, worktrees and job records; git wins |
| Disjoint file sets per writer | **Adopted** | Package `files` globs; `campaign check` reports files changed on two active branches |
| Sequential integration that stops on conflict | **Adopted** | `campaign land` fast-forwards one rebased branch at a time |
| Review round cap with a breaker; one fix wave | **Adopted** | Two fix rounds per review, then the lead re-plans or asks the owner |
| Inline mode with one final review | **Adapted** | Low and normal packages run one builder and at most one review |
| Brief, report and diff from a recorded base | **Adapted** | Package brief (lead), work file (builder), `base` recorded at `campaign start` |
| "Rulings I made" list in the builder's report | **Adopted** | Work file section, read by the reviewer |
| Explicit model on every launch | **Adopted** | SKILL.md model table; prompts name the model |
| Integration branch per package set (Gas Town) | **Adapted** | A package branch with optional task branches merged into it by the lead |
| Batch-then-bisect merge queue | Rejected | Campaign lands a few branches a day; one-at-a-time fast-forwards suffice |
| Dolt, Beads, Gas Town runtime | Rejected | A database server and migrations for a problem one JSON file and one writer solve |
| Watchdog daemons, agent-to-agent mail | Rejected | The job worker exits when idle; agents report to the lead only |
| Per-task fresh implementer plus full review | Rejected | Measured as the dominant cost by its own maintainers |
| AI merge-conflict resolution as default | Rejected | Semantic conflicts go back to the package's builder |
| Agent teams, `/fleet` as the backbone | Rejected | Experimental; in-process teammates are not restored on resume |
| Mandatory-trigger bootstrap | Rejected | The core skill is invoked when the work is a campaign |

## Decisions the research changed

1. Machine state moved from markdown front matter (my first draft) to one JSON file written by
   the tool, because of Beads' history and Anthropic's clobbering observation.
2. Recheck agents are replaced by deterministic reruns, following Superpowers' `task-done`.
3. The job worker exits when the queue is empty rather than running as a daemon, after Gas Town
   and Overstory showed the operating cost of standing processes.

## Gaps

- No system publishes outcome measurements comparable to campaign's own records; all cost
  comparisons below rest on campaign's logs and labelled estimates.
- Copilot CLI inter-agent messaging, hook events beyond `SessionStart`, and Codex CLI worktree
  behavior were not verified. Codex is not installed on the test host.
