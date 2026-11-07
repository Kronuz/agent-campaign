# Campaign 2.0 architecture

> Status: CURRENT (2026-11-05), revised after the independent challenge (`challenge.md`). Decisions and their reasons. The skill itself
> (`home/.agents/skills/campaign/SKILL.md`) is what agents read; this file is for whoever has to
> change it. Evidence for the problems it solves: `audit.md`; outside ideas: `research.md`.

## Vocabulary

The skill names its parts after the tabletop campaign it is named for. This record keeps the
brief's engineering terms; the shipped skill, tool and agent types use the themed ones.

| This record | The skill and the tool |
|---|---|
| owner | patron |
| lead | game master |
| package | quest (`campaign new`, `quest/<id>` branches, `docs/campaign/quests/`) |
| builder | adventurer (agent type) |
| reviewer | paladin (agent type); lenses keep their technical names |
| sweep, explorer | scouting party, scout |
| work file | journal (`docs/campaign/journal/`) |
| lessons | lore (`references/lore.md`) |
| assignment (design, research, performance, docs, mechanical) | adventurer class: Sage, Wizard, Artificer, Scout, Bard, Squire (`references/classes.md`) |

Classes restore the methods 1.x carried in its architect, designer, researcher, performance
engineer, explorer, tech writer and maintenance engineer files, without restoring them as roles:
one agent type, one authority, no handoff between classes, the class and model named per launch.

## The idea in one paragraph

A lead turns the owner's goals into **packages**: small, independently verifiable outcomes, each
with acceptance criteria, a risk level and a file set. **Builders** implement packages on their
own branches; machine work (builds, tests, fuzzing, benchmarks) goes to a **job queue** that runs
it on immutable commits under `quiet` while no agent waits. A **reviewer** with a fresh context
examines a whole package when its risk calls for it, spending on what the evidence does not
already establish. **`campaign land`** fast-forwards a package onto `main` only when the checks
ran green at that exact commit and the required reviews were accepted for that exact change.
State is one JSON file written only through the tool, plus prose briefs, plus git.

```
owner goals ─► PLAN ─► packages (risk, criteria, files, needs/after)
                          │ campaign start
                          ▼
              builder on pkg/<id> ─► jobs (queue → quiet → results) ─┐
                          │ ready for review                         │ evidence at a commit
                          ▼                                          ▼
              reviewer (lens by risk) ─► lead accepts ─► campaign land (ff main) ─► landed
```

## What changed from 1.x and why

| 1.x | 2.0 | Reason |
|---|---|---|
| 15 roles, 14 agent types | Lead, builder, reviewer; 2 agent types | Roles differed in assignment, not authority or independence (`audit.md`, "Roles") |
| Workstream per branch, reviewed per branch | Package per outcome, reviewed once as a whole | Review cost scaled with branch count; defects scale with risk |
| Ledger + status index + exploration log + INDEX + status lines | `state.json` (tool-written) + brief + work file + git | One source of truth per fact; the index is derived |
| Review tiers: floor, adjust, defaults, auditor | Four risk levels, each naming its lenses | One rule a lead can apply without another agent |
| Rechecker, proof auditor | Rerun jobs; reviewer judges proof | Rechecks confirmed closures at ~134k each |
| Agents hold `quiet` while waiting | Jobs on immutable commits; worker exits when idle | Agents waited up to 7 h; polling rereads context |
| `quiet` command runs on unheld past its deadline | Strict and exclusive commands are killed at the deadline | A measurement must not outlive its isolation |
| `pace advise` targets 95-97% use, floor of 2 agents | `pace` reports; no target | Utilization was manufacturing work |
| `lease` rents AWS machines | Campaign provisions nothing | Prose-guarded spending already lost money |
| Phase gates with reports and reviews; system review every gate | Milestones are packages with a full-validation check; system review on detected conflicts | Calendar reviews repeated what event detection can target |

## Concepts

### Package

The unit of planning, review and acceptance. One coherent behavior a reviewer can hold in mind:
usually a few hundred to ~1,500 changed lines. Fields (`state.json`):

- `risk`: `low`, `normal`, `high`, `critical` (below).
- `needs`: packages that must **land before this starts**. `after`: packages that must **land
  before this lands**. Work may start against an `after` dependency because its contract is fixed
  in its brief; that is how 1.x's "gates decide landing, not starting" survives without building
  on unstable assumptions.
- `files`: globs this package may change. `campaign start` refuses a package or task whose globs
  may overlap an active one in the same repository (forcing needs a recorded reason), and `land`
  refuses changes outside them. `provides` and `consumes`: contract names (an API, a format, a
  protocol) so the tool can see two packages touching one contract.
- `checks`: named commands that must pass at the landed commit (run as jobs).
- `lenses`: the reviews required before final landing (from risk; the lead may add).
- `branch`, `worktree`, `base`, optional `tasks` (parallel writers inside one package, each on
  `pkg/<id>--<task>`, merged into the package branch by the lead).
- `accepted`: per lens, the commit, the merge base, the quest file's hash, and the mode and blob of
  each changed path when the lead accepted it, or a waiver with its reason.
- `landed`: each landing (increments and final) with the evidence that justified it.

The **brief** (`docs/campaign/packages/<id>.md`) holds the prose: outcome, acceptance criteria
(A1, ...), contracts, decisions, what may land early. The lead writes it; builders never edit it.

### Roles

| Role | Authority | Independence | Writes |
|---|---|---|---|
| Owner | Goals, non-goals, reserved decisions | | `PLAN.md` goals |
| Lead (main session) | Plans, assigns, accepts, lands | Holds the whole picture | `state.json` (through `campaign`), briefs, `PLAN.md`, `main` |
| Builder (agent type) | Its branch | Shares the plan, not the reviewer's context | Code, tests, its work file, design notes |
| Reviewer (agent type) | Rules on one package through one lens | Fresh context, never forked | One review file |

Designing, investigating, optimizing, documenting and mechanical changes are **assignments** to a
builder, chosen with a model and effort per launch. A **sweep** launches many cheap explorer
assignments at once (Sonnet class, medium), one target and one package each, disjoint areas: the
1.x explorer swarm, which had the best yield in the records (20 of 33 runs landed, median 93k
tokens), without its handoff to a second agent or its fights over `quiet` holds. Architecture review, code review, security
review and system review are **lenses** of the reviewer. An assignment that needs a different
model is a launch parameter, not a role.

### State

| File | Writer | Content |
|---|---|---|
| `docs/campaign/PLAN.md` | owner, lead | goals, non-goals, constraints, reserved decisions, milestones, decision log |
| `docs/campaign/state.json` | `campaign` only, from the home repository's main checkout | packages and their machine state |
| `docs/campaign/packages/<id>.md` | lead | brief |
| `docs/campaign/work/<id>.md` (on the package branch) | builder | doing, next, blocked, evidence by criterion, rulings made |
| `docs/campaign/reviews/<id>-<lens>.md` | reviewer (lead commits) | verdict, findings, probes, not run |
| `~/.scratch/campaign/jobs/<job>/` | job runner | spec, state, log, outputs (host-local) |
| git history | everyone | every transition (`campaign` commits `state.json` on each change) |

Derived, never stored: the status index, readiness, overlaps, staleness, landability. A fresh lead
runs `campaign status` and `campaign check` and has the picture.

Why JSON for state: one writer (the tool) makes concurrent-write corruption impossible by
construction, the schema is validated on every write, and models do not hand-edit it. Prose stays
in markdown where people read it.

## Review

The question for every review: **what uncertainty remains after the evidence, and what is the
cheapest reliable way to resolve it?**

| Risk | Meaning (first match wins) | Before building | Before final landing |
|---|---|---|---|
| critical | Irreversible: freezing a format or protocol for release, migrating users' data, removing user-visible behavior | Owner approval; one design review | Each high lens, plus acceptance evidence the reviewer ran itself |
| high | Persistent formats, wire protocols, durability, concurrency, security surfaces, shared public APIs | One design review when a decision is expensive to reverse | One reviewer, by the lens of the dominant risk, carrying every risk question; a second only for a named question needing separate expertise |
| normal | Ordinary features and fixes | — | One `correctness` review, or a recorded waiver for a small change with strong evidence |
| low | Docs, tests, tooling, mechanical edits, behavior-preserving refactors | — | Checks green; the lead reads the diff |

Rules that keep this cheap:

1. **One review per package by default.** Not per branch, task or design revision; a second
   lens only with a recorded reason.
2. **Reviewers start from the evidence.** Jobs are keyed by commit, command and host, so a
   reviewer's identical job returns the builder's result; reviewers spend on probes for what the
   evidence does not show and may challenge any claim.
3. **Fixes are confirmed by reruns, not re-reviews.** The builder fixes; the lead reruns the
   reviewer's probe or the new test as a job (and its revert check), then accepts. A fix that adds
   a mechanism or changes an assumption gets a scoped review of the fix only.
4. **Two fix rounds, then a breaker.** A third round means the package or the design is wrong;
   the lead re-plans or asks the owner.
5. **A design is reviewed once.** Revisions that fold findings in are not re-reviewed; a disputed
   behavior is settled by an executable model or test.
6. **System review on events.** `campaign check` reports files changed on two active branches,
   two packages providing one contract, and a contract changing under an active consumer. The lead
   then asks one reviewer one question with the system lens.
7. **Yield is counted.** `campaign status --reviews` lists verdicts and blocking defects. An
   optional review (normal risk) that finds nothing material over about five packages of a kind
   is dropped for that kind; high and critical reviews are never dropped by yield.
8. **Acceptance is tied to content.** A changed path, a changed brief, or (for high and critical
   packages) a code change on `main` under the package since its review voids an acceptance until
   the lead re-accepts. The `design` lens approves the approach before the code exists, so only a
   changed brief voids it.

What must be decided when:

| Question | When |
|---|---|
| Decisions expensive to reverse (bytes on disk, wire format, public API, durability or ordering protocol) | Before substantial implementation, in the brief or a design note, reviewed once |
| What a test can show | During implementation, as checks and jobs |
| What needs the whole change in view (error paths, resource lifetimes, scope) | The package review |
| What needs several packages together | Milestone validation on `main` |

## Machine work

### Jobs

`campaign job submit --repo R --commit REF [--ref NAME=REF] --level L --minutes M -- CMD`
resolves REF to a commit at submission and returns a job id. A job records:

- identity: host, toolchain fingerprint, repository, commit, extra refs (of this or another
  repository), command, subdirectory, level, explicit environment (the dedupe key);
- scheduling: priority, `after` jobs, timeout, package;
- results: state, exit code, queued/started/ended times, log, outputs in `$JOB_OUT`.

The runner checks the commit out into a fresh detached worktree (or a reusable named slot, kept
for incremental builds and verified clean at the commit), so the builder can keep editing its own
worktree. An identical successful or pending job is returned instead of rerun unless `--force`.

### Levels

`none` (tiny single-threaded probes; no hold), `light` (builds and tests), `strict` (behavioral
timing, smoke), `exclusive` (authoritative and cache-sensitive measurements). The last three are
`quiet`'s levels, unchanged in meaning; `quiet` now kills strict and exclusive commands at their
deadline (exit 124, result invalid) and refuses strict or exclusive holds longer than 30 minutes.
Under the job runner, light commands are bounded the same way.

### Worker and runner

```
submit ──► jobs/<id>/spec.json (queued) ──► worker (singleton, exits when the queue is empty)
                                              │ claim: mkdir jobs/<id>/claim
                                              ▼
                                  runner (own session, heartbeat) ──► quiet LEVEL ... ──► cmd
                                              │ exit file, state.json, log
                                              ▼
                              reaper: stale heartbeat ⇒ kill orphans, requeue once
```

- The worker is started by `submit` and `wait` when none is alive and exits when nothing is
  queued or running, reaping every tick. There is no daemon to install. It runs at most one strict
  or exclusive job at a time and up to a quarter of the CPUs' worth of light jobs.
- Each attempt has its own runner process, the leader of a new session, writing a heartbeat every
  15 s, and its own directory (`a<N>/`: log, outputs, exit code). The command runs in its own
  process group under `quiet`; when it ends, the runner kills everything left in its session.
- Anyone reading the queue reaps: a running attempt whose heartbeat is older than two minutes has
  lost its runner; what is left of its session is killed (unless the boot id changed, when the old
  pids mean nothing), a recorded exit code is kept, and otherwise the job is requeued if its
  `retries` allow (off by default; `verify` checks retry once) or marked `interrupted`. A runner
  writes its result only if it still owns its attempt.
- Fairness lives in `quiet`, which governs every session on the machine: strict requests queue in
  arrival order ahead of new light holds, and a light request waiting ten minutes goes before new
  strict holds. A bounded hold outlives its deadline by 15 s so the next measurement starts only
  after the stopped one is gone.

### Agents never wait

A builder submits jobs, records their ids in its work file, and either continues useful work or
ends its turn. A wait of a few minutes is one blocking call (`campaign wait JOB --timeout 600`),
which costs one turn; anything longer ends the turn. The lead runs one `campaign wait` as a
background command, which costs no tokens until it exits, then resumes the builder (by message
while its context is small, else a fresh builder from the work file).

### Direct path first

Only what decides the next step blocks it. A package's checks are **gates** (`--check`) or
**deferred** (`--later`). Gates block landing: the build, the tests, and the evidence the risk
requires (TSan for concurrency, fuzzing for a decoder, the crash model for durability). Deferred
checks are the completeness work that rarely changes the decision: other platforms, soaks, full
matrices, extended fuzzing, repeated confirm runs. `land` queues them on the landed commit at
priority -1, and they gate the milestone instead of the package. A red one is an error in
`campaign check` until a fix or revert lands and `campaign verify ID --deferred` reruns them on
`main`.

Two limits keep completeness work from becoming a new queue in front of the direct path: deferred
light jobs use at most half the light slots, and a deferred strict or exclusive check lasts at
most 10 minutes (longer runs are chained). Slow CI workflows can be non-gating the same way
(`campaign config ci_later=...`). The cost is a window in which `main` carries code whose
completeness checks have not finished; the milestone gate bounds it, and code specific to a
platform still gates on that platform.

## Integration

`campaign land <id> [--upto SHA] [--push]` checks, in order, and stops at the first failure:

1. The package is active or in review; its `needs` (and, for the final landing, `after`) landed.
2. The target is on the package branch, and `main` changed nothing outside `docs/campaign/` since
   the target's merge base (otherwise: rebase, verify, retry).
3. The package's commits touch no lead-owned files (`state.json`, briefs, `PLAN.md`, reviews) and
   nothing outside its file set.
4. Every check has a successful job at the target commit.
5. For the final landing, every required lens is accepted for the current content (rule 8 above).
6. CI, when configured, is green for the target commit.

It records a landing intent in `state.json`, fast-forwards `main` (or merges, when `main` is
ahead only by campaign metadata, and verifies the merged code tree equals the tested commit's),
records the landing with its evidence, commits, and pushes only with `--push`, checking the push's
status. State commits on `main` therefore never force a rebase or a rerun.

**Increments** (`--upto`) land a prefix of the package branch before the package is accepted when
the prefix is safe alone: behavior-preserving refactors, tests, unused internal APIs, additive code
behind a disabled switch, docs. Never early: bytes on disk or the wire, public API, user-visible
behavior, or the part the package's review exists for. The final review covers the landed
increments too (`campaign status <id>` lists the ranges).

**Integration regressions:** `main` is validated by the milestone's full check as a job; a red
result names the landed packages since the last green one, and the lead reverts or fixes forward
before the next landing.

## Recovery

`campaign status` and `campaign check` reconstruct goals (PLAN), packages, dependencies,
ownership, branches, worktrees (dirty or not), the builder's last work file, jobs (queued,
running, finished, interrupted), reviews and acceptances, and what is ready or landable.

| Situation | Behavior |
|---|---|
| State says active, branch deleted | `check` error; the lead decides (drop, or restart from `main`) |
| Branch merged but not recorded | `check` warning; `campaign land <id> --record` records it |
| Lead dies between moving `main` and recording it | The intent left in `state.json` is completed (or cleared, if `main` never moved) by the next state-writing command |
| Job finished while nobody watched | Its result is on disk; `status` shows it |
| Runner or worker died, host rebooted | Stale heartbeat: orphans killed (same boot only), job requeued if its retries allow, else `interrupted` |
| Worktree has uncommitted changes | `status` shows them; the resume prompt says keep, never discard |
| Two branches change one file | `check` warning naming both packages |
| Evidence older than the head | `status` shows checks as stale; `land` refuses |
| Package superseded | `campaign set <id> status=dropped reason=...`; its branch is kept until `clean` |
| Job process alive after its coordinator exited | Jobs are meant to outlive agents; a job whose runner died is killed by the reaper |

In-process subagents die with their lead in Claude Code and Copilot CLI, so a builder cannot
survive to race its replacement; jobs, which do survive, are supervised by their runners.

## Authority

The owner decides goals and non-goals, significant scope changes, removal or narrowing of
user-visible behavior, reserved public API decisions, commitments to other teams, new external
services or repositories, anything that spends money, and irreversible operations outside the
project. The lead decides the rest. A decision request states the decision, the facts, the
options with tradeoffs, a recommendation, and what deferring costs.

## Cloud

Campaign never provisions billable infrastructure; the 1.x `lease` tool is removed from the
skill and from `PATH`. A future capability would need, enforced outside any agent: explicit owner
authorization per batch, a provider-side maximum lifetime set at launch, a spending cap with
alerts, an inventory and cleanup check that runs without the agent, an emergency stop, and safe
behavior when the orchestrator or the network dies. 1.x's `lease run` met some of these (an AWS
one-time stop at launch); the decision on whether to rebuild it belongs to the owner.

## Harnesses

The methodology, the tool and the state are harness-neutral. The harness reference maps five
operations: launch an agent of a type with a model and effort, run it in the background, resume
it by message, wait for a long command without tokens, and schedule a report. Agent types are
written once (`home/.claude/agents/{builder,reviewer}.md`) and generated for Copilot CLI and
Codex. Where a harness lacks an operation, the reference says so and gives the fallback: Copilot
agent files cannot pin effort (pass it per launch); a harness without background notification
waits with `campaign wait` in bounded calls.

## Failure modes of the new mechanisms

| Event | Effect |
|---|---|
| `campaign` crashes mid-write | Writes go to a temporary file then rename; the old state stays valid |
| Two lead sessions | State writes take a lock and require the home repository's main checkout; the second waits |
| Job submitted twice | The dedupe key returns the first |
| Runner died after the command finished, before recording | The exit code was written first; the reaper records it |
| Old runner wakes after its attempt was replaced | It no longer owns the attempt and writes nothing |
| Results arrive after the package moved | Recorded at their commit; `land` ignores them for a newer head |
| No worker can run | `campaign job submit --sync` runs the job through the same runner in the foreground and records the same result |
| Permission denied, disk full | The job fails with its log; `quiet` refuses new holds below 5 GiB free |
| Timeout | Process group TERM, then KILL after 10 s; state `timeout`; never a valid measurement |

## Tradeoffs taken

1. **A Python tool.** State transitions, dependency graphs and process supervision are safer in
   a typed standard-library program than in bash. It needs only `python3` (3.9+) and `git`.
   Every tool has that shape; only `install.sh` stays shell, as the bootstrap.
2. **Evidence identity is the code tree.** Landing requires the branch to contain `main`'s code
   and the checks to have passed at that commit. A rebase over another package's code costs a
   rerun of the fast checks; state commits never do, because `.campaign/` is outside the
   identity. A merge queue that batches landings would amortize reruns; at a few landings a day it
   is not worth its machinery.
3. **Content-based acceptance.** A review is tied to file contents, not commit ids, so rebases that
   do not touch the package's files keep it (for normal risk). High and critical packages also
   void it on upstream code changes, because a reviewer's judgment can depend on code the package
   did not change.
4. **No agent leases.** Claims and heartbeats for agents would protect against a builder outliving
   its lead, which in-process subagents cannot do. Jobs, which can, have heartbeats.
5. **Host-local job records.** Logs stay on the machine that ran them; the summary that justified
   a landing is copied into `state.json`. Losing a host loses logs, not the landing record.
6. **One repository per package.** Cross-repository work is ordered packages plus an integration
   job pinned to every repository's commit. Atomic multi-repository landing would need a
   coordinator nobody has asked for.
7. **No messages between campaigns.** Campaigns on one machine see each other's overlapping
   quests, changing contracts and `NAME:ID` dependencies in `campaign check`; across machines they
   see nothing, and the patron carries what one game master must tell another. A mailbox would
   be outboxes in each home (`.campaign/<name>/outbox/<peer>/`, written only by their campaign),
   read from peers' homes by path or fetched git URL, acknowledged in the reader's state, and
   waking `campaign wait`; the tool would send what it already detects. It is not built: the
   records hold no coordination failure between campaigns (the relays the patron made were
   between ordinary sessions, or about the skill itself), and it costs about 200 lines, a fetch
   loop and instruction words. Build it when two campaigns that share a repository or a contract
   run at once, or when the patron relays between game masters more than occasionally.
