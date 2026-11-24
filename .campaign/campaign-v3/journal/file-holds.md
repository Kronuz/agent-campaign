# file-holds: journal

State: blocked on lead: add scripts/campaign_tool/cli.py to file-holds' files (set lacks --force)
Quest: .campaign/campaign-v3/quests/file-holds.md (the lead's; never edit it)

Keep this current and commit it with the work, so a fresh contributor can continue. `campaign
status` reads State and the first step under Next. State is one of: working, waiting on jobs <ids>,
ready for review, fixing <findings>, blocked on <who>: <what>.

## Doing

Waiting for the lead to widen the file set by scripts/campaign_tool/cli.py.

## Next

1. Commit the one cli.py line: `p.add_argument("--force", metavar="REASON", help="widen files
   over a quest that holds them: recorded")` before `p.set_defaults(fn=cmd_set)` (cli.py:175).
2. Submit A1 (`python3 tests/run.py test_file_holds`) and its fails-without at main's
   quests.py; the two suite shards; push for CI. State "ready for review", then "waiting on jobs".

## Blocked on

Lead: `set` has no `--force` (contract: "gains --force REASON only if it lacks one"), and its
parser is in scripts/campaign_tool/cli.py, outside the file set. No live quest holds cli.py
(checked state.json: close-readers-quiet, drain-yields, evidence-once, fails-without-honest,
todo-ci-waiter). Recommendation: `campaign set file-holds
files=scripts/campaign_tool/quests.py,scripts/campaign_tool/state.py,scripts/campaign_tool/cli.py,tests/test_file_holds.py`.
Until then cmd_set reads `a.force`, which `set`'s parser does not define: the branch is not
runnable and must not land.

## Evidence

Local only so far (quiet light, on a scratch copy with the cli.py line):
`tests/run.py -j 2 test_file_holds test_quests`: 28 tests OK. The local no-fix run was
stopped unfinished (machine busy): the fails-without job is the evidence. Jobs follow once
cli.py is in the set.

| Criterion | Evidence |
|---|---|
| A1 | <no evidence yet> |
| A2 | <no evidence yet> |
| A3 | <no evidence yet> |

## Result

`set ID files=` on a quest that holds files (`writes`) refuses a set whose `clashes` gains a
quest it did not have, naming each with the globs it shares ("split (src/auth/**)"); `--force
REASON` passes and the state commit records "(forced over split: REASON)". New cross-campaign
overlaps warn, as `start` does. Code: scripts/campaign_tool/quests.py cmd_set; test:
tests/test_file_holds.py.

## Rulings I made

- Narrowing is "gains no clash": only quests that clash after the set and did not before refuse.
  A quest forced over X keeps X without --force while it narrows or keeps its files.
- --force is recorded in the state commit message, like --weaken's reason: no record format
  change (the quest's `reason` is popped by the next set, and `history` is risk/lenses/checks).
- --force with no new clash is refused ("--force: these files overlap no other quest's held
  files"), as `--weaken` with nothing to weaken is.
- A planned quest's set never refuses: it holds nothing; `start` checks it.
