# Migrating from campaign 1.x

Moving a live 1.x project to 2.0 keeps every commit, branch, worktree and document of 1.x; 2.0 adds
`.campaign/<name>/` and `quest/<name>/...` branches beside them. Nothing here rewrites history, so
rolling back is a matter of links (Roll back, below).

## What changed

| 1.x | 2.0 |
|---|---|
| 14 agent types | `campaign-contributor` and `campaign-reviewer`; an assignment or a lens carries the difference |
| Workstream ledgers, status index, exploration log, `INDEX.md`, book outline | `.campaign/<name>/state.json` (through `campaign`), quest files, journals on branches; status is derived by `campaign status` |
| Review tiers with floor, adjust, defaults and a proof audit | Four risk levels; one review per needed lens per quest, bound to the commit it saw |
| Rechecker after fixes | The lead reruns each finding's probe as a job: `campaign accept --confirm FINDING=JOB` |
| Lead, owner, workstream, contributor, reviewer | The same, and a workstream is a quest |
| Phase gates, gate reviews, periodic system reviews | Milestones in `PLAN.md` with a validation job; a system review when `campaign check` reports a conflict |
| `quiet` holds taken by waiting agents | Jobs; strict and exclusive commands stop at their deadline (exit 124); holds over 30 minutes are refused |
| `pace advise` targets and the two-agent floor | `pace` reports only |
| `lease` (AWS) | Removed: campaign provisions nothing, and `install.sh --check` fails while `lease` is installed |
| `docs/reviews/<subject>-<kind>.md` | `.campaign/<name>/reviews/<quest>-<lens>.md` |
| Branch prefixes `phase*/`, `design/`, `explore/`, `fix/`, ... | `quest/<name>/<id>` and `quest/<name>/<id>--<task>` (add `quest/**` to CI push triggers) |

## 1. Park 1.x

1. Let running agents commit their ledgers and stop; let holds drain (`quiet list`).
2. Commit what only exists in working trees: in each live worktree, `git add -A && git commit -m
   "wip: parked for the 2.0 move"` on its own branch. Adoption creates new worktrees, so an
   uncommitted change left in an old one is not lost but is not seen either.
3. Record the starting point outside the repositories, for the check in step 5 and for a rollback:
   `git for-each-ref --format='%(refname) %(objectname)' refs/heads > ~/.scratch/1x-heads.txt` and
   `git worktree list >> ~/.scratch/1x-heads.txt` in each repository.

## 2. Install

```sh
git clone https://github.com/Kronuz/agent-campaign.git ~/code/agent-campaign
~/code/agent-campaign/install.sh --dry-run    # what it would change
~/code/agent-campaign/install.sh              # replaces only campaign links; reports anything else
rm -f ~/.local/bin/lease                      # 1.x's cloud provisioning
~/code/agent-campaign/install.sh --check      # fails while lease is still installed
```

Agent types load when a session starts: restart sessions after installing. Install for every
session on a machine at once: the hold directory (`~/.scratch/quiet`) is shared, and a 1.x `quiet`
reads 2.0's `<name>.starving` marker as a strict hold. To try 2.0 in one session first, put a
directory with `campaign` and `quiet` links to the clone first on that session's PATH and launch
general-purpose agents with the agent type's body (`references/harness.md`, body fallback); do this
only while no other session uses 1.x's `quiet`.

## 3. Map active work

In the project's home repository: `campaign init --name <name>`, then per item:

| 1.x | 2.0 |
|---|---|
| A live workstream | `campaign new <id> --title T --risk R --files GLOBS --check ...`, then adopt its branch: `git branch quest/<name>/<id> <old-branch>` and `campaign start <id>` (it reuses that branch and adds a worktree and a journal) |
| Two branches that build one outcome | One quest: adopt the main branch as above, the other as a task: `git branch quest/<name>/<id>--<task> <old-branch-2>` and `campaign start <id> --task <task> --files GLOBS`. It is reviewed and lands once |
| Done criteria in a ledger | Criteria A1... in the quest file, each with its evidence kind (untagged: a job; `[artifact]`, `[decision]`, `[manual]`). Keep every obligation the ledger had; a criterion may only get stricter |
| Doing, Next, Blocked on | The journal's sections; link the old ledger from the quest file |
| An open architectural decision | An open question in the quest file's Decisions, naming who decides; the quest stays `blocked` (`campaign set <id> status=blocked reason=...`) until it is decided and recorded in `PLAN.md`'s Decisions |
| Evidence in logs or notes | Rerun it as jobs (`campaign verify <id>`, `campaign job submit --quest <id>`); a committed result file may be `[artifact]` evidence |
| A dependency on another repository | A quest per repository (`--repo PATH`), ordered with `--after`, and an integration job pinning both (`--ref NAME=PATH@REF`) |
| A landed workstream | Nothing: it is on `main` |
| A queued workstream | `campaign new`, planned |

## 4. Reviews: inherit what was examined, nothing more

A 1.x review covers what it examined: one component, one branch, one commit, maybe a plan with
conditions. It does not approve the 2.0 quest unless it examined exactly that code.

- **It examined the quest's code as it stands:** copy it to `.campaign/<name>/reviews/<id>-<lens>.md`
  with `Commit: <the commit it reviewed>`, `Verdict:` and its findings in the table shape
  (`templates/review.md`), linking the original. `campaign accept` takes it only if the quest's code
  equals that commit's (journal commits aside); otherwise it refuses and names what changed.
- **It examined part of it, or an earlier commit:** record it under the quest file's Decisions as
  history: what it covered (component, branch, commit), its verdict and its open findings. The 2.0
  review is still required; its prompt's Context names the settled findings and, when only part
  changed, scopes the review to `<reviewed commit>..<head>` so nothing is reviewed twice.
- **A conditional approval** ("approve if X") is never an acceptance: each condition becomes a
  criterion or a decision in the quest file, and the review follows the 2.0 risk table.

## 5. Validate before resuming

- `campaign check` reports no errors; `campaign status` shows every quest with the right status,
  branch and criteria; `campaign brief` in each worktree names its quest and file set.
- Every branch head in `~/.scratch/1x-heads.txt` is unchanged or an ancestor of its quest branch,
  and `git status` in every old worktree is clean (step 1).
- A fresh contributor resumes each quest from the "Resume" prompt; launch them one at a time at
  first and read `campaign usage` after each.

## Roll back

Rolling back changes links, not repositories: 2.0's state, branches and journals stay as files and
branches 1.x ignores, and what 2.0 landed is ordinary history on `main`.

1. Stop 2.0 agents; let jobs finish or cancel them (`campaign job list --all`, `campaign job
   cancel`).
2. `install.sh --uninstall` removes only 2.0's links and generated agent files.
3. Link 1.x's skill, agent types and tools from its checkout by hand, **without `lease`**: do not
   rerun 1.x's installer as it was, since it links `lease` (the author's dotfiles did, from `links.tsv`
   before `dotfiles:fb5f29a`). Then `command -v lease` must print nothing.
4. Restart sessions. A quest branch in flight continues under 1.x as an ordinary branch.
