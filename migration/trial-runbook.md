# The first week: runbook

For the owner, while one moved project (Detent first) runs on campaign for about a week. The move
itself is [checklist.md](checklist.md); the readiness verdict and its conditions are in
[preflight.md](preflight.md).

## Before the move

1. Land this branch on `main` of agent-campaign, then on the machine that will run the project:
   `cd tests && python3 run.py` (about five minutes) and `install.sh --dry-run`.
2. On the machine where the earlier campaign ran (for Detent, the Codex host): push every branch it
   worked on, task branches included (`quest/dense-pages--deltas`, `--integration`,
   `--performance` were not pushed with `quest/dense-pages`), and keep its job directory
   (`~/.scratch/campaign/jobs`): moving there lets the new tool reuse those jobs.
3. Park, install and map the work by the checklist. For Detent: one `dense-pages` quest from the
   pre-release branch (checklist, step 3, last case; rehearsed on a copy, `preflight.md`), and the
   parked 1.x work (`reader/s6`, `namespace/s3b`, `namespace/s1-s2`, the library adoptions) as
   planned quests marked blocked, so nothing starts beside Dense pages by accident.
4. Validate before resuming (checklist, step 5), then resume one contributor and read `campaign
   usage` after its first turn before launching more.

## Every day

Collect, from the home checkout, into a dated file outside the repository:

```sh
campaign usage --json > usage-$(date +%F).json   # tokens by role, cached share, resumes, job time
campaign status --reviews                        # verdicts and blocking defects per review
campaign check                                   # must stay without errors
du -sh ~/.scratch/campaign/jobs                  # job records; `campaign job prune --dry-run` shows what could go
git log --oneline --since=yesterday -- .campaign # what the lead did: starts, accepts, lands, weakenings
```

What the week should answer, from those files:

| Measure | From |
|---|---|
| Tokens per landed quest, split lead / contributors / reviewers | `usage` runs and lead sessions, landings in `git log` |
| Recovery cost: fresh resumes per quest and their tokens | `usage`, `recovery:` line |
| Review yield and rework: CHANGES verdicts, fix rounds, blocking defects found | `status --reviews`, review files |
| Machine time and queue delay of gate checks | `usage` jobs (`ran`, `waited`) |
| Exceptions taken: `--weaken`, `--waive`, `--fix-scope`, `--no-ci`, `--record` | `git log -- .campaign` messages, quest `history`, landing records |

## Stop and look when

- A quest is weakened, waived, landed `--no-ci` or recorded by hand without the owner having agreed:
  each is in `git log -- .campaign` with its reason.
- `--fix-scope` appears more than once or twice: fixes are growing past what reviewers saw.
- A third fix round on one quest: re-plan it (SKILL.md, Risk and review).
- More than two fresh resumes of one quest, or the lead's tokens exceeding all contributors'
  together for a day: the lead is re-deriving state instead of reading `campaign brief`.
- Gate checks waiting more than an hour for the machine, or `interrupted` jobs in `campaign check`.
- The job directory passes a few GB: `campaign job prune --dry-run`, then prune.
- Anything that would create a cloud or billable resource: stop the agent.

## Roll back

[checklist.md](checklist.md), Roll back. What landed stays on `main`; quest branches continue as
ordinary branches.
