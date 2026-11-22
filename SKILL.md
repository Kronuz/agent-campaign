---
name: campaign
description: Run a large engineering project with agents as a campaign. The lead (the main session) splits the owner's goals into quests, each one independently verifiable outcome; contributor agents complete them on their own branches and worktrees; a fresh reviewer examines the high-risk ones; and the `campaign` tool keeps the state, runs builds, tests and benchmarks as jobs on exact commits under `quiet`, and lands only what was verified. Use when starting or resuming a multi-part project (an engine, a library family, a migration, a rewrite), when work should run in parallel worktrees, when a quest needs review or is ready to land, or when the owner says "start the campaign", "resume the campaign", "status", "review it" or "land it".
---

# Campaign

The **lead** (the main session) turns the **owner**'s goals into **quests**. A **contributor**
agent completes each quest on its own branch; machine work goes to a **job queue** that runs it at
exact commits while no agent waits. A **reviewer** agent examines a high-risk quest from a fresh
context. **`campaign land`** puts a quest on `main` only when its suite passed at its code and CI
at the exact commit, each criterion has a job tagged for it (or the evidence it names), and each
required review was accepted for the code it saw. Everything that matters is in the repository, so
any agent, the lead included, can be replaced.

```
goals ─► PLAN ─► quests ─► campaign start ─► contributor ─► jobs (quiet) ─► evidence
                                                  │                              │
                                                  └─► reviewer (high risk) ─► accept ─► campaign land
```

## Invariants

They apply the Zen in the global AGENTS.md ("The Zen German keeps").

1. **Evidence, not narrative.** Only a job run on the code being landed counts.
2. **One writer per file set.** `campaign start` refuses overlapping file sets; `land` refuses
   changes outside the quest's set and edits to the lead's files.
3. **Criteria before code.** The lead writes each quest's acceptance criteria and the evidence each
   needs; contributors never edit them, and a changed criterion voids earlier acceptances.
4. **Tasks build, the quest is checked.** Tasks merge into their quest's branch on their own
   tests; reviews and measurements run once, on the finished feature, before it lands.
5. **Nothing weakens silently.** Lowering a risk, removing a lens or a check, landing without CI or
   recording a landing done by hand needs a reason, which the tool records.
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

**Session names.** Every session's title leads with its campaign and role, so messages and the
owner's session list name the campaign: start the lead with `claude -n '<campaign> · lead'` (in
cmux: `cmux new-workspace --name '<campaign> · lead' --cwd HOME --command "claude -n '<campaign> ·
lead'"`; other harnesses: `references/harness.md`); a lead's `campaign brief`, `status` and `wait`
name its cmux workspace and print the `/rename` to type when the session's title is not that.

A contributor's **assignment** names the kind of work; each but implementation has a method in
`references/assignments/<name>.md`. The lead names the assignment, the model tier and the effort
at every launch, choosing by the work, never by habit:

| Assignment | For | Tier, effort |
|---|---|---|
| implementation | features and fixes with concrete criteria | economical, medium for low and normal risk; strong, high for high and critical, or after an economical attempt stalls |
| design | quest lists for consequential plans; formats, protocols, durability, concurrency | strong, high; max for a design about to freeze |
| research | rarely: the mechanism itself is the limit | frontier, max (strong where the harness has none) |
| performance | profiling and optimization | strong, high |
| exploration | one target of an exploration batch, or a quick question | economical, medium |
| documentation | docs, guides, READMEs | economical, medium |
| maintenance | one mechanical change across repositories | economical, medium |

Reviewers use the strong tier at high effort (max for critical quests); for critical quests and
the plan challenge, a reviewer from another model family when the harness offers one.

The **frontier** tier, the most capable model a harness offers above strong, is for targeted hard
or sensitive work only: research, a critical quest's review, and the design or review of a format,
wire protocol, durability or concurrency mechanism about to freeze. Name its task at each launch,
run one at a time, never make it a role's default, and measure whether it beats strong: both on
one commit from fresh contexts, compared by the material findings each alone made. Each
harness's models for the tiers are in `references/harness.md`. Change a default on outcomes
(`campaign usage`, criteria met, rework), not on price alone.

**Quota never decides quality.** The tier, effort, reviews, lenses and checks follow from the work
and its risk alone, never from quota left, burn rate or price: never downgrade a model, skip or
shrink a review or check, or trim a criterion to save quota.

## Start a campaign

1. In the home repository's main checkout: `campaign init --name NAME [--ci github]`. The owner
   writes Goals, Non-goals, Constraints and Reserved decisions in `.campaign/<campaign>/PLAN.md`.
2. Split the goals into quests, each ID naming the feature it builds (`group-heal`, never `p3-step2`
   or `t7`): `campaign new ID --title T --risk R --files GLOBS [--needs IDS]
   [--after IDS] [--provides C] [--consumes C] [--milestone M]`; its gate is its repository's suite
   (`campaign config suite=CMD` for the home, `suite.REPO=CMD` for another), unless `--check
   NAME[:LEVEL[:MIN]]=CMD` names others. Fill in each quest file
   (`.campaign/<campaign>/quests/ID.md`): outcome, three to five criteria at outcome level, each
   naming its test (Evidence, below), contracts, decisions, tasks.
3. When the architecture is consequential, one reviewer (lens `design`) challenges the plan and the
   quest list before anything is built. This is the one review worth waiting for.

## Quests

- **Size:** a whole feature, built on its quest branch. Split quests only into features that ship
  alone; split the work inside one into tasks.
- **Quick fixes** skip the quest: an obvious cause, tens of lines outside the high and critical
  areas, and a test failing without it. The lead or one economical agent commits fix and test to
  `main`, pushes, and fixes forward on red CI; the commit message is the record. A design choice
  or growth makes it a quest. A pin bump is the lead's: `campaign pin --push` checks that only
  version strings changed, runs `config pin_check` and records it; quests merge `main`, never
  claiming the build file.
- **`needs`** must land before a quest starts; **`after`** before it lands, which a quest may
  start against only because that quest file fixes the contract. Any other wait is a park (`set ID
  status=blocked until=IDS|DECISION|owner reason=R`); `land` prints and mails what it unlocks.
- **Contracts** (`--provides`, `--consumes`) name APIs, formats and protocols, so `campaign check`
  sees two quests touching one. A quest in another repository changes that repository's contracts:
  a quest building on a library names it (`--consumes http`, or `http:client` for one part), and its
  lead hears of every quest any campaign starts there.
- **Tasks** are the steps of a quest (`campaign start ID --task T --files GLOBS`): one agent each,
  its assignment, tier and effort chosen for that step, on a branch off the quest branch. `campaign
  task merge ID T` merges it once its own tests (its `none` and `light` jobs, `--task T`) pass at
  its head; its `strict` and `exclusive` jobs are measurements the quest's landing checks, so they
  never hold a merge. The quest branch takes `main` daily. Disjoint tasks run at once.
- **Increments:** `campaign land ID --upto COMMIT --reason R` lands a prefix the quest file's "May
  land early" allows (behavior-preserving refactors, tests, unused internal APIs, code behind a
  disabled switch). Never early: bytes on disk or the wire, public API, user-visible behavior, or
  the part the review exists for.
- **Cross-repository work** is several quests (`--repo PATH`) ordered by `after`, with an
  integration job that pins every repository's commit (`--ref NAME=PATH@REF`). Landing is ordered,
  never atomic: each intermediate state must work. Its journal stays in the home
  (`campaign get ID journal`), so the other repository holds only code and docs.
- **Messages:** an agent with a finding for a quest it is not working on, in any campaign on the
  machine, sends `campaign message NAME:ID "text" --from WHO`. The quest cannot land until one of
  its journals answers it by its id (`msg-N`) or the lead closes it (`campaign message ID --close
  msg-N --reason R`); `NAME:` writes to that campaign's lead and never blocks a landing (more in
  `references/host.md`). The lead never hand-lists receivers: a change other campaigns must know (a
  rule, a reserved decision, a format) goes to every other lead with `campaign message --all
  "TEXT"`, and the tool itself tells them, once each, of every skill commit landed and installed.
- **Docs areas** under `tested_docs` are separate quests from the start, chained with `--after`;
  docs of an in-flight format change stay in its quest.
- **Scope changes:** a new ask goes into the quest file first, then to the contributor; superseded
  work is `campaign set ID status=dropped reason=R`. When a quest in any campaign on the machine
  waits on it, the drop must also say where that wait goes, or it is refused: `into=SUCCESSOR` (the
  quest now doing the work; the wait moves to it) or `into=none` (nobody lands that work; the wait
  ends, and the waiting campaign's `check` warns until its lead confirms nothing needed it). Each
  campaign's tool reads the others' state, so no lead has to pass this on; their leads are told
  anyway. A dependency on a quest that exists in no campaign is refused, and an error in both.

## Direct path first

A **task**'s gate is its own jobs, never a review. A **quest**'s gate, once, when the feature is done: the suite
and CI, the review its risk needs, and the measurements its criteria name. Findings and slow rows
become tasks on the quest branch; nothing reruns but what they change. Sanitizers and the platform
matrix are CI's. Gate checks queued by `verify` run first, at priority 2, ahead of the quest's other light evidence (1).

Crash matrices, fuzzing, soaks, mutant batches and full sanitizer sweeps are **milestone checks**
(`--later`): queued at low priority on the landed code, run once per milestone on `main`, green
before it closes. A red one is an error in `campaign check`: fix forward or revert, then `campaign
verify ID --deferred` reruns them. The exception is the crash model of a quest that writes a new
on-disk or wire format or changes a durability mechanism, which gates it. Slow CI workflows are made non-gating the same way
(`campaign config ci_later=NAME,...`).

With `ci=github`, what CI runs is a `ci:` check (`--check tsan=ci:ThreadSanitizer`), met by that CI
job passing at the exact commit; it queues no local job.

## Run a quest

1. `campaign start ID` creates `quest/<campaign>/ID` with a journal. The lead splits the feature
   into tasks in the quest file and starts each launchable one (`--task T`), launching a
   contributor **in the background** per task with the "Contributor" prompt
   (`references/prompts.md`), critical path first, capped by what it can merge and the host's
   light slots. A quest is launchable when it is planned, its `needs` are met and its file set is
   free in its repository; `campaign status` lists them, critical path first.
2. A contributor builds its task, runs its tests as jobs (`--quest ID --task T`), commits and ends
   its turn; the lead merges it and starts what it unblocked.
3. The lead waits with one background `campaign wait --commits`, never with a loop that retries
   `land`, which hides every refusal. It returns when a job ends, a branch moves, a message arrives
   or the lead's **to-do list** gains an item, at once when the wait starts with one, and again for
   an item left undone 30 minutes (`CAMPAIGN_TODO_REPEAT`). The list ends every wait, `status` and
   `brief`, critical path first, each item with its command: quests to start, tasks to merge,
   quests to land or review, quests nothing moves without (a journal waiting on the lead or on jobs
   that ended, or untouched for an hour with no job queued or running and no CI running), quests
   to park (a journal waiting on the owner) and idle light slots beside open hunt rows; a wait and
   `brief` show ten items and count the rest, `status` all. Every process on the host shares
   CI answers: a commit's runs for 2 minutes while live (`CAMPAIGN_CI_SECONDS`), 10 once
   completed, and a run's jobs for good once green. Waiting on a job, CI or a drain is never a reason for the
   list to be empty. The lead acts on every item at every wake: it resumes a contributor by message
   while its context is small (about 150k tokens), else starts a fresh one with the "Resume"
   prompt; "wait again" is printed only when the list is empty.
4. When every task is in, the quest is the feature: the lead sets `status=review`, runs the suite,
   launches the review its risk needs and the measurements its criteria name, all at once. Their
   findings become tasks; then `campaign accept ID LENS` and `campaign land ID` on a green suite
   and CI.

## Exploration batches

For a list worth hunting (a benchmark's losing rows, a profile's hotspots, thinly proven code,
small bugs), send several economical contributors at once, one target each, confirmed on `main`
first: `references/assignments/exploration.md`. Size the batch by its targets and by what the lead
can review and land.
A quest made from a hunt row names it (`campaign new ... --hunt WORDS`, or `campaign set ID
hunt=WORDS`, the first words of exactly one open row), and its landing marks that row done with the
landed commit, so a fixed target never goes out to another explorer.

The targets live in the **hunt list**, `.campaign/<campaign>/hunt.md` (the lead's, committed;
`campaign init` writes it): one row per target with its evidence and the files to start in;
`campaign status` counts the open rows.

- When fewer quests are launchable than the lead can review, first act on what most quests wait
  on (`campaign status` names it under "most waited on": finish, review or land that quest, or ask
  its campaign's lead whether the order is needed), then launch a batch from the hunt list.
- After a milestone closes or performance-relevant code lands, run a short benchmark on `main`
  and add its losing rows.
- Non-blocking findings, follow-ups and small bugs go to the hunt list.

## Risk and review

| Risk (first match wins) | Before building | Before final landing |
|---|---|---|
| **critical**: irreversible (freezing a format or protocol for release, migrating users' data, removing user-visible behavior) | owner approval; a `design` review | the high-risk review, plus acceptance evidence the reviewer ran itself |
| **high**: persistent formats, wire protocols, durability, concurrency, security surfaces, shared public APIs | — | one review of the finished feature by the dominant risk's lens |
| **normal**: ordinary features and fixes; **low**: docs, tests, tooling, mechanical edits, behavior-preserving refactors | — | suite and CI green; the lead reads the diff |

- **One reviewer,** carrying every risk question the quest raises (the prompt names three to five);
  a second only for a question that needs separate expertise or independence.
- **A review is bound to what it saw.** It names the commit it reviewed (`Commit:`), a verdict, and
  a findings table with an ID, Blocking (yes or no) and Kind (defect, gap, nit) per row. An APPROVE
  covers the quest's changed lines; main's merges and untested docs keep it. A CHANGES whose findings
  are all nits is an APPROVE. Editing the review after acceptance voids it.
- **Fixes are confirmed by tests, not re-reviews.** Each blocking finding's fix adds a test; the
  suite job that passes it at the fixed commit confirms it: `campaign accept ID LENS --confirm
  C1=JOB --confirm C2=JOB`. A job confirms only if it passed at the accepted code in the quest's
  repository, and a blocking defect needs changed code. A fix that changes files the review never
  saw goes back to the reviewer, unless `--fix-scope REASON` says why it adds no mechanism.
- **Two fix rounds, then stop.** A third means the quest or design is wrong: re-plan or ask the
  owner.
- **A design is reviewed once,** with one text fix round; the reviewer's probes, run as jobs, settle
  what is still disputed, never another text round.
- **What a quest must pass changes only on the record:** `campaign set ID ... --weaken REASON`
  lowers its risk, removes a lens, or removes, changes, defers or scopes a check; it is kept in its
  history, shown by `campaign status ID`, and voids its acceptances. The review a risk needs (a code
  review for high and critical, a design review for critical) cannot be removed.
- **System review on events, not on a calendar:** when `campaign check` reports two quests
  changing one file or one contract, ask one reviewer (lens `system`) that specific question.

## Evidence

Each criterion names its test or other evidence, and landing reads it from the records:

| Criterion | Its evidence |
|---|---|
| `- A1 ...` (untagged) | a job tagged `--for A1` (or a later run of it), or the one gate check its text names (`` the `unit` check ``), passed at the quest's code; with `--fails-without` tags, one of those's test failed (exited nonzero) too |
| `- A2 [artifact] ...` | a journal row naming a path committed in the quest's repository; with a quote (`` `path`: "text" ``), the text it holds |
| `- A3 [decision] ...` | a journal row naming an id in `PLAN.md`'s Decisions table |
| `- A4 [manual] ...` | a journal row saying who checked what; shown as manual, never as automated proof |
| `- A5 [ci] ...` | the land gate's CI, green at the commit |

`campaign brief` generates the journal's Evidence table. The quality bar (`references/review.md`): tests check values; a
fix's test is shown failing without it once; a performance
claim has one short before/after run (`references/measurement.md`).

## Machine work

| Level | For | Isolation (`quiet`) |
|---|---|---|
| `none` | tiny single-threaded probes | no hold |
| `light` | builds, suites, fuzzing, at most 4 processes each | half the CPUs, nice 19 |
| `strict` | timings, including a decision's interleaved A/B with its A/A; load and race repros | own cores on Linux; beside light work on macOS |
| `exclusive` | a number that gets published, or a decision whose strict interval straddles its threshold: one short job each | the machine to itself |

Peak memory, disk, bytes written and operation counts do not move with load while memory is free:
measure them at `light`. Isolation buys timing precision only; a strict A/B whose difference
clears its A/A band decides without an exclusive run.

Jobs run at their submitted commit, a quest's in its build slot, with the submitter's PATH;
`--build CMD` builds there under light first. A result is reused for identical code
(without `.campaign/`), command, directory, level, `--env`, host and toolchain, by read sets, and
across docs-only commits and merges from `main` touching neither the quest's files nor the job's
`--inputs GLOBS` (a measurement without them: only docs). Name a mutable input with
`--env`; rerun with `--force` what depends on more. A strict or exclusive job lasts at most 10
minutes, a light fuzz or soak 30, each stopped at its deadline (`--long REASON` lifts it, deferred).
Declare the `--minutes` a job needs: drains and the shortest-first order plan by it until the job
has run once. `campaign job rerun J... [--commit REF] [--minutes N]` resubmits jobs at the quest's
head, keeping their command, level, directory, `--env`, tags and other repositories; `--minutes`
gives a timed-out job the time it needs. Every process a job starts dies with it; long local work is a job, never a
loose background process, stopped with `campaign job cancel ID`. The worker starts by itself and prunes
daily (`campaign job prune`); `campaign sweep` removes merged worktrees. Mechanics: `references/host.md`.

## Integration

`campaign land` refuses with each gap `campaign brief` lists: dependencies, a merge of `main` when it
changed code (`--merge-main` merges, pushes and lands when evidence carries), the file set, tasks,
criteria, messages, the suite at its code, reviews, CI.
It fast-forwards `main` (or merges when only campaign files differ), checks the landed code is the
tested code, records the landing, and pushes only with `--push`. Recorded exceptions: `--no-ci
REASON`, and `--record COMMIT --reason R` (a landing done by hand, with the gates it never checked). A red
milestone check is fixed or reverted before the next landing.

Other campaigns on the machine share the job queue, `quiet` and the quota. `campaign check` reports
their quests that overlap yours or change a contract you consume; order them with `--after
NAME:ID`, which landing enforces, and tell their lead (`campaign message NAME: "..."`).

## Recovery

Every session, lead or contributor, starts with `campaign brief` (in a worktree, its quest) and
`campaign check`, then reads the journal and `git log`. Observable state wins: git, worktrees and
job records over any document. A dirty worktree is kept, never discarded. An interrupted landing is
completed or cleared by the next state-writing command. A job whose runner died is requeued when
that is safe, else marked `interrupted`: resubmit it when its command is safe to repeat (builds,
tests and measurements are), else check what it changed first. Never invent a missing decision:
write it under "Blocked on" and ask.

## Cost

- Quota is a hint, never a constraint: launch all ready work by impact. Idle light slots take
  token-free `--priority -1` work: fuzz soaks, flake hunts, `main` benchmarks for hunt targets.
- The lead coordinates, never re-deriving the state: it orients with `campaign brief` and learns
  of changes from `campaign wait --commits`, not journals, diffs or code.
- Prompts point at files and commits; final reports stay under ~250 words, detail in the journal
  or review file. `campaign usage` shows tokens, cost and time per quest, role and assignment.
- Report to the owner on events (a landing, a blocker, a decision) and in the periodic report
  (`references/harness.md`), from `campaign status` and `campaign map`, never by waking agents;
  the periodic report quotes `status`'s host and waste lines. The lead owns the queue: it
  cancels, re-levels and reruns jobs, and no contributor waits on one.

## Writing

Operational records (journals, reviews, decision rows, messages) are terse and factual: the state,
the next step, the evidence, the decision and why. Engineering documents (designs, results,
runbooks) are plain technical prose pointing at code and job ids; a personal writing-style skill
is only for public writing the owner asks for in that voice. Results and
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
| `references/harness.md` | for launching, resuming and model tiers in Claude Code, Copilot CLI and Codex, and every harness-specific mechanism the campaign relies on (hooks, status line, scheduler, session names), listed so another harness can build it |
| `references/lessons.md` | before changing a rule: the incidents behind them |
