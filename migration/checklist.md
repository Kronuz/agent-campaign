# Moving a project onto campaign

A one-time procedure for the few projects run by the earlier campaign skill (ledgers in
`docs/status/`, reviews in `docs/reviews/`) or by a pre-release build of this one (files in
`docs/campaign/`). Nothing here rewrites history: old branches, worktrees, ledgers and reviews stay
as they are, and campaign adds `.campaign/<name>/` and `quest/<name>/...` branches beside them, so
rolling back is a matter of links.

## Steps

1. **Park the old campaign.** Let agents commit and stop; let holds and jobs drain (`quiet list`).
   In every live worktree, commit what is uncommitted (`git add -A && git commit -m "wip: parked"`):
   adoption makes new worktrees and never sees an old one's working tree. Work on another machine
   comes over by its branches: push every one, task branches included, not only the quest's. Record
   `git for-each-ref refs/heads` and `git worktree list` of each repository in a file outside it.
2. **Install** (`install.sh --dry-run`, then `install.sh`), remove `~/.local/bin/lease`, restart
   sessions. Do it for every session on the machine at once: the old `quiet` reads the new
   `<name>.starving` marker as a strict hold.
3. **Map the work** with `campaign init --name <name>` in the home repository, then (with
   `CAMPAIGN_NO_COMMIT=1` exported, so the tool writes its state without a commit per change, and
   the mapping lands as one commit of `.campaign/<name>` and the history marks):
   - a live workstream: `campaign new <id> ...`, `git branch quest/<name>/<id> <old-branch>`,
     `campaign start <id>` (it adopts the branch);
   - two branches building one outcome: one quest, the second branch adopted as a task
     (`git branch quest/<name>/<id>--<task> <old-branch-2>`, `campaign start <id> --task <task>
     --files GLOBS`);
   - a parked workstream: a planned quest, `blocked` with the reason, its `quest/<name>/<id>` branch
     created so `campaign start` adopts it when it resumes;
   - done criteria: criteria in the quest file, each with its evidence kind, none weakened; a
     criterion that is a review ("the landing reviews clear") is the quest's lenses instead; an open
     decision: an open question in the quest file naming who decides;
   - evidence in logs: rerun as jobs, or `[artifact]` for a committed result file;
   - completed work (a ledger whose work is on `main`, reviewed and verified then): a quest with the
     ledger's criteria as `[artifact]` rows quoting the ledger's ticked line (`` `path`: "text" ``),
     its journal written on `main` in the home with Evidence and a Result (what landed, its numbers,
     its review, its docs), then
     `campaign land ID --record COMMIT --earlier --reason "LEDGER; REVIEW; CI"`;
   - another repository: its own quest (`--repo`), ordered with `--after`;
   - a pre-release campaign in `docs/campaign/` with quests in flight: move it in place, no
     `campaign init` and no second campaign (rehearsed end to end on a copy shaped like Detent's):
     1. On `main`: `mkdir -p .campaign && git mv docs/campaign .campaign/<name>`, set `"campaign": "<name>"` in its
        `state.json` (a plain name: letters, digits, `.`, `_`, `-`), commit. Recorded branch and
        worktree names stay as they are. Rename the registry entry, a file holding the home's path:
        `mv ~/.scratch/campaign/campaigns/"<old name>" ~/.scratch/campaign/campaigns/<name>`;
        until it exists, `status --all` and `job prune` do not see the campaign. The old build slot
        (`jobs/slots/<old name>-<id>`) is left for `job prune`; the first build under the new name
        starts cold.
     2. On each quest branch: `git merge main`, then in one commit `mkdir -p .campaign/<name>/journal`,
        move every `docs/campaign/journal/*.md` there and `git rm -r docs/campaign`.
     3. On each task branch not yet merged into its quest:
        `git -c merge.directoryRenames=true merge <quest branch>` (git carries the task's journal to
        the new place), then the same move for anything left under `docs/campaign`.
     4. Retag the old jobs so status, usage and prune count them: in every
        `~/.scratch/campaign/jobs/*/spec.json` whose `"campaign"` is the home's path, set it to
        `<home>/.campaign/<name>`. Their identity is untouched, so they are still reused.
     5. `campaign check` must be clean. Rows citing jobs at the old code need `campaign job rerun`
        (merging `main` changed the code); an acceptance from the earlier tool is void: rerun each
        finding's confirming job at the head and `campaign accept ID LENS --confirm FINDING=JOB`.
4. **Reviews.** An old review counts only for the exact code it examined: write it in
   `templates/review.md`'s shape with its `Commit:` and `campaign accept` it; the tool refuses when
   the quest's code differs. A review of part of the work, of an earlier commit, or with conditions
   ("APPROVE WITH CONDITIONS" is not a verdict, nor is a review under a lens that does not exist) is
   history: note what it covered under the quest
   file's Decisions, turn its conditions into criteria, and scope the new review to what changed.
5. **Check** before resuming: `campaign check` clean; every recorded branch head unchanged or an
   ancestor of its quest branch; every old worktree clean; then resume one contributor at a time
   with the Resume prompt.

## Roll back

Stop agents and jobs, `install.sh --uninstall`, and link the old skill and tools by hand, without
`lease` (the earlier skill's installer links it); `command -v lease` must print nothing. The new
files and branches stay, ignored by the old skill; what landed is ordinary history on `main`.
