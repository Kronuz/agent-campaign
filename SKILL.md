---
name: campaign
description: Run a large engineering project with agents as a campaign. The lead (the main session) splits the owner's goals into quests, each one independently verifiable outcome; contributor agents complete them on their own branches and worktrees; a fresh reviewer examines each quest as its risk requires; and the `campaign` tool keeps the state, runs builds, tests and benchmarks as jobs on exact commits under `quiet`, and lands only what was verified. Use when starting or resuming a multi-part project (an engine, a library family, a migration, a rewrite), when work should run in parallel worktrees, when a quest needs review or is ready to land, or when the owner says "start the campaign", "resume the campaign", "status", "review it" or "land it".
---

# Campaign

The **lead** (the main session) turns the **owner**'s goals into **quests**. A **contributor**
agent completes each quest on its own branch; machine work goes to a **job queue** that runs it at
exact commits while no agent waits. A **reviewer** agent examines a quest from a fresh context
when its risk calls for it. **`campaign land`** puts a quest on `main` only when its checks passed
at that exact code, each criterion has the evidence it names, and each required review was
accepted for the code it saw. Everything that matters is in the repository, so any agent, the lead
included, can be replaced.

```
goals ─► PLAN ─► quests ─► campaign start ─► contributor ─► jobs (quiet) ─► evidence
                                                  │                              │
                                                  └─► reviewer (by risk) ─► accept ─► campaign land
```

## Invariants

1. **Evidence, not narrative.** A check counts only when a job ran it on the code being landed; a
   journal line or a reviewer's say-so is never a green test.
2. **One writer per file set.** `campaign start` refuses overlapping file sets; `land` refuses
   changes outside the quest's set and edits to the lead's files.
3. **Criteria before code.** The lead writes each quest's acceptance criteria and the evidence each
   needs; contributors never edit them, and a changed quest file voids earlier acceptances.
4. **Review follows risk.** One fresh reviewer per risk that needs one; an acceptance covers the
   code its review saw and the review's exact content.
5. **Nothing weakens silently.** Lowering a risk, removing a lens or a check, waiving a review,
   landing without CI or recording a landing done by hand needs a reason, which the tool records.
6. **State lives in files.** `.campaign/<campaign>/` and git hold how the campaign runs; what the
   product is goes to `docs/` of the repository it describes.
7. **Agents never wait on machines, and measurements never run without isolation.**
8. **The owner decides goals, scope, and any removal or narrowing of user-visible behavior.** The
   lead decides the rest and records it in `PLAN.md`. **Campaign never rents machines** or creates
   any other billable resource.

## Roles

| Role | Does | Writes |
|---|---|---|
| Owner (a person) | Goals, non-goals, reserved decisions | `PLAN.md` goals |
| Lead (the main session) | Plans quests, launches agents, accepts reviews, lands, reports to the owner | `state.json` (only through `campaign`), quest files, `PLAN.md`, `main` |
| Contributor (`campaign-contributor` agent) | One quest or task, by its assignment | its branch and journal |
| Reviewer (`campaign-reviewer` agent) | One quest through one lens, from a fresh context | one review file |

A contributor's **assignment** names the kind of work; each but implementation has a method in
`references/assignments/<name>.md`. The lead names the assignment, the model tier and the effort
at every launch, choosing by the work, never by habit:

| Assignment | For | Tier, effort |
|---|---|---|
| implementation | features and fixes with concrete criteria | economical, medium for low and normal risk; strong, high for high and critical, or after an economical attempt stalls |
| design | quest lists for consequential plans; formats, protocols, durability, concurrency | strong, high; max for a design about to freeze |
| research | rarely: the mechanism itself is the limit | strong, max |
| performance | profiling and optimization | strong, high |
| exploration | one target of an exploration batch, or a quick question | economical, medium |
| documentation | docs, guides, READMEs | economical, medium |
| maintenance | one mechanical change across repositories | economical, medium |

Reviewers use the strong tier at high effort (max for critical quests); for critical quests and
the plan challenge, a reviewer from another model family when the harness offers one. Each
harness's models for the tiers are in `references/harness.md`. Change a default on outcomes
(`campaign usage`, criteria met, rework), not on price alone. The lead does small edits itself
(a five-line fix, a trivial conflict): a launch costs more.

## Start a campaign

1. In the home repository's main checkout: `campaign init --name NAME [--ci github]`. The owner
   writes Goals, Non-goals, Constraints and Reserved decisions in `.campaign/<campaign>/PLAN.md`.
2. Split the goals into quests: `campaign new ID --title T --risk R --files GLOBS [--needs IDS]
   [--after IDS] [--provides C] [--consumes C] [--milestone M] --check NAME[:LEVEL[:MIN]]=CMD`.
   Fill in each quest file (`.campaign/<campaign>/quests/ID.md`): outcome, criteria A1... each with
   the evidence it needs (Evidence, below), contracts, decisions, what may land early.
3. When the architecture is consequential, one reviewer (lens `design`) challenges the plan and the
   quest list before anything is built. This is the one review worth waiting for.

## Quests

- **Size:** the smallest change that delivers a complete, meaningful behavior a reviewer can hold
  in mind, usually a few hundred to ~1,500 lines. Split bigger ones; fold a change not worth its
  own landing into the quest it serves.
- **`needs`** must land before a quest starts; **`after`** must land before it lands. A quest may
  start against an `after` dependency only because that dependency's quest file fixes the contract.
- **Contracts** (`--provides`, `--consumes`) name APIs, formats and protocols, so `campaign check`
  sees two quests touching one.
- **Tasks:** parallel writers inside one quest (`campaign start ID --task T --files GLOBS`),
  merged into the quest branch by the lead. The quest is still reviewed once.
- **Increments:** `campaign land ID --upto COMMIT --reason R` lands a prefix the quest file's "May
  land early" allows (behavior-preserving refactors, tests, unused internal APIs, code behind a
  disabled switch). Never early: bytes on disk or the wire, public API, user-visible behavior, or
  the part the review exists for.
- **Cross-repository work** is several quests (`--repo PATH`) ordered by `after`, with an
  integration job that pins every repository's commit (`--ref NAME=PATH@REF`). Landing is ordered,
  never atomic: each intermediate state must work.
- **Messages:** an agent with a finding for a quest it is not working on, in any campaign on the
  machine, sends `campaign message NAME:ID "text" --from WHO`. The quest cannot land until one of
  its journals answers it by its id (`msg-N`) or the lead closes it (`campaign message ID --close
  msg-N --reason R`); `NAME:` writes to that campaign's lead and never blocks a landing. Details,
  and other machines: `references/host.md`, Messages.
- **Scope changes:** a new ask goes into the quest file first (a criterion, or its own quest if it
  can land alone), then to the contributor; superseded work is `campaign set ID status=dropped
  reason=...`.

## Direct path first

Only what decides the next step blocks it. A quest's **gate checks** (`--check`: the build, the
tests, and the evidence its risk requires) block its landing. Its **deferred checks** (`--later`:
other platforms, long soaks, full matrices, extended fuzzing) are queued at low priority on the
landed code and must be green before the milestone closes. A red one is an error in `campaign
check`: fix forward or revert, then `campaign verify ID --deferred` reruns them on `main`. The
evidence a high or critical risk needs (TSan for concurrency, fuzzing for a decoder, the crash
model for durability) is always a gate. Slow CI workflows can be made non-gating the same way
(`campaign config ci_later=NAME,...`).

## Run a quest

1. `campaign start ID` creates `quest/<campaign>/ID` in its own worktree with a journal. Launch a
   contributor **in the background**, so it can be resumed by message, with the "Contributor"
   prompt (`references/prompts.md`); run quests in parallel only when they are ready, disjoint and
   not waiting on the same machine.
2. The contributor works in its worktree, keeps its journal current, and produces evidence as jobs:
   `campaign job submit --quest ID --level L --minutes M -- CMD` and `campaign verify ID`. When
   only jobs remain, it records their ids and ends its turn.
3. The lead waits with one background `campaign wait --commits` (no tokens until a job ends, a
   branch moves or a message arrives), then resumes the contributor by message while its context
   is small (about 150k tokens). A larger context, or a harness that cannot resume it, gets a fresh
   contributor with the "Resume" prompt, which rebuilds from the journal and job records.
4. When the journal says "ready for review", the lead sets `status=review` and launches the
   reviewers the risk requires; each writes `.campaign/<campaign>/reviews/ID-LENS.md`. The lead
   relays blocking findings to the contributor by id.
5. `campaign accept ID LENS` once a review approves, or after CHANGES with the confirming jobs
   (Risk and review); it commits the review file with the acceptance. Then `campaign land ID`, which
   also removes the quest's worktrees, merged branches and build slot, and names what it kept.

## Exploration batches

For a list worth hunting (a fresh benchmark's losing rows, a profile's hotspots, thinly proven
code, a backlog of small bugs), send several economical contributors at once, one target each,
targets from a fresh measurement on current `main`: `references/assignments/exploration.md`. Size
the batch by its targets and by what the lead can review and land, never by idle quota.

## Risk and review

| Risk (first match wins) | Before building | Before final landing |
|---|---|---|
| **critical**: irreversible (freezing a format or protocol for release, migrating users' data, removing user-visible behavior) | owner approval; a `design` review | the high-risk review, plus acceptance evidence the reviewer ran itself |
| **high**: persistent formats, wire protocols, durability, concurrency, security surfaces, shared public APIs | a `design` review when a decision is expensive to reverse | one review by the lens of the dominant risk (`durability`, `security` or `correctness`) |
| **normal**: ordinary features and fixes | — | one `correctness` review, or `--waive REASON` for a small change with strong evidence |
| **low**: docs, tests, tooling, mechanical edits, behavior-preserving refactors | — | checks green; the lead reads the diff |

- **One reviewer by default,** carrying every risk question the quest raises (the prompt names
  three to five). A second only for a question that needs separate expertise or independence.
- **A review is bound to what it saw.** It names the commit it reviewed (`Commit:`), a verdict, and
  a findings table with an ID, Blocking (yes or no) and Kind (defect, gap, nit) per row. An APPROVE
  covers that commit's code; commits that touch only `.campaign/` keep it. Editing the review after
  acceptance voids the acceptance.
- **Fixes are confirmed by jobs, not re-reviews.** For each blocking finding, rerun its probe or
  the new test as a job at the fixed commit, then `campaign accept ID LENS --confirm C1=JOB
  --confirm C2=JOB`. A job confirms only if it passed at the accepted code in the quest's
  repository; a blocking defect needs changed code; the tool records whether the same probe failed
  at the reviewed commit. A fix that changes files the review never saw goes back to the reviewer
  (the review names the fixed commit and approves), unless `--fix-scope REASON` says why it adds no
  mechanism (a regression test for a finding).
- **Two fix rounds, then stop.** A third means the quest or design is wrong: re-plan or ask the
  owner. Non-blocking findings become a follow-up quest or are dropped with a reason.
- **A design is reviewed once.** Settle disputed behavior with an executable model or test, not
  another text round.
- **What a quest must pass changes only on the record:** `campaign set ID ... --weaken REASON`
  lowers its risk, removes a lens, or removes, changes or defers a check; the change is kept in its
  history, shown by `campaign status ID`, and voids its acceptances. The review a risk needs (a code
  review above low, a design review for critical) cannot be removed, and a waiver is refused once a
  review exists.
- **System review on events, not on a calendar:** when `campaign check` reports two quests
  changing one file or one contract, ask one reviewer (lens `system`) that specific question.
- **Yield:** `campaign status --reviews` counts verdicts and blocking defects. An optional review
  (normal risk) that finds nothing material over about five quests of a kind is dropped for that
  kind; high and critical reviews are never dropped by yield.

## Evidence

Each criterion in a quest file names the evidence it needs; the journal's Evidence table gives one
row per criterion, and landing checks each row at the code being landed:

| Criterion | Its row must name |
|---|---|
| `- A1 ...` (untagged) | a job that passed at that code (it, or the same job rerun) |
| `- A2 [artifact] ...` | a `path` committed in the quest's repository |
| `- A3 [decision] ...` | an id in `PLAN.md`'s Decisions table |
| `- A4 [manual] ...` | who checked what, by hand; shown as manual, never as automated proof |

Proof is proportional to the claim (`references/review.md`): tests check values, not counts; a fix
has a test that fails without it; a claim has a mutant a test kills; sanitizers in separate builds.
Performance claims follow `references/measurement.md`: before and after at named commits,
`exclusive` for numbers that decide anything, honest magnitude.

## Machine work

| Level | For | Isolation (`quiet`) |
|---|---|---|
| `none` | tiny single-threaded probes | no hold |
| `light` | builds, test suites, fuzzing | a few at once, nice 19 |
| `strict` | smoke timings, behavioral runs | own cores on Linux; alone on macOS |
| `exclusive` | numbers that decide or get published, cache-sensitive A/Bs | the machine to itself |

Jobs run at the commit they were submitted for, in their own checkout, with the submitter's PATH.
A result is reused for identical code (without `.campaign/`), command, directory, level, `--env`,
host and toolchain: name a mutable input (a dataset version) with `--env`, and rerun with
`--force` what depends on more. A strict or exclusive job lasts at most 30 minutes and is stopped at
its deadline. Every process a job starts dies with it. A worker starts by itself and exits when
the queue is empty, pruning once a day: jobs nothing names go after 14 days, and jobs evidence
names keep their record and lose only bulky outputs (`campaign job prune`). Mechanics:
`references/host.md`.

## Integration

`campaign land` refuses with each gap `campaign brief` lists: dependencies, a rebase when `main`
changed code, the file set, tasks, criteria, messages, gate checks at the exact code, reviews, CI.
It fast-forwards `main`, or merges when only campaign files differ, verifies that the landed code
equals the tested code, records the landing, and pushes only with `--push`. Exceptions are explicit
and recorded with the landing: `--upto COMMIT --reason R` (an increment), `--no-ci REASON`, and
`--record COMMIT --reason R` (a landing done by hand, with the gates it never checked). Each
milestone in `PLAN.md` runs its full validation as a job on `main`; a red result is fixed or
reverted before the next landing.

Other campaigns on the machine share the job queue, `quiet` and the quota. `campaign check` reports
their quests that overlap yours or change a contract you consume; order them with `--after
NAME:ID`, which landing enforces, and tell their lead (`campaign message NAME: "..."`).

## Recovery

Every session, lead or contributor, starts with `campaign brief` (in a worktree, its quest) and
`campaign check`, then reads the journal and `git log`. Observable state wins: git, worktrees and
job records over any document. A dirty worktree is kept, never discarded. An interrupted landing is
completed or cleared by the next state-writing command. A job whose runner died is requeued when it
allows retries, else marked `interrupted`. Submitting a job again never duplicates one: an
identical job that is queued, running or done is returned instead. An interrupted job may have run
partway: resubmit it when its command is safe to repeat (builds, tests and measurements are);
otherwise check what it changed first. Never invent a missing decision: write it under "Blocked
on" and ask.

## Cost

- No minimum number of agents, no utilization target. `pace` reports quota: check it before
  launching agents in parallel, which multiplies the burn.
- The lead coordinates; it does not re-derive what the state holds. It reads this file and
  `references/prompts.md`, and other references only when the table below says; it orients with
  `campaign brief` and learns what changed from `campaign wait --commits`, not by rereading
  journals, diffs or code.
- Prompts point at files and commits; final reports stay under ~250 words, detail in the journal
  or review file. `campaign usage` shows tokens, cost and time per quest, role and assignment.
- Report to the owner on events (a landing, a blocker, a decision); any periodic report is built
  from `campaign status` and `campaign map`, never by waking agents.

## Writing

Operational records (journals, reviews, decision rows, messages) are terse and factual: the state,
the next step, the evidence, the decision and why. Engineering documents (designs, results,
runbooks) are plain technical prose that points at code and job ids. A personal writing-style skill
is for public-facing writing the owner asks to have in that voice, never for records. Results and
how-it-works notes (`templates/mechanism.md`, when a mechanism is not obvious from its code) go in
the docs of the repository the quest changed, a library's own included; the journal's Result names
them, and `campaign map` shows it.

## The owner's authority

Beyond goals and scope, the owner decides reserved public APIs and formats, commitments to other
teams, new external services or repositories, and anything that spends money. The owner decides on
evidence: an ask that contradicts a measurement or a review finding goes back with that evidence,
the options and a recommendation. A decision request gives the decision, the facts, the options
with tradeoffs, a recommendation, and what deferring costs, in plain words. An action the
permission system denied an agent stays undone until the owner authorizes it.

## References

| File | Read it |
|---|---|
| `references/prompts.md` | before launching a contributor or reviewer |
| `references/assignments/<name>.md` | the method of a contributor's assignment |
| `references/review.md` | before writing tests for a claim, reviewing, or judging proof |
| `references/measurement.md` | before planning or judging any benchmark |
| `references/host.md` | for jobs, `quiet`, build slots, remote hosts and failure behavior |
| `references/harness.md` | for launching, resuming and model tiers in Claude Code, Copilot CLI and Codex |
| `references/lessons.md` | before changing a rule: the incidents behind them |
