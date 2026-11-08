# Migration (temporary)

Everything here exists only to move the last few projects onto campaign, and is deleted, with
`git rm -r migration`, once they have moved. The skill, the tool and the tests do not depend on it.

| File | What |
|---|---|
| [checklist.md](checklist.md) | the steps to move one project, and to roll back |
| [trial-runbook.md](trial-runbook.md) | the first week of running a moved project: what to watch, when to stop |
| [preflight.md](preflight.md) | the review before that week: findings, cost, naming, the rehearsal, readiness |
| [redesign/](redesign/INDEX.md) | the record of the rewrite this skill came from |

A lead running a move reads `checklist.md`; the owner reads the runbook and `preflight.md`.

## Steps

The whole procedure, in order. `checklist.md` has the detail of steps 4 to 7.

**On each machine, once**

1. Park every session on the machine: agents committed and stopped, `quiet list` empty, no job
   queued or running. Old and new `quiet` misread each other's holds, so the machine moves at once.
2. Install, in the machine's dotfiles: the campaign submodule at agent-campaign `main`; the
   `campaign-contributor` and `campaign-reviewer` agent links in place of `adventurer` and
   `paladin`; `scripts/sync-agents.py` for the Copilot and Codex agent files; one commit; then
   `./install.sh && ./install.sh --check`, `sync-agents.py --check`, and `command -v lease` prints
   nothing. Run the suite once on the machine (`tests/run.py` under `quiet light`).
3. Restart the sessions, so they load the new agent types.

**For each project** (Detent, Xapiand)

4. Park it: commit every worktree's uncommitted work as `wip: parked` (never a mid-merge: save it
   on a `wip/` branch instead), push every branch, and record `git for-each-ref refs/heads` and
   `git worktree list` outside the repository.
5. Create its campaign: a pre-release campaign in `docs/campaign/` is moved in place, with its
   registry entry renamed and its jobs retagged (Detent); otherwise `campaign init --name NAME`
   (Xapiand). Set `ci=github` where landing needs green CI. Through step 8, export
   `CAMPAIGN_NO_COMMIT=1`: the tool writes its state without a commit per change, and the
   migration lands as one commit of `.campaign/NAME` and the history marks.
6. Map the work: `PLAN.md` from the lead's plan (goals, milestones with their validation,
   decisions); each workstream a planned quest with its milestone, its branch created for
   adoption in its own repository (`git -C REPO branch quest/NAME/ID BRANCH`), its ledger's
   criteria never weakened; reviews by checklist step 4. Quests start in the lead's order, since
   `start` refuses overlapping files. Completed work comes in as history, so the map shows the
   whole campaign: per finished ledger a quest whose criteria cite the ledger, a journal on `main`
   with its Result (numbers, review, docs), and `campaign land ID --record COMMIT --earlier
   --reason "LEDGER; REVIEW; CI"` at the commit where it landed.
7. Keep what was learned: the earlier ledgers, plans and handoffs get a `Status: HISTORY` line and
   stay; results and how-it-works go in the docs of the repository each quest changes. Point
   `AGENTS.md` at `.campaign/NAME/` and stop any old run queue.
8. Verify: `campaign check` clean, `campaign status` and `campaign map` read right, every recorded
   branch head unchanged or adopted. `campaign map --write` keeps the map in the repository.
9. Hand it to the lead: it reviews the map against its plan, corrects the mapping, and resumes one
   contributor at a time.

**The trial week**

10. Follow `trial-runbook.md`: the daily records, the warning signs, and rollback if needed.

## When every project has moved

Once each lead's `campaign check` is clean with all its work mapped, and the trial week's records
are read:

1. Move what lasts into the product first: harness findings to `references/harness.md`, a lesson
   worth a rule to `references/lessons.md`. Then, in one commit, `git rm -r migration`. Nothing
   else in the skill, the tool or the tests refers to it.
2. Delete the merged branches it used on GitHub (`preflight-hardening`, `claude-code`).
3. On every machine with the skill installed, confirm step 2 above was done, then remove
   `~/.scratch/campaign-install.lock` if present.
4. In each moved project, delete a safety branch only after its lead has adopted, merged or
   dropped its work: Detent's `wip/main-parked-2026-11-08`, `wip/dense-s2-parked`,
   `wip/dense-t2-base-parked`, `wip/laptop/ci-fixture-logs` and `wip/laptop/legacy-wave2`;
   Xapiand's `wip/laptop/fetchcontent-leaf-libs` and `wip/laptop/fmt-to-std-format`; strata's
   `wip/laptop/perf-lanebuf`; queryparser's `wip/laptop/main-2026-11-08`.
5. Keep the earlier skill's ledgers, reviews and handoffs in the projects, marked as history: they
   hold results and reasoning. Old worktrees and merged branches go by each lead's cleanup.
