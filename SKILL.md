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

1. **Evidence, not narrative.** A check counts only when a job ran it on the quest's code being
   landed; a journal line or a reviewer's say-so is never a green test.
2. **One writer per file set.** `campaign start` refuses overlapping file sets; `land` refuses
   changes outside the quest's set and edits to the lead's files.
3. **Criteria before code.** The lead writes each quest's acceptance criteria and the evidence each
   needs; contributors never edit them, and a changed criterion voids earlier acceptances.
4. **A gate blocks a landing only when it catches something nothing later would.** CI and
   milestone checks run the rest.
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
run one at a time, never make it a role's default, and measure whether it beats strong. Each
harness's models for the tiers are in `references/harness.md`. Change a default on outcomes
(`campaign usage`, criteria met, rework), not on price alone. The lead does small edits itself (a
five-line fix, a trivial conflict): a launch costs more.

## Start a campaign

1. In the home repository's main checkout: `campaign init --name NAME [--ci github]`. The owner
   writes Goals, Non-goals, Constraints and Reserved decisions in `.campaign/<campaign>/PLAN.md`.
2. Split the goals into quests: `campaign new ID --title T --risk R --files GLOBS [--needs IDS]
   [--after IDS] [--provides C] [--consumes C] [--milestone M]`; its gate is its repository's suite
   (`campaign config suite=CMD` for the home, `suite.REPO=CMD` for another), unless `--check
   NAME[:LEVEL[:MIN]]=CMD` names others. Fill in each quest file
   (`.campaign/<campaign>/quests/ID.md`): outcome, three to five criteria at outcome level, each
   naming its test (Evidence, below), contracts, decisions, what may land early.
3. When the architecture is consequential, one reviewer (lens `design`) challenges the plan and the
   quest list before anything is built. This is the one review worth waiting for.

## Quests

- **Size:** the smallest change that delivers a complete, meaningful behavior a reviewer can hold
  in mind, usually a few hundred to ~1,500 lines. Split bigger ones; fold a change not worth its
  own landing into the quest it serves.
- **`needs`** must land before a quest starts; **`after`** must land before it lands. A quest may
  start against an `after` dependency only because that dependency's quest file fixes the contract.
- **Contracts** (`--provides`, `--consumes`) name APIs, formats and protocols, so `campaign check`
  sees two quests touching one. A quest in another repository changes that repository's contracts:
  a quest building on a library names it (`--consumes http`, or `http:client` for one part), and its
  lead hears of every quest any campaign starts there.
- **Tasks:** parallel writers inside one quest (`campaign start ID --task T --files GLOBS`),
  merged into the quest branch by the lead. The quest is still reviewed once.
- **Land small and often:** `campaign land ID --upto COMMIT --reason R` lands a prefix the quest
  file's "May land early" allows; the lead allows internal code behind tests by default. Never
  early: bytes on disk or the wire, public API, user-visible behavior, or the part the review
  exists for.
- **Cross-repository work** is several quests (`--repo PATH`) ordered by `after`, with an
  integration job that pins every repository's commit (`--ref NAME=PATH@REF`). Landing is ordered,
  never atomic: each intermediate state must work. Such a quest's journal stays in the home, in a
  sparse worktree on a branch of its name (`campaign get ID journal`), merged into the home's `main`
  on landing: the other repository's history holds only its code and docs.
- **Messages:** an agent with a finding for a quest it is not working on, in any campaign on the
  machine, sends `campaign message NAME:ID "text" --from WHO`. The quest cannot land until one of
  its journals answers it by its id (`msg-N`) or the lead closes it (`campaign message ID --close
  msg-N --reason R`); `NAME:` writes to that campaign's lead and never blocks a landing. Details,
  and other machines: `references/host.md`, Messages.
- **Docs areas** under `tested_docs` are separate quests from the start, chained with `--after`;
  docs of an in-flight format change stay in its quest.
- **Scope changes:** a new ask goes into the quest file first (a criterion, or its own quest if it
  can land alone), then to the contributor; superseded work is `campaign set ID status=dropped
  reason=...`.

## Direct path first

A quest's **gate** is its suite job, which covers every untagged criterion it tests, and CI.
Sanitizers (ASan, UBSan, TSan) and the platform matrix are CI's, on the quest branch or on `main`
after landing; a red CI is fixed forward or reverted within the day. The only local sanitizer gate
is a TSan test a high-risk quest adds to prove a concurrency claim.

Crash matrices, fuzzing, soaks, mutant batches and full sanitizer sweeps are **milestone checks**
(`--later`): queued at low priority on the landed code, run once per milestone on `main`, green
before it closes. A red one is an error in `campaign check`: fix forward or revert, then `campaign
verify ID --deferred` reruns them. The exception is the crash model of a quest that writes a new
on-disk or wire format or changes a durability mechanism, which gates it. Slow CI workflows are made non-gating the same way
(`campaign config ci_later=NAME,...`).

With `ci=github`, what CI runs is a `ci:` check (`--check tsan=ci:ThreadSanitizer`), met by that CI
job passing at the exact commit; it queues no local job.

## Run a quest

1. `campaign start ID` creates `quest/<campaign>/ID` in its own worktree with a journal. Launch a
   contributor **in the background**, so it can be resumed by message, with the "Contributor"
   prompt (`references/prompts.md`). Keep every **launchable** quest running, critical path
   first, capped by what the lead can review and land and by the host's light slots. A quest is
   launchable when it is planned, its `needs` have landed, and its file set overlaps no active or
   review quest's in its repository; `campaign status` lists them in that order.
2. The contributor works in its worktree, keeps its journal current, and produces evidence as jobs
   tagged with the criteria they prove: `campaign verify ID --for A1` and `campaign job submit
   --quest ID --for A2 -- CMD`. When only jobs remain, it records their ids and ends its turn.
3. The lead waits with one background `campaign wait --commits` (no tokens until a job ends, a
   branch moves or a message arrives), then resumes the contributor by message while its context
   is small (about 150k tokens). A larger context, or a harness that cannot resume it, gets a fresh
   contributor with the "Resume" prompt, which rebuilds from the journal and job records. At
   every wake it fills idle capacity within step 1's caps: launchable quests first, then an
   exploration batch (below).
4. A high or critical quest's reviewer starts at the contributor's candidate head, in parallel with
   its evidence jobs, and writes `.campaign/<campaign>/reviews/ID-LENS.md`; the lead relays
   blocking findings by id. Any other quest: the lead reads the diff.
5. When the journal says "ready for review", the lead sets `status=review`, runs `campaign accept
   ID LENS` for each required review (Risk and review), and `campaign land ID` on a green suite and
   CI, which also removes the quest's worktrees, merged branches and build slot.

## Exploration batches

For a list worth hunting (a fresh benchmark's losing rows, a profile's hotspots, thinly proven
code, a backlog of small bugs), send several economical contributors at once, one target each,
each confirmed on current `main` first: `references/assignments/exploration.md`. Size the batch
by its targets and by what the lead can review and land, never by idle quota.

The targets live in the **hunt list**, `.campaign/<campaign>/hunt.md` (the lead's, committed;
`campaign init` writes it): one row per target with its evidence and the files to start in;
`campaign status` counts the open rows.

- When fewer quests are launchable than the lead can review, launch a batch from the hunt list.
- After a milestone closes or performance-relevant code lands, run a short benchmark on `main`
  and add its losing rows.
- Non-blocking review findings, follow-ups and small bugs go to the hunt list, not the lead's
  notes.

## Risk and review

| Risk (first match wins) | Before building | Before final landing |
|---|---|---|
| **critical**: irreversible (freezing a format or protocol for release, migrating users' data, removing user-visible behavior) | owner approval; a `design` review | the high-risk review, plus acceptance evidence the reviewer ran itself |
| **high**: persistent formats, wire protocols, durability, concurrency, security surfaces, shared public APIs | a `design` review when a decision is expensive to reverse | one review by the dominant risk's lens (`durability`, `security` or `correctness`), run in parallel with the evidence |
| **normal**: ordinary features and fixes; **low**: docs, tests, tooling, mechanical edits, behavior-preserving refactors | — | suite and CI green; the lead reads the diff |

- **One reviewer,** carrying every risk question the quest raises (the prompt names three to five);
  a second only for a question that needs separate expertise or independence.
- **A review is bound to what it saw.** It names the commit it reviewed (`Commit:`), a verdict, and
  a findings table with an ID, Blocking (yes or no) and Kind (defect, gap, nit) per row. An APPROVE
  covers that commit's code; commits that touch only `.campaign/` keep it. A CHANGES whose findings
  are all nits is an APPROVE. Editing the review after acceptance voids the acceptance.
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
| `- A1 ...` (untagged) | a job tagged `--for A1` (or a later run of it) that passed at the quest's code; with `--fails-without` tags, one of those exited 0 too |
| `- A2 [artifact] ...` | a journal row naming a `path` committed in the quest's repository; with a quote (`` `path`: "text" ``), the text it holds |
| `- A3 [decision] ...` | a journal row naming an id in `PLAN.md`'s Decisions table |
| `- A4 [manual] ...` | a journal row saying who checked what; shown as manual, never as automated proof |

`campaign brief` generates the journal's Evidence table; an untagged criterion of a journal written
before tags is read from its row. The quality bar (`references/review.md`): tests check values; a
fix's test is shown failing without it once; a performance
claim has one short before/after run (`references/measurement.md`).

## Machine work

| Level | For | Isolation (`quiet`) |
|---|---|---|
| `none` | tiny single-threaded probes | no hold |
| `light` | builds, test suites, fuzzing | half the CPUs at once, nice 19, while memory lasts |
| `strict` | every other timing, behavioral runs | own cores on Linux; alone on macOS |
| `exclusive` | numbers that decide or get published | the machine to itself |

Jobs run at the commit they were submitted for, in their own checkout, with the submitter's PATH.
A result is reused for identical code (without `.campaign/`), command, directory, level, `--env`,
host and toolchain, and across merges from `main`, documentation-only commits, unchanged read sets
and `--inputs` scopes (`references/host.md`). Name a mutable input with `--env`; rerun with
`--force` what depends on more. A strict or exclusive job lasts at most 10 minutes, a light fuzz
or soak 30, and each is stopped at its deadline; `--long REASON` lifts the cap and defers it. Every process a
job starts dies with it. The worker starts by itself, exits when the queue is empty, and prunes
daily (`campaign job prune`: unnamed jobs after 14 days, evidence keeps its record, idle build
directories of worktrees no open quest owns after 3 days); `campaign sweep` removes clean
worktrees of merged or finished work; `campaign check` warns under `disk_warn` free GiB (100) with
the largest consumers. Mechanics: `references/host.md`.

## Integration

`campaign land` refuses with each gap `campaign brief` lists: dependencies, a merge of `main` when it
changed code, the file set, tasks, criteria, messages, the suite at its code, reviews, CI.
It fast-forwards `main`, or merges when only campaign files differ, verifies that the landed code
equals the tested code, records the landing, and pushes only with `--push`. Exceptions are explicit
and recorded with the landing: `--upto COMMIT --reason R` (an increment), `--no-ci REASON`, and
`--record COMMIT --reason R` (a landing done by hand, with the gates it never checked). A red
milestone check is fixed or reverted before the next landing.

Other campaigns on the machine share the job queue, `quiet` and the quota. `campaign check` reports
their quests that overlap yours or change a contract you consume; order them with `--after
NAME:ID`, which landing enforces, and tell their lead (`campaign message NAME: "..."`).

## Recovery

Every session, lead or contributor, starts with `campaign brief` (in a worktree, its quest) and
`campaign check`, then reads the journal and `git log`. Observable state wins: git, worktrees and
job records over any document. A dirty worktree is kept, never discarded. An interrupted landing is
completed or cleared by the next state-writing command. A job whose runner died is requeued when it
allows retries or its command never started (up to three tries), else marked `interrupted`: it
may have run partway, so resubmit it when its command is safe to repeat (builds, tests and
measurements are), else check what it changed first. Never invent a missing decision: write it
under "Blocked on" and ask.

## Cost

- No minimum number of agents, no utilization target; `pace` is advice, launch by the work.
- The lead coordinates; it does not re-derive what the state holds. It reads this file and
  `references/prompts.md`, and other references only when the table below says; it orients with
  `campaign brief` and learns what changed from `campaign wait --commits`, not by rereading
  journals, diffs or code.
- Prompts point at files and commits; final reports stay under ~250 words, detail in the journal
  or review file. `campaign usage` shows tokens, cost and time per quest, role and assignment.
- Report to the owner on events (a landing, a blocker, a decision), from `campaign status` and
  `campaign map`, never by waking agents. The periodic report quotes `status`'s host and waste
  lines; the lead owns the queue.

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
