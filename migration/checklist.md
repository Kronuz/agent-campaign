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
3. **Map the work** with `campaign init --name <name>` in the home repository, then:
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
   - another repository: its own quest (`--repo`), ordered with `--after`;
   - a pre-release campaign (`docs/campaign/`, branches `quest/<id>`): copy its state's fields into
     `campaign new` (risk, lenses, files, checks), its quest file and PLAN.md into `.campaign/<name>/`,
     adopt `quest/<id>` as `quest/<name>/<id>`, then on that branch move `docs/campaign/journal/*`
     to `.campaign/<name>/journal/` (replacing the template `start` wrote) and `git rm -r
     docs/campaign`, in one commit. That commit leaves the code as the old tool keyed it, so jobs
     the old tool ran on that machine are reused by `campaign verify` and `campaign job rerun`.
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
