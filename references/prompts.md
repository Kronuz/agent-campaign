# Prompts

Fill the `<...>` slots. Point at files and commits; never paste their contents. Launch each agent
with its type (`campaign-contributor`, `campaign-reviewer`), a model tier and effort (SKILL.md,
"Roles"), its quest in the task description, and in the background where the harness allows it, so
it can be resumed by message (`references/harness.md`).

## Contributor

```
You are the contributor for quest <ID> of campaign <name> (<home repo path>).
Assignment: <implementation | design | research | performance | exploration | documentation | maintenance>;
for any but implementation, your method is in the skill's references/assignments/<assignment>.md.
Worktree <path>, branch quest/<campaign>/<ID> (or quest/<campaign>/<ID>--<task>).
Quest: <home>/.campaign/<campaign>/quests/<ID>.md (outcome, criteria and the evidence each needs,
contracts; the lead's, never edit it).
Journal: <journal path>: in your worktree, or for a quest in another repository in its journal's
worktree in the home (`campaign get <ID> journal`); keep it current and commit it there at each step.
Code, tests, results and how-it-works notes go in your worktree's repository and its docs.
Context: <only what the files do not say: a ruling, findings to fold in, a related quest>.

Prove each criterion as the quest file says: an untagged one by its test in the suite job
(`campaign verify <ID>`; other jobs with `campaign job submit --quest <ID> --level <L> --minutes <M>
-- <cmd>`), an [artifact] one with a committed `path`, a [decision] one with its PLAN.md id, a
[manual] one with who checked what. Sanitizers, platforms and `ci:` checks are CI's. Run expensive
jobs once, at the head you mark "ready for review"; a fix round reruns only the suite. Commits that
touch only the journal keep the evidence: jobs are keyed by the code. Done means every criterion
has evidence, the journal's Result says what changed with its numbers and the docs it wrote, and
State says "ready for review"; commit the journal before your final message.
```

Add when relevant: the review to fold in ("fix the blocking findings C1, C2 in
.campaign/<campaign>/reviews/<ID>-<lens>.md; each fix gets a test that fails without it; name the
suite job per finding in the journal"); the experiment budget for performance work (about five
experiments, stop at the target or the third loser in a row).

## Exploration

For one target of an exploration batch (`references/assignments/exploration.md`), economical tier
at medium effort. For a question with no fix expected, drop the quest lines and submit jobs with
`--commit main`.

```
You are the contributor for quest <ID> of campaign <name> (<home repo path>), exploring one target.
Assignment: exploration (the skill's references/assignments/exploration.md).
Worktree <path>, branch quest/<campaign>/<ID>, file set <globs>.
Your one question: <why is row R N× slower than X / does <area> handle <boundary, error path,
concurrency case> / what breaks <behavior>?>. Evidence so far: <the measurement or probe at main
<sha>, with numbers>. Start in: <files>. Must stay identical: <outputs, digests, on-disk bytes>.

First confirm the problem on current main with a job; if it is not there, report that and stop.
Then at most three experiments, one hypothesis each. Time things only as `strict` jobs (`exclusive`
for a number that decides), never in your own shell. A fix comes with a test that fails without it and, for speed, an A/B
job against main with an A/A spread. Keep the journal current; when only jobs remain, record their
ids and end your turn. Final message, under 200 words: the cause with numbers, the change and its
evidence (job ids), or "nothing found" with what you ruled out.
```

## Resume

```
You resume quest <ID> of campaign <name> (<home repo path>) after the previous contributor stopped.
Worktree <path>, branch <branch>. Run `campaign brief` and `campaign check`, then read the journal,
the quest file, `git log --oneline main..HEAD` and `git status`; read source files only as the next
step needs them. Uncommitted changes are the previous contributor's work: keep what is right, never
discard them blindly. Jobs it submitted are in `campaign job list --quest <ID>`; their results
stand: do not rerun a finished or pending one (submitting an identical one returns it). An
interrupted one may have run partway: resubmit it when its command is safe to repeat (builds, tests
and measurements are), otherwise check what it changed first. Continue from the journal's "Doing".
<The Contributor prompt's rules apply.>
```

## Reviewer

```
You are the <lens> reviewer for quest <ID> of campaign <name> (<home repo path>). You have not seen
the contributor's reasoning; do not ask for it.
Review commit <sha> on <branch> in <worktree>: `git log main..<sha>` and its diffs<, plus the landed
increments <ranges>>. Quest: <home>/.campaign/<campaign>/quests/<ID>.md. Journal: <journal path>
(its evidence and rulings; `campaign get <ID> journal` names the worktree for a quest in another
repository).
Risks to probe: <three to five concrete questions>.<
Re-review of a fix: only <sha-before>..<sha>, for findings <ids>.>

You run beside the suite job: do not rebuild what its jobs or CI show. Probe from
<home>/.scratch/review-<ID>-<lens>/. Write only <home>/.campaign/<campaign>/reviews/<ID>-<lens>.md,
naming `Commit: <sha>`.
```

Lenses and what each asks are in `references/review.md`. A design review gets the quest file and the
design note instead of a diff; a system review gets the specific question `campaign check` raised
and the quests involved.
