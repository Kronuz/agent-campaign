# CI reuse across a clean merge: the measured risk

`campaign land` counts CI green at a quest's head at a merge from `main` that git completed alone
(`clean_merge` in `scripts/campaign_tool/ci.py`: no conflict, even a resolved one, and the merge's
tree is the one `git merge-tree --write-tree` writes for its parents). This page measures what that
rule would have let through on xapiand's and detent's history.

## Method

Every two-parent merge from `main` into a quest branch reachable from any ref, 2026-11-07 to
2026-11-21 (`git log --all --merges --since=14.days`, the subject `Merge [branch] main|master|
origin/main|origin/master into BRANCH`, the second parent on `main`, the merge itself not on
`main`'s first-parent line). For each:

1. Clean: `git merge-tree --write-tree` of its parents exits 0 and writes the merge's tree, run
   against a scratch object directory so the repositories stay untouched.
2. Same file: main's side (merge base to second parent) and the quest's side (merge base to first
   parent) change a common path outside `.campaign/`.
3. CI at the pre-merge head and at the merge: `gh run list --commit`, green when every run
   finished green, red when any failed, else cancelled, running or none.

## Result

| Repository | Merges from main | Conflict | Clean | Clean, same file | Clean, head green | Then merge CI green | Then merge CI red | Then cancelled, running or none |
|---|---|---|---|---|---|---|---|---|
| xapiand | 349 | 7 | 342 | 11 | 84 | 43 | 4 | 37 |
| detent | 132 | 5 | 127 | 7 | 15 | 7 | 0 | 8 |
| **Both** | 481 | 12 | 469 | 18 | 99 | 50 | 4 | 45 |

1. 98% of merges from main are clean (469 of 481); none edited anything beside the merge.
2. Of the 99 clean merges whose head was green, 54 got a finished CI run at the merge: 50 green,
   4 red. The other 45 never finished: cancelled by the next push or never run, which is the
   livelock this rule removes.
3. Two of the 4 reds were `main` itself red at the merged commit (xapiand `5b3dc8649a8d`, main
   `bf593a759d69`; `9a2ef8bf4a81`, main `1c80917208f5`): the landing would add nothing to a red
   `main`. One was FreeBSD's ctest alone, with `main` and the head green on every platform
   (`6aaf86f8d2de`, run 44607481291): an interaction or a flake. One is unattributed
   (`56332bd11fe1`: no CI on `main`'s side within 25 commits).
4. Of the 18 clean same-file merges, 5 had a green head; the 3 of those whose merge run finished
   were green.

So at most 2 of 54 decided clean merges (4%) went red for a reason the merge could have
introduced; `main_ci_problems` reports such a landing as an error in `check` and `status` once
`main`'s CI runs, naming it and its fix-forward command.
