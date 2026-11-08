# Preflight review before the first week

Date: 2026-11-08. Branch `preflight-hardening` of agent-campaign, from `agent-campaign:b4b7754`. A
review and hardening pass before one project (Detent) runs on campaign for about a week. Commits
named here are `agent-campaign:` unless marked. This file goes away with `migration/`.

## Baseline

At `agent-campaign:b4b7754`: 127 tests pass (2 skipped) in 250 s on macOS, ruff and pyrefly (strict)
clean. Context loaded at launch, in words: lead 3,161, contributor 1,005, reviewer 1,440 plus
`lore.md` (898) its prompt sent it to, every session 252. Roles: patron, game master, adventurer,
paladin, six classes. No job records or campaigns existed on the host.

## Findings

Each confirmed defect has a test that fails on `b4b7754` and passes after the fix (verified by running
the new tests against the old code before fixing).

| # | Issue | Sev | Evidence | Disposition | Test | Residual risk |
|---|---|---|---|---|---|---|
| 1 | A CHANGES review was confirmed by any finished job: another repository's, a job running `true`, one at the old commit | P0 | reproduced: a job from another repository running `true` confirmed a CHANGES review | fixed `ec6b008`: `accept --confirm FINDING=JOBS`; each blocking finding needs jobs that passed at the accepted code in the quest's repository; a blocking defect needs changed code; whether the probe failed at the reviewed commit is recorded | `ReviewBinding` | a trivially passing probe is still accepted: whether a command tests the finding is judgment |
| 2 | An APPROVE was accepted at the branch head, whatever commit the review saw | P0 | test | fixed `ec6b008`: it covers the code at the review's `Commit:`; `.campaign/`-only commits keep it | `ReviewBinding` | a design acceptance binds the quest file, not code (intended) |
| 3 | A review edited after acceptance kept the acceptance | P0 | test; a live trial's lead appended its own disposition to a review file | fixed `ec6b008`, `c212b84`: the review's blob id is recorded; a change voids it; `accept` commits the review | `ReviewBinding` | none known |
| 4 | A waiver was accepted over an existing CHANGES review | P0 | test | fixed `ec6b008`: refused; a review written after a waiver voids it | `ReviewBinding` | none known |
| 5 | A narrative row ("tested it, all green"), a missing file or an unrecorded decision met a criterion | P0 | test: 3 of 4 met | fixed `ec6b008`: criteria name their evidence: job (default), `[artifact]`, `[decision]`, `[manual]` | `Evidence` | `[manual]` is trust by definition, shown as manual |
| 6 | Risk lowered, a lens removed, a failing check replaced by `true` or deferred, silently | P0 | tests | fixed `ec6b008`: `set --weaken REASON`, kept in the quest's history, shown by status, voids acceptances; a risk's minimum review enforced at new, set and land | `RiskPolicy` | any reason can be typed: the owner reads `git log -- .campaign` (runbook) |
| 7 | `--no-ci` left no trace; `--record` needed no reason and dropped the gates; `--upto` could land the whole quest without review | P0 | tests | fixed `ec6b008` (table below) | `Increments`, `Integration`, `Recovery` | increments trust the lead's "May land early" |
| 8 | "Verdict: APPROVE WITH CONDITIONS" read as APPROVE | P0 | Detent rehearsal accepted a 1.x conditional approval | fixed `1837eb3`: the verdict is the whole line | `ReviewBinding` | none known |
| 9 | A job ran with the environment of whichever session started the worker, while its key carried the submitter's toolchain | P1 | test: a tool on the submitter's PATH not found | fixed `a7d6e58`: the spec carries PATH | `Identity` | other variables are the worker's: declare inputs with `--env` |
| 10 | The reaper killed an unrelated session that reused a dead runner's pid | P1 | test: bystander killed | fixed `a7d6e58`: only a gone leader or the job's own runner | `Crashes` | none known |
| 11 | `wait` on a job with a corrupt state file never ended | P2 | test | fixed `a7d6e58`: `corrupt` is terminal | `Crashes` | none known |
| 12 | Job directories, kept checkouts and slots grew without bound | P1 | no mechanism | fixed `a7d6e58`: `campaign job prune [--days 14] [--dry-run]`, keeping what open quests, journals, reviews, landings and acceptances name | `Prune` | manual by design: no daemon |
| 13 | `install.sh` replaced any symlink in its way; no dry run, no uninstall | P1 | test in a throwaway home | fixed `a696c98` | `test_install` | none known |
| 14 | `usage` printed unavailable cost as 0 and one token count | P1 | test | fixed `d233737`: cached, output, reasoning, requests; `n/a`; fresh resumes as recovery | `Usage` | Claude Code logs not read |
| 15 | File-set overlap by glob prefix refused unrelated files | P2 | rehearsal: `tests/*dense*` blocked another test | fixed `1837eb3`: exact when one side is a file | `Ownership` | two patterns still compare by prefix |
| 16 | `check` held a task to the quest's file set, not its own | P2 | rehearsal: a task writing its sibling's file went unreported | fixed `49f6764` | `Ownership` | none known |
| 17 | A measurement result reused at identical code | concern | review of `job_key` | intentional: a strict or exclusive A/B is one self-contained job; key documented with what it does not see (host.md) | existing | drift of the same host over days |
| 18 | Slots reuse untracked build outputs | concern | `prepare` | accepted: tracked files verified clean; critical quests and `--fresh` check out afresh | existing | a stale generated file |
| 19 | A host suspended past 60 s has live runners reaped on wake | concern | `reap` | accepted: the attempt is requeued or `interrupted`, never recorded done (fencing) | existing | lost machine time |
| 20 | Two identical submits at the same instant both run | concern | `submit` has no lock | accepted: a duplicate run, never a wrong result | none | machine time |
| 21 | Retries | concern | `submit`, `verify` | intentional: off by default; checks from `verify` retry once (builds and tests) | existing | none |
| 22 | The redesign's token savings were estimates | concern | `migration/redesign/assessment.md` | valid; measured below where possible | n/a | unmeasured end to end |
| 23 | Personal style skill loaded for docs; personal paths and tools (ccache, `~/.profile`) in references | P2 | a live trial's lead loaded `german-writing` for a README task | fixed `202796d` | n/a | none |
| 24 | A review rewritten while `accept` ran was accepted under the earlier verdict (parsed and hashed in two reads) | P0 | independent review; test swaps the file mid-accept | fixed `0bc2e76`: the blob of the bytes parsed | `ReviewBinding` | none known |
| 25 | `--no-ci ""` landed without CI and without a record | P0 | independent review; test | fixed `0bc2e76`: every bypass reason must be non-empty | `Integration` | none known |
| 26 | `job prune` deleted a landed quest's job that an open quest's gate reused | P1 | independent review; test | fixed `0bc2e76`: open quests' gate evidence kept | `Prune` | a landed quest's gate logs may be pruned (its summary stays): policy |
| 27 | `install.sh --uninstall` deleted any agent file that mentioned the generator | P0 | independent review; test | fixed `0bc2e76`: ownership is the generated line | `test_agents` | none known |
| 28 | `land --record` audited only that each lens had an acceptance | P2 | independent review; test | fixed `0bc2e76`: the same review gaps as `land` | `Recovery` | none known |

`campaign migrate`, the `docs/campaign/` layout and the other pre-release compatibility code were
removed (`1a45a60`): the few projects still on them move once, by `checklist.md`.

## Landing exceptions

The tool writes state only from the home's main checkout; it cannot tell the lead from a contributor
there. Contributors are told never to land or set state; harness permissions are the only hard stop.

| Exception | Bypasses | Needs | Recorded | After a crash |
|---|---|---|---|---|
| `land --no-ci REASON` | the CI gate | a reason | the landing record (`ci_skipped`), `status` | carried in the landing intent |
| `land --upto C --reason R` | criteria and reviews for that prefix (gate checks still run) | "May land early" filled; not the whole quest | the landing record (`final: false`, reason) | the same intent recovery |
| `land --record C --reason R` | every gate (the merge happened by hand) | a reason | the landing record, with the gates it never checked (`unverified`), `status` | n/a |
| `accept --waive REASON` | the review of a low or normal risk quest | no review file; not high or critical | the acceptance (`by: waived`) | n/a |
| `accept --fix-scope REASON` | a re-review of fix files the review never saw | a reason | the acceptance (`fix_scope`) | n/a |
| `set ... --weaken REASON` | risk, lenses, checks | a reason; the risk's minimum review stays | the quest's `history`, the commit message; acceptances voided | n/a |
| `start --force REASON` | needs, one writer per file set | a reason | the quest's reason | n/a |

None turns a failed check into a passing one: a failing gate can only be replaced through
`--weaken`, which is visible in the history and in `git log -- .campaign`.

## Evidence identity

A job's key: host, toolchain fingerprint (taken on the submitter's PATH), repository, the code of each
commit without `.campaign/`, refs, command, directory, level, `--env`. What the key does not see is
named by the submitter (`--env` for a dataset version, `$CAMPAIGN_FINGERPRINT` for installed
libraries, `--force` otherwise); `references/host.md` has the table. Metadata-only commits keep the
evidence; another commit with identical code reuses it (`test_campaign_metadata_commits_keep_the_evidence`).

## Cost

Measured, not estimated:

| What | Before | After | Source |
|---|---|---|---|
| Lead's load at launch (words) | 3,161 | 3,135 | `scripts/context-size.py` |
| Reviewer's load (words) | 2,338 (1,440 + `lore.md` 898) | 1,508 | the same; the prompt no longer sends it to the lessons |
| Contributor's load (words) | 1,005 | 947 | the same |
| Every session's listing (words) | 252 | 235 | the same |
| `campaign brief` / `status` on the Detent rehearsal (median of 7, strict hold) | 2.63 s / 3.94 s, 46 / 71 git processes | 2.83 s / 4.16 s, 47 / 72 | `bench-cli.sh`: about 7% more, from the review checks |
| A fresh contributor resuming a 15-criterion critical quest (Detent copy) | 1.39M and 0.54M tokens for the two fresh resumes in the earlier live trial (smaller quests) | 569,207 tokens, 86% cached input, 5,115 output, 9 tool calls, 66 s; oriented correctly | `campaign usage` |

What the records show about where tokens go:

1. **The lead.** In the Codex trial the lead was 51.5% of 10.3M tokens in ten minutes; in the earlier
   Copilot trial the lead used 5.82M input tokens (92% cached) over 54 requests and 57 tool calls, 8
   of them reading references before any work, and loaded the owner's writing skill for a README.
   Changes: the lead reads SKILL.md and `prompts.md` only and orients from `campaign brief`; `accept`
   commits the review (one fewer turn per review); the writing skill is no longer loaded for docs.
2. **Fresh resumes.** The earlier trial paid 1.93M tokens for two fresh resumes because a contributor
   launched in the foreground could not take a message. Contributors now launch in the background.
3. **The reviewer** used about as many tokens as the builder (2.05M against 1.99M). Its prompt now
   starts from `campaign status <ID>`'s criteria and jobs, says not to rebuild what a passing job
   shows, stops when the named risks are answered, and drops the lessons file.
4. **Models.** Defaults are tiers: economical for bounded implementation, exploration, documentation
   and maintenance; strong for design, performance, research, high-risk work and reviews.

Not measured: tokens per landed quest before and after on comparable work. That needs the week's
records (runbook), not another synthetic run.

## Naming and writing

Roles are owner, lead, contributor and reviewer; the agent types `campaign-contributor` and
`campaign-reviewer` (prefixed so they do not collide with a user's own `reviewer`); classes became
assignments (implementation, design, research, performance, exploration, documentation,
maintenance); `lore.md` became `lessons.md`. "Quest" and "journal" stay: they name branches
(`quest/<campaign>/<id>`), state and flags, and renaming them would change every command for no
gain in clarity. There is no alias layer and no version: the product names no earlier one.

Records (journals, reviews, decisions, messages) are terse and factual; engineering documents are
plain technical prose; a personal writing-style skill only for public writing the owner asks for.
How-it-works notes and the book outline are optional; the quest template no longer adds a mandatory
note criterion.

## Migration rehearsals

Both on copies, with pushes disabled; nothing was migrated in a live repository.

**Detent's 1.x state** (`~/.scratch/preflight-detent/Detent`, 73 branches, 25 ledgers, 33 reviews).
Simulated three unpushed Dense pages worktrees, one with uncommitted work, and a 1.x APPROVE for
`fix/variant13`. Mapped Dense pages to one critical quest with two tasks and K1-K15 as A1-A15 (A1
`[artifact]`, A14 `[decision]`), parked work as blocked quests, the persistent-map follow-up as a quest
in its own repository, and adoption `--after` it.

| Check | Result |
|---|---|
| Commits and uncommitted work kept | all 78 recorded branch heads unchanged; the WIP commit is on the quest branch; old worktrees clean |
| Decisions and 1.x files intact | `main` changed only in `.campaign/`; Q1 and Q2 carried with their owners |
| Review inheritance scoped | an APPROVE of the exact code accepted; one of earlier code refused, naming the 8 files changed since; "APPROVE WITH REQUIRED CHANGES" refused |
| Criteria not weakened | 15 of 15 carried |
| `campaign check` | clean, then flagged a task writing its sibling's file (after fix 16) |
| A fresh agent resumes | correct state, tasks, pending job, rebase need, voided acceptance, check warning, decision owners and next step; 9 tool calls |
| Rollback | the procedure removes only campaign links (`test_install`); nothing in the repository changes |

Found and fixed: 8, 15, 16; and in the checklist, parked work as blocked quests started later.

**The real pre-release trial branch** (`quest/dense-pages`, pushed from the Codex host, 140
commits). Moved by the checklist into a fresh clone: state fields into `campaign new`, quest file and
plan copied, branch adopted, journals moved to `.campaign/` and `docs/campaign` removed in one
commit. The code tree is `7fcfd691b209` before (old tool) and after (new tool), so its jobs are reused
once the job records are there. `campaign check` clean; status shows 15 criteria without rows (the
trial's journal kept evidence as prose), the gate check to run, and three reviews to do. The
`deltas` task branch was never merged or pushed: its work is only on the Codex host.

## Readiness

**Go, for a controlled week on Detent**, under these conditions:

1. Land this branch and run the full suite on the host that will run the campaign.
2. Push every task branch from the Codex host (`quest/dense-pages--deltas` at least) and keep its job
   directory before moving; otherwise the deltas work and the reusable jobs stay behind.
3. One contributor at a time at first; the runbook's daily records and stop conditions apply.

Open, none blocking: no end-to-end token comparison yet; Linux, Claude Code and real GitHub CI were
not exercised by this pass (the suite ran on macOS; earlier Linux runs predate these changes);
Codex loads the renamed agent types only in a fresh session (body fallback until then); the
independent review's five findings are fixed (rows 24-28).

## Independent review

One review by a different model family (`gpt-6.1-sol`), of the verification, job and installer
changes against `b4b7754`, scoped to six invariants, read-only: 666 s. It reported five defects, each
with a reproduction; all five were confirmed by a test that failed on the code it reviewed, and fixed
(rows 24-28). No second review round was run; the targeted tests and the full suite were rerun.
