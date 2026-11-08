# Moving a project from campaign 1.x

A one-time procedure for the few projects still on 1.x. Delete this file, its row in SKILL.md and
`campaign migrate` once they have moved. Nothing here rewrites history: 1.x's branches, worktrees,
ledgers and reviews stay as they are, and 2.0 adds `.campaign/<name>/` and `quest/<name>/...`
branches beside them, so rolling back is a matter of links.

## Steps

1. **Park 1.x.** Let agents commit and stop; let holds drain (`quiet list`). In every live
   worktree, commit what is uncommitted (`git add -A && git commit -m "wip: parked"`): adoption makes
   new worktrees and never sees an old one's working tree. Record `git for-each-ref refs/heads` and
   `git worktree list` of each repository in a file outside it.
2. **Install** (`install.sh --dry-run`, then `install.sh`), remove `~/.local/bin/lease`, restart
   sessions. Do it for every session on the machine at once: a 1.x `quiet` reads 2.0's
   `<name>.starving` marker as a strict hold.
3. **Map the work** with `campaign init --name <name>` in the home repository, then:
   - a live workstream: `campaign new <id> ...`, `git branch quest/<name>/<id> <old-branch>`,
     `campaign start <id>` (it adopts the branch);
   - two branches building one outcome: one quest, the second branch adopted as a task
     (`git branch quest/<name>/<id>--<task> <old-branch-2>`, `campaign start <id> --task <task>
     --files GLOBS`);
   - a parked workstream: a planned quest, `blocked` with the reason, its `quest/<name>/<id>` branch
     created so `campaign start` adopts it when it resumes;
   - done criteria: criteria in the quest file, each with its evidence kind, none weakened; an open
     decision: an open question in the quest file naming who decides;
   - evidence in logs: rerun as jobs, or `[artifact]` for a committed result file;
   - another repository: its own quest (`--repo`), ordered with `--after`.
4. **Reviews.** A 1.x review counts only for the exact code it examined: write it in
   `templates/review.md`'s shape with its `Commit:` and `campaign accept` it; the tool refuses when
   the quest's code differs. A review of part of the work, of an earlier commit, or with conditions
   ("APPROVE WITH CONDITIONS" is not a verdict) is history: note what it covered under the quest
   file's Decisions, turn its conditions into criteria, and scope the new review to what changed.
5. **Check** before resuming: `campaign check` clean; every recorded branch head unchanged or an
   ancestor of its quest branch; every old worktree clean; then resume one contributor at a time
   with the Resume prompt.

## Roll back

Stop 2.0 agents and jobs, `install.sh --uninstall`, and link 1.x's skill and tools by hand, without
`lease` (1.x's installer links it); `command -v lease` must print nothing. 2.0's files and branches
stay, ignored by 1.x; what 2.0 landed is ordinary history on `main`.
