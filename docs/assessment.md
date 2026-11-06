# Campaign 2.0: final engineering assessment

> Status: CURRENT (2026-11-05). The short version for whoever decides whether 2.0 replaces 1.x.
> Evidence: `audit.md` (1.x, with its records), `research.md`, `architecture.md`, `challenge.md`,
> `validation.md`. Figures marked "estimate" come from 1.x's measured medians, not from 2.0 runs.

## Recommendation

Adopt 2.0 for new campaigns now; migrate live 1.x campaigns at a natural pause (a landed
milestone), using `references/migration.md`. Keep `main` (1.x) as the rollback until one real
campaign has run a week on 2.0. The `quiet` changes are worth taking even without the rest: they
fix a measurement-validity bug every agent on the machine is exposed to.

## What became substantially simpler

1. **Roles:** 15 roles and 14 agent types become a patron, a game master, adventurers and
   paladins (two agent types). An assignment or a lens carries what used to be a role.
2. **Instructions:** 25,142 words agents read become 10,284 (59% less). `SKILL.md` is 255 lines
   instead of 555 and is self-sufficient: a fresh agent ran a campaign from it alone.
3. **Review policy:** a floor table, an adjust table, defaults, five rules and a proof audit
   become one four-row risk table with lenses.
4. **State:** ledgers, a status index, an exploration log, a document index, status lines and a
   book outline become one tool-written `state.json`, a quest file per quest, a journal per branch,
   and git. The quest log is derived, never maintained.
5. **Host rules:** "at most 30 minutes", "bound every helper", "never wait on CI", "rebase before
   landing", "never edit your criteria", "one writer per file set" stop being sentences and become
   behavior of `quiet` and `campaign`.

## What became cheaper

- **Rechecks and proof audits are gone:** ~1.1M tokens of confirming rechecks in two weeks of
  strata, plus a ~60k audit before every review. Fixes are confirmed by rerunning probes as jobs.
- **One review per quest,** not per branch or step; design text is reviewed once (1.x ran up to
  seven rounds on one design).
- **No handoff from finder to finisher:** a scout or adventurer finishes what it found; 1.x paid a
  second agent to reproduce every explorer win.
- **No waiting agents:** jobs queue on the machine while agents end their turns; the game master
  waits in one background command. 1.x agents waited up to hours on holds.
- **No manufactured work:** no two-agent floor, no 95-97% quota target.
- **Direct path first:** only gate checks block a landing; other platforms, soaks and full
  matrices run afterwards at low priority and gate the milestone, so nobody idles behind them.
- **Reviewers reuse the adventurer's results** for identical code and toolchain instead of
  rebuilding.
- Estimated effect (estimate): 30-45% fewer tokens on medium and high-risk workflows, ~40% on a
  small fix found by measurement, with the reviews that found real defects in 1.x kept. Not
  measured head to head (`validation.md`, "Comparison with 1.x").

## What remains expensive, and why

- **First reviews of risky code.** In 1.x these found a use-after-free, a deadlock, lost wake-ups
  and a durability protocol error. 2.0 keeps them on purpose.
- **Adventurers.** Implementation (240k tokens median in 1.x) is the work; nothing here makes it
  cheaper beyond fewer handoffs and smaller instructions.
- **Scouting parties** spend tokens by design: they had the best yield in the records (20 of 33
  runs landed at a median of 93k).
- **Reruns after rebases:** evidence must match the landed code, so a rebase over another quest's
  code reruns the fast checks. A merge queue would amortize this; at a few landings a day it is
  not worth building.
- **The tool is new code:** ~2,200 lines of Python (stdlib only) and +58 lines of `quiet`.
  Scripts grew from 1,632 to 3,076 lines, while instructions shrank by ~14,900 words. The trade is
  deliberate: code is tested (66 tests, 22 of 22 mutants killed) and prose was not.

## Quality safeguards that remain

Criteria set before building and immutable to the builder (now enforced); fresh, independent
review for every normal, high and critical quest, with lenses for security, durability, design and
system questions; the proof standards and concurrency list; measurement discipline with
exclusive, deadline-bounded measurements; evidence tied to the exact code that lands; CI on every
platform before landing; one writer per file set (now enforced); the patron's authority over goals
and user-visible behavior; recovery from files alone (tested with a fresh agent).

## Removed

Twelve agent types (explorer, developer, validator, performance engineer, researcher, designer,
maintenance engineer, tech writer, code reviewer, security reviewer, architect as separate types,
rechecker, proof auditor, quartermaster), `pace advise` and its utilization targets, `lease` and all
cloud provisioning, phase gate reports and calendar system reviews, the status index, the document
index, universal status lines, the book outline, the daily document audit, and the exploration log
(job records and the quest log replace them).

## Deferred, deliberately

- **Cloud capacity.** Needs the patron's decision and provider-side controls (`architecture.md`,
  "Cloud").
- **Batched landings (a merge queue)** and **atomic multi-repository landing.**
- **Agent leases** (claims with heartbeats): in-process subagents die with their game master, so
  only jobs needed them.
- **Live validation in Claude Code, Codex, Linux, and on remote hosts.**
- **A job-directory pruner:** old jobs are deleted by hand once their quest lands.

## Risks that remain

1. **Small-sample validation.** Two live agent runs on toy repositories and a scripted suite; no
   multi-day campaign yet. The first real campaign is the real test.
2. **Behavior change in `quiet` for every agent:** strict and exclusive commands now stop at their
   deadline, and holds over 30 minutes are refused. Anything that relied on running on unheld will
   now fail loudly, which is the intent.
3. **Evidence identity excludes `docs/campaign/`.** A test that reads campaign metadata would be
   mis-keyed; none should.
4. **Risk is still a judgment.** The table is short, but the game master classifies; a
   misclassified high-risk change gets a lighter review. `campaign status --reviews` and the lore
   file are the feedback loop.
5. **Theme vocabulary.** Quest, adventurer and paladin are defined on first use; a reader who
   skims may still need the mapping in `references/migration.md` or the architecture's vocabulary
   table.

## Evidence for the claims above

| Claim | Evidence |
|---|---|
| Less instruction text, fewer roles | `wc` on both branches (`validation.md`) |
| Recovery works from files alone | live fresh-agent test: 8 orientation calls, nothing lost or duplicated |
| A newcomer can run it from SKILL.md | live first-time game master: three quests landed, one reviewer launched |
| Mechanisms are enforced, not hoped for | 66 tests; 22 of 22 mutants killed (14 core, 8 direct-path) |
| `quiet` keeps measurements valid under contention | deadline, grace, starvation and load tests, each with a revert check |
| Fewer tokens per workflow | estimate only, from 1.x medians |
